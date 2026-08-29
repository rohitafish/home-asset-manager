"""Unit test for app/main.py's security response headers.

Tests the header-setting function directly against a plain Response: these
pin the *content* of the policy, which is easier to read as a set of
assertions on a bare Response than as assertions on a live response's
headers. tests/test_route_security.py covers the complementary question of
whether the middleware carrying them is mounted at all.
"""

from fastapi import Response

from app.main import _apply_security_headers


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


def test_does_not_also_set_x_frame_options():
    """frame-ancestors 'none' supersedes X-Frame-Options in every browser
    this app is used from -- only one should be set, not both."""
    response = _apply_security_headers(Response())
    assert "X-Frame-Options" not in response.headers
