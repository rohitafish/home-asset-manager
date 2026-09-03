"""Verifies that the auth guards are actually *attached* to every route.

tests/test_auth.py already covers require_admin and require_same_origin
thoroughly -- but as plain callables. That proves the guards work; it says
nothing about whether they are wired to anything. Deleting the
`dependencies=[Depends(require_admin), Depends(require_same_origin)]`
argument from app/routers/dashboard.py's APIRouter would serve this app's
entire dashboard -- every asset, price, location and finding -- unauthenticated
to any device on the LAN, and before this module every other test in the suite
would still have passed.

Two independent layers here on purpose:

  * The structural check reads the routers' declared dependencies. It fails
    with a clear message naming the router that lost its guard.
  * The behavioural check drives real requests through the real app and
    asserts the response an unauthorized LAN device would actually get. It
    is the one that stays honest if FastAPI ever changes how router-level
    dependencies are merged into a route, and it is the reason this module
    walks *every* route rather than a representative sample: a route added
    to some future third router, or one that somehow bypasses the router's
    dependencies, is caught by enumeration and cannot be caught by a
    hand-written list.
"""

import pkgutil

import pytest
from fastapi.routing import APIRoute

import app.routers
from app import auth
from app.routers import assets, dashboard

# Every router that must sit behind both guards. Not hardcoded as the whole
# story -- test_every_router_module_is_listed_here below fails if a new
# app/routers/*.py appears that isn't in this list, so adding a router
# without auth can't slip through by simply not being mentioned.
GUARDED_ROUTERS = {
    "app/routers/dashboard.py": dashboard.router,
    "app/routers/assets.py": assets.router,
}

# /health is deliberately unauthenticated: scripts/redeploy.sh curls it as
# its final deploy gate and launchd uses it as a liveness check, neither of
# which can present credentials. It exposes only {"status": "ok"} plus backup
# freshness -- no inventory data. It lives on the app object directly, not on
# either guarded router, so it never appears in the walks below.
PUBLIC_PATHS = {"/health"}


def _routes(router):
    return [r for r in router.routes if isinstance(r, APIRoute)]


def _concrete_path(route: APIRoute) -> str:
    """Fill path params with a plausible id. Nothing here should reach a
    handler -- the guards reject first -- so the id never has to exist."""
    path = route.path
    for name in route.param_convertors:
        path = path.replace("{" + name + "}", "1")
    return path


def _walk_all_guarded_routes():
    for label, router in GUARDED_ROUTERS.items():
        for route in _routes(router):
            for method in sorted(route.methods - {"HEAD", "OPTIONS"}):
                yield label, method, _concrete_path(route)


ALL_GUARDED_ROUTES = list(_walk_all_guarded_routes())


def test_the_walk_actually_found_the_routes():
    """A guard on the guards: if _routes() ever returns nothing (a FastAPI
    refactor, a bad import), every parametrised test below would vacuously
    pass with zero cases. Pin the count's order of magnitude instead."""
    assert len(ALL_GUARDED_ROUTES) > 50


@pytest.mark.parametrize("label", sorted(GUARDED_ROUTERS))
def test_router_declares_both_auth_guards(label):
    declared = {d.dependency for d in GUARDED_ROUTERS[label].dependencies}
    assert auth.require_admin in declared, (
        f"{label}'s router no longer requires authentication -- every route on "
        "it is served to any unauthenticated client on the LAN."
    )
    assert auth.require_same_origin in declared, (
        f"{label}'s router no longer has its CSRF guard -- HTTP Basic has no "
        "SameSite cookie to fall back on, so a page in another tab can drive "
        "its state-changing routes."
    )


def test_every_router_module_is_listed_here():
    """GUARDED_ROUTERS is hand-written, so a new app/routers/*.py added
    without auth would otherwise be invisible to this module -- untested and
    unprotected. Fail until it's added above (or documented as public)."""
    found = {
        f"app/routers/{m.name}.py"
        for m in pkgutil.iter_modules(app.routers.__path__)
        if not m.name.startswith("_")
    }
    assert found == set(GUARDED_ROUTERS), (
        "app/routers/ gained or lost a module. Add it to GUARDED_ROUTERS (and "
        "give it both auth dependencies) rather than deleting this assertion."
    )


@pytest.mark.parametrize(("label", "method", "path"), ALL_GUARDED_ROUTES)
def test_route_rejects_an_unauthenticated_request(client, label, method, path):
    """The end-to-end proof: no credentials, no data, on every single route."""
    auth._failures.clear()  # don't let this walk trip the brute-force throttle
    response = client.request(method, path)
    assert response.status_code == 401, (
        f"{method} {path} ({label}) answered {response.status_code} without "
        "credentials -- it should be 401."
    )


def test_health_is_reachable_without_credentials(client, engine, monkeypatch):
    """The other half of the same invariant: the deploy gate must stay open,
    or scripts/redeploy.sh's health check fails every deploy.

    /health opens app.db.engine directly instead of taking get_session as a
    dependency (it has to answer even when the pool is sick), so conftest's
    dependency override doesn't reach it -- point the module's engine at the
    in-memory one for the duration.
    """
    monkeypatch.setattr("app.main.engine", engine)

    response = client.get("/health")

    assert response.status_code == 200, "the unauthenticated deploy gate is closed"
    assert response.json()["status"] == "ok"


def test_wrong_password_is_rejected_on_a_real_route(client):
    """Distinguishes "the guard is attached" from "the guard says yes to
    anything" -- a require_admin that always passed would still 401 above
    only if it were absent, not if it were broken open."""
    response = client.get("/assets", auth=("admin", "not-the-password"))
    assert response.status_code == 401


def test_cross_origin_post_is_rejected_even_with_valid_credentials(admin_client):
    """require_same_origin, end to end. A browser with saved Basic
    credentials will replay them on a cross-site POST; Sec-Fetch-Site is what
    stops that, and this asserts it's applied at the router, not just
    unit-tested in isolation."""
    response = admin_client.post(
        "/assets/1/delete", headers={"sec-fetch-site": "cross-site"}
    )
    assert response.status_code == 403


def test_authenticated_same_origin_request_is_allowed_through(admin_client):
    """The guards must not be so strict that the app's own pages break --
    this is the counterweight to the 401/403 assertions above."""
    response = admin_client.get("/assets")
    assert response.status_code == 200


def test_no_route_runs_anything_as_root():
    """/discovery/run/nmap-privileged used to run `sudo nmap` from a web
    request, backed by a NOPASSWD sudoers rule on /opt/homebrew/bin/nmap. That
    binary and its directory are owned by the app user on a Homebrew install,
    so the rule was a one-line root escalation for anything running as that
    user -- the app included. Privileged scans are CLI-only now (a terminal
    where sudo can prompt). Pin the absence so a convenience button can't
    quietly bring the rule back."""
    for label, method, path in ALL_GUARDED_ROUTES:
        assert "privileged" not in path and "sudo" not in path, (
            f"{method} {path} ({label}) looks like a privileged-execution route"
        )


def test_unknown_host_header_is_rejected_before_auth(app_with_session):
    """TrustedHostMiddleware sits outside the routers. A request for a Host
    the app doesn't serve (DNS rebinding: a hostile page's domain re-pointed
    at the Mini's IP) is refused with a 400 -- no 401, no WWW-Authenticate
    challenge, no route code, no DB. localhost/127.0.0.1 are always allowed;
    the proxy's name comes from APP_ALLOWED_HOSTS (see app/main.py)."""
    from fastapi.testclient import TestClient

    evil = TestClient(app_with_session, base_url="http://evil.example")
    response = evil.get("/assets")
    assert response.status_code == 400
    assert "WWW-Authenticate" not in response.headers

    loopback = TestClient(app_with_session, base_url="http://127.0.0.1")
    assert loopback.get("/health").status_code == 200
