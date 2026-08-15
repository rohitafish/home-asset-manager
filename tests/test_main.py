"""Unit test for app/main.py's security response headers.

Tests the header-setting function directly against a plain Response, the
same way app/auth.py's require_admin/require_same_origin are tested as
callables rather than through a live app (see tests/test_auth.py's
docstring -- this app has no route-level TestClient).
"""

from fastapi import Response

from app.main import _apply_security_headers


def test_applies_all_security_headers():
    response = _apply_security_headers(Response())
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["Referrer-Policy"] == "no-referrer"

    csp = response.headers["Content-Security-Policy"]
    assert "frame-ancestors 'none'" in csp
    assert "object-src 'none'" in csp
    assert "default-src 'self'" in csp


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
