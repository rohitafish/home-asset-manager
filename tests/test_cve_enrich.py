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
from app.models import AssetService, Finding
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
