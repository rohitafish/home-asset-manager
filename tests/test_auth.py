"""Unit tests for app/auth.py's require_admin (Basic auth + brute-force
throttle) and require_same_origin (CSRF guard).

The app deliberately has no route-level TestClient (see conftest.py), so these
exercise the dependency callables directly against a minimally-constructed
Starlette Request rather than through a live app -- same spirit as the rest of
the suite, which unit-tests functions, not routes.
"""

import pytest
from fastapi import HTTPException, status
from fastapi.security import HTTPBasicCredentials
from starlette.requests import Request

from app import auth


def _request(method="POST", headers=None, client=("10.0.0.5", 5000)) -> Request:
    scope = {
        "type": "http",
        "method": method,
        "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()],
        "client": client,
    }
    return Request(scope)


def _creds(user="admin", password="s3cret-pass"):
    return HTTPBasicCredentials(username=user, password=password)


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    """A configured admin password and a fresh throttle table per test."""
    monkeypatch.setenv("APP_ADMIN_USER", "admin")
    monkeypatch.setenv("APP_ADMIN_PASSWORD", "s3cret-pass")
    auth._failures.clear()
    yield
    auth._failures.clear()


# --- require_admin ---------------------------------------------------------

def test_correct_credentials_return_username():
    assert auth.require_admin(_request(), _creds()) == "admin"


def test_wrong_password_is_401():
    with pytest.raises(HTTPException) as exc:
        auth.require_admin(_request(), _creds(password="nope"))
    assert exc.value.status_code == status.HTTP_401_UNAUTHORIZED


def test_non_ascii_username_is_401_not_500():
    """A non-ASCII username must not crash secrets.compare_digest (which would
    surface as an unauthenticated 500); it should just fail to match."""
    with pytest.raises(HTTPException) as exc:
        auth.require_admin(_request(), _creds(user="münchen"))
    assert exc.value.status_code == status.HTTP_401_UNAUTHORIZED


def test_blank_configured_password_never_authenticates(monkeypatch):
    monkeypatch.setenv("APP_ADMIN_PASSWORD", "")
    with pytest.raises(HTTPException) as exc:
        auth.require_admin(_request(), _creds(password=""))
    assert exc.value.status_code == status.HTTP_401_UNAUTHORIZED


def test_throttle_locks_out_after_max_failures():
    req = _request()
    for _ in range(auth._MAX_FAILURES):
        with pytest.raises(HTTPException) as exc:
            auth.require_admin(req, _creds(password="wrong"))
        assert exc.value.status_code == status.HTTP_401_UNAUTHORIZED
    # The next attempt is throttled before the password is even checked.
    with pytest.raises(HTTPException) as exc:
        auth.require_admin(req, _creds(password="wrong"))
    assert exc.value.status_code == status.HTTP_429_TOO_MANY_REQUESTS
    assert exc.value.headers.get("Retry-After")


def test_throttle_is_per_client_ip():
    for _ in range(auth._MAX_FAILURES):
        with pytest.raises(HTTPException):
            auth.require_admin(_request(client=("10.0.0.9", 1)), _creds(password="x"))
    # A different client is unaffected and can still log in.
    assert auth.require_admin(_request(client=("10.0.0.10", 1)), _creds()) == "admin"


def test_successful_login_clears_failures():
    req = _request()
    for _ in range(auth._MAX_FAILURES - 1):
        with pytest.raises(HTTPException):
            auth.require_admin(req, _creds(password="wrong"))
    assert auth.require_admin(req, _creds()) == "admin"
    assert "10.0.0.5" not in auth._failures


def test_stale_failure_entries_are_swept_on_any_request(monkeypatch):
    """Regression test: an IP that fails once and never returns used to sit
    in _failures forever -- only a *successful* login or hitting the
    throttle branch pruned anything, and neither touches an IP whose
    failures have already all aged out of the window. A request from ANY
    client (not just the stale one) should sweep it."""
    fake_now = [1000.0]
    monkeypatch.setattr(auth.time, "monotonic", lambda: fake_now[0])

    with pytest.raises(HTTPException):
        auth.require_admin(_request(client=("10.0.0.99", 1)), _creds(password="wrong"))
    assert "10.0.0.99" in auth._failures

    # Time passes well beyond the window -- that IP's one failure is now stale.
    fake_now[0] += auth._WINDOW_SECONDS + 1

    # A request from a completely different client must still sweep it.
    auth.require_admin(_request(client=("10.0.0.100", 1)), _creds())

    assert "10.0.0.99" not in auth._failures


# --- require_same_origin ---------------------------------------------------

def test_safe_method_is_always_allowed():
    assert auth.require_same_origin(_request(method="GET", headers={"sec-fetch-site": "cross-site"})) is None


@pytest.mark.parametrize("site", ["same-origin", "none"])
def test_sec_fetch_site_allows_first_party(site):
    assert auth.require_same_origin(_request(headers={"sec-fetch-site": site})) is None


@pytest.mark.parametrize("site", ["cross-site", "same-site"])
def test_sec_fetch_site_rejects_other_sites(site):
    with pytest.raises(HTTPException) as exc:
        auth.require_same_origin(_request(headers={"sec-fetch-site": site}))
    assert exc.value.status_code == status.HTTP_403_FORBIDDEN


def test_falls_back_to_origin_match_when_no_sec_fetch_site():
    ok = _request(headers={"origin": "http://host.local", "host": "host.local"})
    assert auth.require_same_origin(ok) is None

    bad = _request(headers={"origin": "http://evil.example", "host": "host.local"})
    with pytest.raises(HTTPException) as exc:
        auth.require_same_origin(bad)
    assert exc.value.status_code == status.HTTP_403_FORBIDDEN


def test_no_csrf_headers_at_all_is_allowed():
    """A bare API/curl client sends neither Sec-Fetch-Site nor Origin/Referer."""
    assert auth.require_same_origin(_request(headers={"host": "host.local"})) is None
