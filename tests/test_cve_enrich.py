"""Tests for discovery/cve_enrich.py's NVD keyword deduplication -- added
alongside the AI spending-boundaries work as the non-LLM equivalent of the
same problem: enrich_findings_from_services() used to fire one NVD request
(with a 6s/0.2s rate-limit sleep each) per AssetService row, so two devices
sharing an identical "<product> <version>" banner paid for the same query
twice. Network calls are stubbed by monkeypatching the module-level
fetch_kev_catalog/search_nvd_by_keyword/fetch_epss_scores functions, same
spirit as tests/test_sonos_household.py.
"""

from conftest import make_asset
from sqlmodel import select

import discovery.cve_enrich as cve_enrich
from app.models import AssetService, Exposure, Finding, FindingStatus, Severity
from discovery.cve_enrich import enrich_findings_from_services


def _stub_kev_and_epss(monkeypatch):
    monkeypatch.setattr(cve_enrich, "fetch_kev_catalog", lambda client: set())
    monkeypatch.setattr(cve_enrich, "fetch_epss_scores", lambda client, ids: {})


def test_identical_banners_query_nvd_only_once(session, monkeypatch):
    a1 = make_asset(session)
    a2 = make_asset(session)
    session.add(AssetService(asset_id=a1.id, port=80, product="nginx", version="1.18.0"))
    session.add(AssetService(asset_id=a2.id, port=8080, product="nginx", version="1.18.0"))
    session.commit()

    _stub_kev_and_epss(monkeypatch)
    calls = []

    def fake_search(client, keyword, results_limit=5):
        calls.append(keyword)
        return []

    monkeypatch.setattr(cve_enrich, "search_nvd_by_keyword", fake_search)

    summary = enrich_findings_from_services(session)

    assert calls == ["nginx 1.18.0"]  # one query, not two
    assert summary["services_checked"] == 2
    assert summary["nvd_queries"] == 1


def test_a_shared_query_result_creates_findings_for_every_matching_service(session, monkeypatch):
    a1 = make_asset(session)
    a2 = make_asset(session)
    session.add(AssetService(asset_id=a1.id, port=80, product="nginx", version="1.18.0"))
    session.add(AssetService(asset_id=a2.id, port=8080, product="nginx", version="1.18.0"))
    session.commit()

    _stub_kev_and_epss(monkeypatch)
    monkeypatch.setattr(
        cve_enrich, "search_nvd_by_keyword",
        lambda client, keyword, results_limit=5: [
            {
                "cve_id": "CVE-2020-0001", "cvss_score": 9.8, "cvss_version": "3.1",
                "description": "d", "published_date": None,
            },
        ],
    )

    summary = enrich_findings_from_services(session)

    findings = session.exec(select(Finding)).all()
    assert {f.asset_id for f in findings} == {a1.id, a2.id}
    assert summary["findings_created"] == 2
    assert summary["vulnerabilities_created"] == 1  # one Vulnerability row, reused


def _run_with_score(session, monkeypatch, score):
    monkeypatch.setattr(
        cve_enrich, "search_nvd_by_keyword",
        lambda client, keyword, results_limit=5: [
            {
                "cve_id": "CVE-2020-0001", "cvss_score": score, "cvss_version": "3.1",
                "description": "d", "published_date": None,
            },
        ],
    )
    return enrich_findings_from_services(session)


def test_open_finding_is_rescored_when_the_vulnerability_gets_worse(session, monkeypatch):
    """Regression test: a Finding used to be frozen at whatever severity/SLA
    applied when it was first created -- a CVE later revised to a much
    higher CVSS score (or added to KEV) kept displaying at its original,
    lower severity with the original, now-too-late SLA due date, even
    though the underlying Vulnerability row WAS refreshed every run."""
    asset = make_asset(session)
    session.add(AssetService(asset_id=asset.id, port=80, product="nginx", version="1.18.0"))
    session.commit()
    _stub_kev_and_epss(monkeypatch)

    _run_with_score(session, monkeypatch, 3.0)  # low
    finding = session.exec(select(Finding)).one()
    assert finding.severity == Severity.low
    original_detected = finding.detected_date
    original_due = finding.sla_due_date

    _run_with_score(session, monkeypatch, 9.8)  # critical, on a later run
    session.refresh(finding)

    assert finding.severity == Severity.critical
    assert finding.detected_date == original_detected  # detection time itself is untouched
    assert finding.sla_due_date != original_due  # recomputed for the new severity
    # ...from the ORIGINAL detection date, not "now" -- re-scoring must not
    # also silently grant extra time by resetting the SLA clock.
    assert finding.sla_due_date == cve_enrich.sla_due_date(
        Severity.critical, finding.exposure, original_detected
    )


def test_rescore_also_updates_stored_exposure(session, monkeypatch):
    """Regression test: rescoring recomputed sla_due_date from a fresh
    local `exposure` but never reassigned existing.exposure -- the stored
    exposure and the due date supposedly derived from it ended up computed
    from two different SLA inputs. Change is_internet_facing between runs
    (alongside a severity change, since that's what actually triggers the
    rescore branch) and confirm the stored exposure follows the new value."""
    asset = make_asset(session, is_internet_facing=False)
    session.add(AssetService(asset_id=asset.id, port=80, product="nginx", version="1.18.0"))
    session.commit()
    _stub_kev_and_epss(monkeypatch)

    _run_with_score(session, monkeypatch, 3.0)  # low, internal
    finding = session.exec(select(Finding)).one()
    assert finding.exposure == Exposure.internal

    asset.is_internet_facing = True
    session.add(asset)
    session.commit()
    _run_with_score(session, monkeypatch, 9.8)  # critical + now internet-facing -> triggers rescore
    session.refresh(finding)

    assert finding.exposure == Exposure.internet_facing
    assert finding.sla_due_date == cve_enrich.sla_due_date(
        Severity.critical, Exposure.internet_facing, finding.detected_date
    )


def test_rescore_triggers_on_exposure_change_alone(session, monkeypatch):
    """A finding whose severity never changes again must still pick up an
    is_internet_facing flip on the next enrichment run -- before this, only
    a severity change entered the rescore branch at all, so an
    exposure-only drift (the asset edited, or an AI proposal applied, with
    no accompanying CVE severity change) was invisible forever."""
    asset = make_asset(session, is_internet_facing=False)
    session.add(AssetService(asset_id=asset.id, port=80, product="nginx", version="1.18.0"))
    session.commit()
    _stub_kev_and_epss(monkeypatch)

    _run_with_score(session, monkeypatch, 9.8)  # critical, internal
    finding = session.exec(select(Finding)).one()
    assert finding.exposure == Exposure.internal
    original_due = finding.sla_due_date

    asset.is_internet_facing = True
    session.add(asset)
    session.commit()
    _run_with_score(session, monkeypatch, 9.8)  # SAME score -- no severity change
    session.refresh(finding)

    assert finding.severity == Severity.critical  # unchanged, as expected
    assert finding.exposure == Exposure.internet_facing  # but exposure still followed
    assert finding.sla_due_date != original_due
    assert finding.sla_due_date == cve_enrich.sla_due_date(
        Severity.critical, Exposure.internet_facing, finding.detected_date
    )


def test_mitigated_finding_is_not_rescored(session, monkeypatch):
    """A finding a human has already mitigated/accepted/closed keeps their
    call -- re-scoring only applies to still-open findings."""
    asset = make_asset(session)
    session.add(AssetService(asset_id=asset.id, port=80, product="nginx", version="1.18.0"))
    session.commit()
    _stub_kev_and_epss(monkeypatch)

    _run_with_score(session, monkeypatch, 3.0)
    finding = session.exec(select(Finding)).one()
    finding.status = FindingStatus.mitigated
    session.add(finding)
    session.commit()
    original_due = finding.sla_due_date

    _run_with_score(session, monkeypatch, 9.8)
    session.refresh(finding)

    assert finding.severity == Severity.low  # untouched
    assert finding.sla_due_date == original_due  # untouched
    assert finding.status == FindingStatus.mitigated


def test_different_versions_still_query_separately(session, monkeypatch):
    a1 = make_asset(session)
    a2 = make_asset(session)
    session.add(AssetService(asset_id=a1.id, port=80, product="nginx", version="1.18.0"))
    session.add(AssetService(asset_id=a2.id, port=80, product="nginx", version="1.20.0"))
    session.commit()

    _stub_kev_and_epss(monkeypatch)
    calls = []
    monkeypatch.setattr(
        cve_enrich, "search_nvd_by_keyword",
        lambda client, keyword, results_limit=5: calls.append(keyword) or [],
    )

    summary = enrich_findings_from_services(session)

    assert sorted(calls) == ["nginx 1.18.0", "nginx 1.20.0"]
    assert summary["nvd_queries"] == 2


def test_max_keywords_env_cap_limits_distinct_queries(session, monkeypatch):
    for i in range(5):
        a = make_asset(session)
        session.add(AssetService(asset_id=a.id, port=80 + i, product=f"product{i}", version="1.0"))
    session.commit()

    monkeypatch.setenv("CVE_ENRICH_MAX_KEYWORDS", "2")
    _stub_kev_and_epss(monkeypatch)
    calls = []
    monkeypatch.setattr(
        cve_enrich, "search_nvd_by_keyword",
        lambda client, keyword, results_limit=5: calls.append(keyword) or [],
    )

    summary = enrich_findings_from_services(session)

    assert len(calls) == 2  # capped, not one query per one of the 5 distinct keywords
    assert summary["nvd_queries"] == 2


# -- search_nvd_by_keyword's rate-limit pacing --------------------------------


class _FakeNvdResponse:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {"vulnerabilities": []}

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class _FakeNvdClient:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self.payload = payload
        self.calls = []

    def get(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return _FakeNvdResponse(self.status_code, self.payload)


def test_search_nvd_by_keyword_paces_even_on_a_403(monkeypatch):
    """A 403 is exactly what NVD returns when the keyless 5-req/30s rate
    limit is exceeded. Regression test: the pacing sleep used to live after
    the 404/403 early return, so the very request that got throttled was
    the one request that skipped pacing -- every remaining keyword in a run
    would then fire back-to-back, each getting another 403."""
    monkeypatch.delenv("NVD_API_KEY", raising=False)
    sleeps = []
    monkeypatch.setattr(cve_enrich.time, "sleep", lambda s: sleeps.append(s))

    result = cve_enrich.search_nvd_by_keyword(_FakeNvdClient(403), "some keyword")

    assert result == []
    assert sleeps == [6.0]


def test_search_nvd_by_keyword_paces_on_the_happy_path_too(monkeypatch):
    monkeypatch.setenv("NVD_API_KEY", "test-key")
    sleeps = []
    monkeypatch.setattr(cve_enrich.time, "sleep", lambda s: sleeps.append(s))

    result = cve_enrich.search_nvd_by_keyword(_FakeNvdClient(200), "some keyword")

    assert result == []  # no vulnerabilities in the fake response
    assert sleeps == [0.2]  # the shorter, API-key pace


# -- _best_cvss ---------------------------------------------------------------


def test_best_cvss_prefers_the_newest_metric_version_present():
    cve_item = {
        "metrics": {
            "cvssMetricV2": [{"cvssData": {"baseScore": 5.0, "version": "2.0"}}],
            "cvssMetricV31": [{"cvssData": {"baseScore": 9.8, "version": "3.1"}}],
        }
    }
    assert cve_enrich._best_cvss(cve_item) == (9.8, "3.1")


def test_best_cvss_falls_back_through_the_priority_order():
    cve_item = {"metrics": {"cvssMetricV2": [{"cvssData": {"baseScore": 5.0, "version": "2.0"}}]}}
    assert cve_enrich._best_cvss(cve_item) == (5.0, "2.0")


def test_best_cvss_none_when_no_metrics_present():
    assert cve_enrich._best_cvss({"metrics": {}}) == (None, None)
    assert cve_enrich._best_cvss({}) == (None, None)


# -- fetch_kev_catalog / fetch_epss_scores -------------------------------------


class _FakeSimpleClient:
    """Generic fake httpx.Client stand-in: returns the given payload from
    every .get() call and records the calls made, for asserting on URLs/params
    (e.g. the EPSS batching boundary below)."""

    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append({"url": url, "params": params, "timeout": timeout})
        return _FakeNvdResponse(200, self.payload)


def test_fetch_kev_catalog_extracts_cve_ids_into_a_set():
    client = _FakeSimpleClient({"vulnerabilities": [{"cveID": "CVE-2020-0001"}, {"cveID": "CVE-2020-0002"}]})
    assert cve_enrich.fetch_kev_catalog(client) == {"CVE-2020-0001", "CVE-2020-0002"}


def test_fetch_kev_catalog_empty_feed():
    client = _FakeSimpleClient({"vulnerabilities": []})
    assert cve_enrich.fetch_kev_catalog(client) == set()


def test_fetch_epss_scores_parses_scores_as_floats():
    client = _FakeSimpleClient({"data": [{"cve": "CVE-2020-0001", "epss": "0.973"}]})
    assert cve_enrich.fetch_epss_scores(client, ["CVE-2020-0001"]) == {"CVE-2020-0001": 0.973}


def test_fetch_epss_scores_batches_at_100_ids_per_request():
    # search_nvd_by_keyword's own docstring names 100 as the batch boundary;
    # 150 ids must produce exactly 2 requests, not 1 (truncating the tail)
    # or 150 (one per id).
    client = _FakeSimpleClient({"data": []})
    cve_ids = [f"CVE-2020-{i:04d}" for i in range(150)]

    cve_enrich.fetch_epss_scores(client, cve_ids)

    assert len(client.calls) == 2
    first_batch = client.calls[0]["params"]["cve"].split(",")
    second_batch = client.calls[1]["params"]["cve"].split(",")
    assert len(first_batch) == 100
    assert len(second_batch) == 50


def test_fetch_epss_scores_empty_id_list_makes_no_requests():
    client = _FakeSimpleClient({"data": []})
    assert cve_enrich.fetch_epss_scores(client, []) == {}
    assert client.calls == []


# -- search_nvd_by_keyword's result-shaping loop -------------------------------


def _nvd_item(cve_id, score=9.8, version="3.1", lang_and_text=(("en", "An English description."),), published="2024-01-15T10:00:00.000"):
    return {
        "cve": {
            "id": cve_id,
            "metrics": {"cvssMetricV31": [{"cvssData": {"baseScore": score, "version": version}}]},
            "descriptions": [{"lang": lang, "value": text} for lang, text in lang_and_text],
            "published": published,
        }
    }


def test_search_nvd_by_keyword_shapes_a_real_result(monkeypatch):
    monkeypatch.setattr(cve_enrich.time, "sleep", lambda s: None)
    client = _FakeNvdClient(200, {"vulnerabilities": [_nvd_item("CVE-2024-0001")]})

    results = cve_enrich.search_nvd_by_keyword(client, "nginx 1.18.0")

    assert results == [
        {
            "cve_id": "CVE-2024-0001",
            "cvss_score": 9.8,
            "cvss_version": "3.1",
            "description": "An English description.",
            "published_date": "2024-01-15T10:00:00.000",
        }
    ]


def test_search_nvd_by_keyword_prefers_the_english_description(monkeypatch):
    monkeypatch.setattr(cve_enrich.time, "sleep", lambda s: None)
    item = _nvd_item(
        "CVE-2024-0002",
        lang_and_text=(("fr", "Description en francais."), ("en", "The English one.")),
    )
    client = _FakeNvdClient(200, {"vulnerabilities": [item]})

    results = cve_enrich.search_nvd_by_keyword(client, "keyword")

    assert results[0]["description"] == "The English one."


def test_search_nvd_by_keyword_description_none_when_no_english_entry(monkeypatch):
    monkeypatch.setattr(cve_enrich.time, "sleep", lambda s: None)
    item = _nvd_item("CVE-2024-0003", lang_and_text=(("fr", "Uniquement en francais."),))
    client = _FakeNvdClient(200, {"vulnerabilities": [item]})

    results = cve_enrich.search_nvd_by_keyword(client, "keyword")

    assert results[0]["description"] is None


def test_search_nvd_by_keyword_passes_the_api_key_header_when_set(monkeypatch):
    monkeypatch.setenv("NVD_API_KEY", "test-key-123")
    monkeypatch.setattr(cve_enrich.time, "sleep", lambda s: None)
    client = _FakeNvdClient(200)

    cve_enrich.search_nvd_by_keyword(client, "keyword")

    assert client.calls[0][1]["headers"] == {"apiKey": "test-key-123"}


# -- ISO-date normalization in enrich_findings_from_services -------------------


def test_published_date_is_stored_as_naive_utc(session, monkeypatch):
    asset = make_asset(session)
    session.add(AssetService(asset_id=asset.id, port=80, product="nginx", version="1.18.0"))
    session.commit()
    monkeypatch.setattr(cve_enrich, "fetch_kev_catalog", lambda client: set())
    monkeypatch.setattr(cve_enrich, "fetch_epss_scores", lambda client, ids: {})
    monkeypatch.setattr(
        cve_enrich, "search_nvd_by_keyword",
        lambda client, keyword, results_limit=5: [
            {
                "cve_id": "CVE-2024-0001", "cvss_score": 9.8, "cvss_version": "3.1",
                "description": "d", "published_date": "2024-01-15T10:00:00.000Z",
            },
        ],
    )

    enrich_findings_from_services(session)

    from datetime import datetime

    vuln = session.exec(select(cve_enrich.Vulnerability)).one()
    assert vuln.published_date == datetime(2024, 1, 15, 10, 0, 0)
    assert vuln.published_date.tzinfo is None
