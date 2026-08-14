import os

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from sqlmodel import Session, select

from app.backup_status import backup_status
from app.clock import utcnow_naive
from app.db import engine
from app.logging_config import configure_logging
from app.models import DiscoveryRun
from app.routers import assets, dashboard

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
