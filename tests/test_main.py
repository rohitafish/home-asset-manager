"""Unit test for app/main.py's security response headers.

Tests the header-setting function directly against a plain Response: these
pin the *content* of the policy, which is easier to read as a set of
assertions on a bare Response than as assertions on a live response's
headers. tests/test_route_security.py covers the complementary question of
whether the middleware carrying them is mounted at all.
"""

from datetime import datetime

import pytest
from fastapi import Response
from sqlmodel import Session, select

from app.main import (
    _allowed_hosts,
    _apply_security_headers,
    _is_secure,
    fail_orphaned_discovery_runs,
    require_admin_password_configured,
)
from app.models import DiscoveryRun


def test_applies_all_security_headers():
    response = _apply_security_headers(Response())
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["Referrer-Policy"] == "same-origin"

    csp = response.headers["Content-Security-Policy"]
    assert "frame-ancestors 'none'" in csp
    assert "object-src 'none'" in csp
    assert "default-src 'self'" in csp


def test_referrer_policy_is_not_no_referrer():
    """Regression test: a browser honoring "no-referrer" sends Origin: null
    on an HTML form POST, even a same-origin one (Fetch spec) -- which
    app/auth.py's require_same_origin then rejects, since it's the same
    signature a sandboxed-iframe/data: URI CSRF bypass produces. This broke
    every plain form submission in the app for every standards-compliant
    browser (traced via a live user report, reproduced with the exact
    captured header set both locally and against the deployed instance).
    Pinned here so "no-referrer" can't quietly come back as a
    privacy-tightening edit without someone re-reading this history."""
    assert _apply_security_headers(Response()).headers["Referrer-Policy"] != "no-referrer"


def test_csp_permits_self_hosted_inline_script_and_style():
    """script-src/style-src deliberately keep 'unsafe-inline' -- see the
    comment above _SECURITY_HEADERS for why (a handful of inline onclick/
    onchange/onsubmit handlers, no untrusted-HTML render path for it to
    defend against). Pin the choice so it isn't silently tightened or
    loosened without updating that reasoning."""
    csp = _apply_security_headers(Response()).headers["Content-Security-Policy"]
    assert "script-src 'self' 'unsafe-inline'" in csp
    assert "style-src 'self' 'unsafe-inline'" in csp


def test_hsts_only_on_a_secure_response():
    """HTTP Basic carries the password on every request, so the app is meant
    to be reached over TLS (README, "Reaching it over HTTPS"). HSTS pins the
    browser to https once it has seen it -- but a browser ignores the header
    on a plain-http response, and sending it there would only mislead anyone
    reading the headers into thinking the transport was covered."""
    plain = _apply_security_headers(Response())
    assert "Strict-Transport-Security" not in plain.headers

    secure = _apply_security_headers(Response(), secure=True)
    assert secure.headers["Strict-Transport-Security"] == "max-age=31536000"
    assert "preload" not in secure.headers["Strict-Transport-Security"]


def _request(scheme="http", headers=None):
    from starlette.requests import Request

    raw_headers = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    return Request({"type": "http", "method": "GET", "scheme": scheme, "path": "/",
                    "headers": raw_headers, "query_string": b"", "server": ("localhost", 80)})


def test_is_secure_from_scheme_or_forwarded_proto():
    assert _is_secure(_request(scheme="https"))
    assert _is_secure(_request(headers={"X-Forwarded-Proto": "HTTPS"}))
    assert not _is_secure(_request())
    assert not _is_secure(_request(headers={"X-Forwarded-Proto": "http"}))


def test_allowed_hosts_always_include_loopback(monkeypatch):
    """scripts/redeploy.sh and mini-brew-upgrade.sh curl /health as
    127.0.0.1; local development uses localhost. Neither should depend on
    someone remembering to list them."""
    monkeypatch.delenv("APP_ALLOWED_HOSTS", raising=False)
    assert _allowed_hosts() == ["127.0.0.1", "localhost"]


def test_allowed_hosts_adds_the_configured_proxy_names(monkeypatch):
    monkeypatch.setenv("APP_ALLOWED_HOSTS", " Mini.tail1234.ts.net, assets.home ,,")
    assert _allowed_hosts() == ["127.0.0.1", "assets.home", "localhost", "mini.tail1234.ts.net"]


def test_allowed_hosts_default_is_not_a_wildcard(monkeypatch):
    """Starlette treats "*" as allow-everything. An operator can still put
    that in APP_ALLOWED_HOSTS deliberately; what this pins is that an unset
    or blank variable never quietly means "answer for anyone"."""
    for value in (None, "", " , ,"):
        if value is None:
            monkeypatch.delenv("APP_ALLOWED_HOSTS", raising=False)
        else:
            monkeypatch.setenv("APP_ALLOWED_HOSTS", value)
        assert "*" not in _allowed_hosts()


def test_does_not_also_set_x_frame_options():
    """frame-ancestors 'none' supersedes X-Frame-Options in every browser
    this app is used from -- only one should be set, not both."""
    response = _apply_security_headers(Response())
    assert "X-Frame-Options" not in response.headers


# -- startup gates ------------------------------------------------------------
# Both startup hooks were uncovered. They run once, before the app serves
# anything, and each exists to stop a specific bad state from becoming the
# steady state -- which is exactly why neither gets exercised in normal use.


def test_refuses_to_start_without_an_admin_password(monkeypatch):
    """The gate that matters most: secrets.compare_digest("", "") is True, so
    an empty APP_ADMIN_PASSWORD is not "no auth" -- it's "any password
    works". Booting anyway would serve the whole dashboard to the LAN behind
    a lock that opens for anyone."""
    monkeypatch.delenv("APP_ADMIN_PASSWORD", raising=False)

    with pytest.raises(RuntimeError, match="APP_ADMIN_PASSWORD"):
        require_admin_password_configured()


def test_refuses_to_start_on_the_env_example_placeholder(monkeypatch):
    """`cp .env.example .env` is the documented setup step, and it leaves
    the literal 'change-me' behind if the next edit gets skipped. A
    placeholder that's published in the repo is a known password, not a
    weak one."""
    monkeypatch.setenv("APP_ADMIN_PASSWORD", "change-me")

    with pytest.raises(RuntimeError, match="change-me"):
        require_admin_password_configured()


def test_refuses_to_start_on_a_blank_string_password(monkeypatch):
    monkeypatch.setenv("APP_ADMIN_PASSWORD", "")

    with pytest.raises(RuntimeError):
        require_admin_password_configured()


def test_starts_with_a_real_password_configured(monkeypatch):
    """The counterweight -- the gate must not be so eager that a correctly
    configured instance can't boot."""
    monkeypatch.setenv("APP_ADMIN_PASSWORD", "a-real-password")

    require_admin_password_configured()  # must not raise


def test_orphaned_running_discovery_runs_are_failed_on_startup(engine, monkeypatch):
    """A restart -- redeploy, crash, or a power cut on the Mini -- abandons
    an in-flight discovery run mid-process, leaving its row stuck at
    "running" forever. That row is also what _discovery_already_running
    consults, so without this cleanup one interrupted run blocks every future
    scan from the dashboard until someone edits the database by hand."""
    monkeypatch.setattr("app.main.engine", engine)
    with Session(engine) as session:
        session.add(DiscoveryRun(source="nmap", status="running",
                                 started_at=datetime(2026, 8, 1)))
        session.commit()

    fail_orphaned_discovery_runs()

    with Session(engine) as session:
        run = session.exec(select(DiscoveryRun)).one()
        assert run.status == "failed"
        assert run.finished_at is not None
        assert "Interrupted by app restart" in run.summary


def test_completed_and_failed_runs_are_left_alone_on_startup(engine, monkeypatch):
    """Only the stuck ones -- rewriting finished history on every boot would
    destroy the discovery log the dashboard shows."""
    monkeypatch.setattr("app.main.engine", engine)
    with Session(engine) as session:
        session.add(DiscoveryRun(source="unifi", status="completed",
                                 started_at=datetime(2026, 8, 1),
                                 finished_at=datetime(2026, 8, 1, 0, 5),
                                 summary="created=3"))
        session.add(DiscoveryRun(source="nmap", status="failed",
                                 started_at=datetime(2026, 8, 2),
                                 summary="controller unreachable"))
        session.commit()

    fail_orphaned_discovery_runs()

    with Session(engine) as session:
        summaries = {r.source: r.summary for r in session.exec(select(DiscoveryRun)).all()}
        assert summaries == {"unifi": "created=3", "nmap": "controller unreachable"}


def test_startup_cleanup_is_a_no_op_with_nothing_to_clean(engine, monkeypatch):
    """The ordinary case -- a clean shutdown -- must not commit anything."""
    monkeypatch.setattr("app.main.engine", engine)

    fail_orphaned_discovery_runs()

    with Session(engine) as session:
        assert session.exec(select(DiscoveryRun)).all() == []
