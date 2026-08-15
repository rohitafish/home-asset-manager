import os

from fastapi import FastAPI, Response
from fastapi.staticfiles import StaticFiles
from sqlmodel import Session, select
from starlette.middleware.base import BaseHTTPMiddleware

from app.backup_status import backup_status
from app.clock import utcnow_naive
from app.db import engine
from app.logging_config import configure_logging
from app.models import DiscoveryRun
from app.routers import assets, dashboard

# Response headers applied to every request, not just the require_admin
# routers -- /health is unauthenticated by design (redeploy.sh's deploy gate,
# launchd), and these are cheap insurance regardless of auth state.
#
# script-src/style-src keep 'unsafe-inline': the templates have a handful of
# inline onclick/onchange/onsubmit handlers (form auto-submit on filter
# change, delete/clear confirm() dialogs) and no external stylesheet. Moving
# those into app/static/ would allow dropping 'unsafe-inline' from script-src,
# but there's no untrusted-HTML rendering path for it to actually defend
# against here -- the only |safe template output is the app's own README
# (see app/readme_render.py's SECURITY INVARIANT comment), and htmx is
# self-hosted (app/static/htmx.min.js), not pulled from a CDN. What this CSP
# does earn regardless of 'unsafe-inline': no *externally hosted* script can
# load, no framing, no <base> tag hijack, no form POST to another origin.
#
# frame-ancestors 'none' supersedes X-Frame-Options in every browser this
# dashboard is used from, so only one of the two is set.
_SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; "
        "frame-ancestors 'none'; "
        "base-uri 'none'; "
        "form-action 'self'; "
        "object-src 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    # NOT "no-referrer": per the Fetch spec (confirmed against real traffic --
    # see the incident this comment replaces below), a browser honoring
    # "no-referrer" sends Origin: null on an HTML form POST, even a same-
    # origin one -- indistinguishable from the literal signature a sandboxed-
    # iframe/data: URI CSRF bypass produces, so app/auth.py's
    # require_same_origin correctly rejects it. That broke every plain form
    # submission in this app, for every standards-compliant browser,
    # regardless of extensions/OS security software -- traced via a live
    # user report, reproduced against the exact captured header set
    # (Origin: null, no Referer, no Sec-Fetch-Site) both against a local
    # instance and confirmed byte-for-byte on the deployed one via temporary
    # server-side logging. "same-origin" doesn't null Origin for a same-
    # origin request (there's no cross-origin/downgrade case here to omit
    # it for), so the CSRF guard keeps working, and it's still at least as
    # private as leaving this header unset -- the browser default
    # ("strict-origin-when-cross-origin") would send the app's origin to any
    # external link clicked from inside it (e.g. from the in-app README
    # view); "same-origin" sends nothing at all cross-origin instead.
    "Referrer-Policy": "same-origin",
}


def _apply_security_headers(response: Response) -> Response:
    """Split out from the middleware below so it's unit-testable against a
    plain Response, the same way app/auth.py's require_admin/
    require_same_origin are tested as callables rather than through a live
    app (see tests/test_auth.py's docstring -- this app has no route-level
    TestClient)."""
    for name, value in _SECURITY_HEADERS.items():
        response.headers[name] = value
    return response


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        response = await call_next(request)
        return _apply_security_headers(response)


# docs/redoc/openapi are disabled: they sit outside the require_admin routers
# and would otherwise hand the full route inventory to any unauthenticated LAN
# client. This is an internal tool with no third-party API consumers, so nothing
# needs the schema.
app = FastAPI(
    title="Home Asset & Vulnerability Management",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)
app.add_middleware(SecurityHeadersMiddleware)
app.mount("/static", StaticFiles(directory="app/static"), name="static")
app.include_router(assets.router)
app.include_router(dashboard.router)


@app.on_event("startup")
def init_logging():
    """Registered first so the startup hooks below (and everything served
    afterwards) log through the configured handlers rather than being dropped
    by logging.lastResort. Runs after uvicorn has applied its own logging
    config, so uvicorn's loggers are re-pointed at root rather than clobbered
    -- see app/logging_config.py."""
    configure_logging()


@app.on_event("startup")
def require_admin_password_configured():
    """The whole dashboard and API sit behind HTTP Basic auth
    (app/auth.py's require_admin) as the only thing standing between a LAN
    device and this app -- and secrets.compare_digest("", "") is True, so an
    empty APP_ADMIN_PASSWORD isn't "no auth", it's "any password works".
    `cp .env.example .env` (the documented setup step) also leaves the
    literal placeholder `change-me` if the next edit gets skipped. Refuse to
    boot rather than silently serve the dashboard wide open -- see README's
    "One-time setup" and "Installing on a new Mac"."""
    password = os.environ.get("APP_ADMIN_PASSWORD", "")
    if not password or password == "change-me":
        raise RuntimeError(
            "APP_ADMIN_PASSWORD is not set (or is still the .env.example "
            "placeholder 'change-me') -- refusing to start with the whole "
            "dashboard unauthenticated. Set a real password in .env and "
            "restart."
        )


@app.on_event("startup")
def fail_orphaned_discovery_runs():
    """A restart (redeploy, crash, reboot) abandons any in-flight discovery
    run mid-process, leaving its DiscoveryRun row stuck at status="running"
    forever. Clean those up on every boot rather than letting them linger."""
    now = utcnow_naive()
    with Session(engine) as session:
        orphaned = session.exec(
            select(DiscoveryRun).where(DiscoveryRun.status == "running")
        ).all()
        for run in orphaned:
            run.status = "failed"
            run.finished_at = now
            run.summary = "Interrupted by app restart (orphaned on startup)"
            session.add(run)
        if orphaned:
            session.commit()


@app.get("/health")
def health():
    with Session(engine) as session:
        session.exec(select(1)).one()
    # Backup freshness is informational only -- it never changes the status
    # code or the "status" field. redeploy.sh curls this endpoint as its
    # final deploy gate, and conflating "the app is serving requests" with
    # "last night's backup succeeded" would make an unrelated problem look
    # like a broken deploy.
    return {"status": "ok", **backup_status()}
