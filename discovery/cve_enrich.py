"""CVE enrichment: matches nmap-detected service versions against the NVD CVE
API (keyword search), then layers on FIRST EPSS scores and the CISA KEV
catalogue, and computes severity + SLA due dates using standard CVSS severity
bands and an exposure-based SLA matrix.

Verified live against the real NVD/EPSS/CISA endpoints during development:
  NVD:  https://services.nvd.nist.gov/rest/json/cves/2.0
  EPSS: https://api.first.org/data/v1/epss
  KEV:  https://www.cisa.gov/sites/default/files/feeds/known_exploited_vulnerabilities.json

Note: this is best-effort keyword matching on "<product> <version>" strings
from nmap's `-sV` banners, not authoritative CPE matching -- nmap doesn't
produce real CPEs, so false positives/negatives are expected and findings
should be spot-checked, same as any lightweight scanner.
"""

import logging
import os
import time
from datetime import datetime, timedelta

import httpx
from sqlmodel import Session, select

from app.clock import utcnow_naive
from app.models import (
    Asset,
    AssetService,
    Exposure,
    Finding,
    FindingStatus,
    Severity,
    Vulnerability,
)

logger = logging.getLogger(__name__)

NVD_API = "https://services.nvd.nist.gov/rest/json/cves/2.0"
EPSS_API = "https://api.first.org/data/v1/epss"
KEV_FEED = (
    "https://www.cisa.gov/sites/default/files/feeds/known_exploited_vulnerabilities.json"
)

# Hard cap on distinct "<product> <version>" keywords queried against NVD in
# one enrich_findings_from_services() run -- a backstop against the fan-out
# growing unbounded (one 6s-paced request per keyword otherwise), not a
# limit this app's normal home-network scale (a few dozen services) should
# ever approach. "" counts as unset, same idiom as ANTHROPIC_MODEL in
# app/assistant.py.
DEFAULT_MAX_NVD_KEYWORDS = 200

SLA_DAYS: dict[tuple[Severity, Exposure], int] = {
    (Severity.critical, Exposure.internet_facing): 7,
    (Severity.critical, Exposure.internal): 14,
    (Severity.high, Exposure.internet_facing): 14,
    (Severity.high, Exposure.internal): 30,
    (Severity.medium, Exposure.internet_facing): 30,
    (Severity.medium, Exposure.internal): 60,
    (Severity.low, Exposure.internet_facing): 90,
    (Severity.low, Exposure.internal): 90,
}


def severity_from_score(score: float | None) -> Severity:
    if score is None:
        return Severity.low
    if score >= 9.0:
        return Severity.critical
    if score >= 7.0:
        return Severity.high
    if score >= 4.0:
        return Severity.medium
    return Severity.low


def sla_due_date(severity: Severity, exposure: Exposure, detected_date: datetime) -> datetime:
    days = SLA_DAYS.get((severity, exposure), 90)
    return detected_date + timedelta(days=days)


def fetch_kev_catalog(client: httpx.Client) -> set[str]:
    resp = client.get(KEV_FEED, timeout=30.0)
    resp.raise_for_status()
    data = resp.json()
    return {v["cveID"] for v in data.get("vulnerabilities", [])}


def fetch_epss_scores(client: httpx.Client, cve_ids: list[str]) -> dict[str, float]:
    scores: dict[str, float] = {}
    for i in range(0, len(cve_ids), 100):
        batch = cve_ids[i : i + 100]
        resp = client.get(EPSS_API, params={"cve": ",".join(batch)}, timeout=30.0)
        resp.raise_for_status()
        for row in resp.json().get("data", []):
            scores[row["cve"]] = float(row["epss"])
    return scores


def _best_cvss(cve_item: dict) -> tuple[float | None, str | None]:
    metrics = cve_item.get("metrics", {})
    for key in ("cvssMetricV40", "cvssMetricV31", "cvssMetricV30", "cvssMetricV2"):
        entries = metrics.get(key)
        if entries:
            data = entries[0]["cvssData"]
            return data.get("baseScore"), data.get("version")
    return None, None


def search_nvd_by_keyword(
    client: httpx.Client, keyword: str, results_limit: int = 5
) -> list[dict]:
    headers = {}
    api_key = os.environ.get("NVD_API_KEY")
    if api_key:
        headers["apiKey"] = api_key
    # The pacing sleep at the end used to be skippable via the 404/403 early
    # return below -- and 403 is exactly what NVD returns once the keyless
    # 5-req/30s rate limit is exceeded, so the first 403 in a run used to
    # remove the only thing pacing every request after it. The run would
    # then fire the rest of its keywords (up to CVE_ENRICH_MAX_KEYWORDS) back
    # to back, each getting another 403, and silently report
    # candidate_cves=0 as if nothing had matched rather than "we got
    # throttled." try/finally guarantees the sleep runs on every path.
    try:
        resp = client.get(
            NVD_API,
            params={"keywordSearch": keyword, "resultsPerPage": results_limit},
            headers=headers,
            timeout=30.0,
        )
        if resp.status_code == 404 or resp.status_code == 403:
            return []
        resp.raise_for_status()
        out = []
        for item in resp.json().get("vulnerabilities", []):
            cve = item["cve"]
            score, version = _best_cvss(cve)
            description = next(
                (d["value"] for d in cve.get("descriptions", []) if d["lang"] == "en"), None
            )
            out.append(
                {
                    "cve_id": cve["id"],
                    "cvss_score": score,
                    "cvss_version": version,
                    "description": description,
                    "published_date": cve.get("published"),
                }
            )
        return out
    finally:
        # be gentle with NVD's rate limit (5 req/30s without an API key)
        time.sleep(0.2 if api_key else 6.0)


def enrich_findings_from_services(session: Session) -> dict[str, int]:
    """For every AssetService with both a product name and a version, search
    NVD for candidate CVEs and create/refresh Finding rows. Returns counts for
    reporting.

    Requires a version, not just a product name: a bare product keyword (e.g.
    "Uvicorn" with no version) is too generic for NVD's keyword search and
    reliably surfaces false positives -- e.g. CVE-2025-27519, a vulnerability
    in an unrelated product ("Cognita") whose description happens to mention
    "uvicorn server", matched purely on that shared word with no version to
    disambiguate. Skipping unversioned services trades some coverage for
    precision, consistent with this being best-effort matching (see README).

    Queries NVD once per distinct "<product> <version>" keyword, not once
    per service row: two services sharing an identical banner (e.g. two of
    the same smart-plug model on the same firmware) previously paid
    search_nvd_by_keyword's 6s/0.2s rate-limit pacing sleep twice for an
    identical result. Every service sharing a keyword reuses that one
    query's result. The distinct-keyword count is additionally capped by
    CVE_ENRICH_MAX_KEYWORDS (default DEFAULT_MAX_NVD_KEYWORDS) as a backstop
    against the fan-out growing unbounded; services past the cap are simply
    skipped this run (a later run picks them up -- there's no "already
    tried" marker to fight, unlike app/assistant.py's model-number guess).
    """
    with httpx.Client() as client:
        kev_ids = fetch_kev_catalog(client)

        services = session.exec(
            select(AssetService).where(
                AssetService.product.is_not(None), AssetService.version.is_not(None)
            )
        ).all()

        services_by_keyword: dict[str, list[AssetService]] = {}
        for svc in services:
            keyword = f"{svc.product} {svc.version}"
            services_by_keyword.setdefault(keyword, []).append(svc)

        max_keywords = int(os.environ.get("CVE_ENRICH_MAX_KEYWORDS") or DEFAULT_MAX_NVD_KEYWORDS)
        keywords = list(services_by_keyword)
        if len(keywords) > max_keywords:
            logger.warning(
                "cve_enrich: %s distinct product/version keywords exceeds the %s-keyword "
                "cap -- querying the first %s and skipping the rest this run",
                len(keywords), max_keywords, max_keywords,
            )
            keywords = keywords[:max_keywords]

        candidates: dict[str, dict] = {}
        service_matches: list[tuple[AssetService, list[str]]] = []
        for keyword in keywords:
            results = search_nvd_by_keyword(client, keyword)
            cve_ids = [r["cve_id"] for r in results]
            for r in results:
                candidates[r["cve_id"]] = r
            for svc in services_by_keyword[keyword]:
                service_matches.append((svc, cve_ids))

        epss_scores = fetch_epss_scores(client, list(candidates.keys()))

        created_vulns = 0
        created_findings = 0
        now = utcnow_naive()

        for cve_id, info in candidates.items():
            vuln = session.exec(
                select(Vulnerability).where(Vulnerability.cve_id == cve_id)
            ).first()
            severity = severity_from_score(info["cvss_score"])
            kev_flag = cve_id in kev_ids
            if kev_flag:
                severity = Severity.critical
            if vuln is None:
                vuln = Vulnerability(cve_id=cve_id)
                created_vulns += 1
            vuln.cvss_score = info["cvss_score"]
            vuln.severity = severity
            vuln.epss_score = epss_scores.get(cve_id)
            vuln.kev_flag = kev_flag
            vuln.description = info["description"]
            if info["published_date"]:
                vuln.published_date = datetime.fromisoformat(
                    info["published_date"].replace("Z", "+00:00")
                ).replace(tzinfo=None)
            vuln.updated_at = now
            session.add(vuln)
            session.flush()

        session.commit()

        vuln_by_cve = {
            v.cve_id: v for v in session.exec(select(Vulnerability)).all()
        }

        for svc, cve_ids in service_matches:
            asset = session.get(Asset, svc.asset_id)
            exposure = (
                Exposure.internet_facing if asset.is_internet_facing else Exposure.internal
            )
            for cve_id in cve_ids:
                vuln = vuln_by_cve.get(cve_id)
                if vuln is None:
                    continue
                existing = session.exec(
                    select(Finding).where(
                        Finding.asset_id == svc.asset_id,
                        Finding.vulnerability_id == vuln.id,
                    )
                ).first()
                if existing:
                    # vuln.severity/kev_flag were just refreshed above (this
                    # run's fresh CVSS/KEV data), but a Finding created from
                    # an earlier run was frozen at whatever severity applied
                    # THEN -- so a CVE later added to KEV, or revised to a
                    # higher CVSS score, kept displaying at its original
                    # (lower) severity with the original (now too-late) SLA
                    # due date. Only touch an open finding -- one a human has
                    # already mitigated/accepted/closed keeps their call,
                    # and the due date is recomputed from the ORIGINAL
                    # detected_date, not today, so re-scoring doesn't also
                    # silently grant extra time.
                    if existing.status == FindingStatus.open and existing.severity != vuln.severity:
                        existing.severity = vuln.severity
                        # This run's fresh `exposure` is what sla_due_date is
                        # recomputed from below -- existing.exposure must be
                        # reassigned to match, or the stored exposure and the
                        # due date it's supposedly derived from come from two
                        # different SLA calculations from here on.
                        existing.exposure = exposure
                        existing.sla_due_date = sla_due_date(
                            vuln.severity, exposure, existing.detected_date
                        )
                        session.add(existing)
                    continue
                finding = Finding(
                    asset_id=svc.asset_id,
                    vulnerability_id=vuln.id,
                    severity=vuln.severity,
                    exposure=exposure,
                    detected_date=now,
                    sla_due_date=sla_due_date(vuln.severity, exposure, now),
                    status=FindingStatus.open,
                    evidence=f"nmap service match: {svc.product} {svc.version or ''} on port {svc.port}/{svc.protocol}",
                    source="nvd_keyword_match",
                )
                session.add(finding)
                created_findings += 1

        session.commit()
        return {
            "services_checked": len(services),
            "nvd_queries": len(keywords),
            "candidate_cves": len(candidates),
            "vulnerabilities_created": created_vulns,
            "findings_created": created_findings,
        }
