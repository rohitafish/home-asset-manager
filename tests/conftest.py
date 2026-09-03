"""Shared fixtures for the test suite.

Runs against an in-memory SQLite database, not the real Postgres instance --
these tests exercise pure application logic (cascade deletes, correlation
scoring, discovery normalization), not Postgres-specific behaviour, so a
throwaway in-memory DB per test keeps the suite fast and independent of
Docker/Colima being up. StaticPool keeps the single in-memory connection
alive for the lifetime of each test's engine (SQLite's default pooling would
otherwise hand out a fresh, empty in-memory database per connection).

Several modules under test resolve paths relative to the process's cwd (see
app/routers/dashboard.py's _STATIC_VERSION, app/backup_status.py's
_MARKER_PATH) on the same assumption the running app makes: it's always
started from the repo root. Chdir there once, at collection time, so the
suite behaves the same regardless of where `pytest` is invoked from.

Route-level testing (the `client`/`admin_client` fixtures below) goes through
the real app object from app/main.py with only get_session swapped out. The
suite used to have no TestClient at all and unit-tested every dependency as a
plain callable instead; that left the *wiring* unverified -- auth dependencies
attached to a router, Jinja templates actually rendering, CSV escaping applied
at each call site -- none of which a callable-level test can see. See
tests/test_route_security.py for the specific hole this closes.
"""

import os
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
os.chdir(REPO_ROOT)

import app.models  # noqa: E402  -- import registers every table on SQLModel.metadata
from app.models import (  # noqa: E402
    Asset,
    AssetInterface,
    AssetType,
    Criticality,
    LifecycleStatus,
)


@pytest.fixture()
def engine():
    eng = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    # SQLite does not enforce foreign keys by default -- without this, a
    # bug that leaves a dangling FK (e.g. a second FK column a cascade
    # helper forgot about) passes every test silently and only surfaces as
    # a real ForeignKeyViolation against Postgres in production. See
    # app/asset_children.py's module docstring for the recurring bug class
    # this exists to catch.
    @event.listens_for(eng, "connect")
    def _enable_sqlite_fk(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    SQLModel.metadata.create_all(eng)
    return eng


@pytest.fixture()
def session(engine):
    with Session(engine) as sess:
        yield sess


ADMIN_USER = "admin"
ADMIN_PASSWORD = "test-admin-password"


@pytest.fixture()
def app_with_session(session, monkeypatch):
    """The real FastAPI app with only the DB dependency redirected at the
    in-memory SQLite session.

    Deliberately does NOT enter TestClient's context manager anywhere below:
    that would run app/main.py's startup hooks, and
    fail_orphaned_discovery_runs opens app.db.engine -- the real Postgres --
    which isn't up in CI and isn't what these tests are about. Those hooks
    are tested directly as callables in tests/test_main.py instead.

    A configured admin password is set for every route test because
    require_admin reads it from the environment on each call; without it
    even the correct password would fail the `bool(expected_password)`
    guard in app/auth.py.
    """
    from app import auth
    from app.db import get_session
    from app.main import app

    monkeypatch.setenv("APP_ADMIN_USER", ADMIN_USER)
    monkeypatch.setenv("APP_ADMIN_PASSWORD", ADMIN_PASSWORD)

    # require_admin's brute-force throttle is process-global module state
    # keyed by client IP, and every TestClient request arrives from the same
    # "testclient" host. Without this reset, a module that makes more than
    # _MAX_FAILURES unauthenticated requests (test_route_security walks every
    # route) would start getting 429s partway through, and would leak that
    # lockout into whatever test ran next.
    auth._failures.clear()

    app.dependency_overrides[get_session] = lambda: session
    yield app
    app.dependency_overrides.clear()
    auth._failures.clear()


@pytest.fixture()
def client(app_with_session):
    """Unauthenticated client -- sends no credentials, so it sees exactly
    what an unauthorized LAN device would."""
    # base_url: TrustedHostMiddleware (app/main.py) only answers for
    # localhost/127.0.0.1 plus APP_ALLOWED_HOSTS; TestClient's default Host
    # is "testserver", which the real app would (correctly) refuse.
    return TestClient(app_with_session, base_url="http://localhost")


@pytest.fixture()
def admin_client(app_with_session):
    """Authenticated, same-origin client for exercising route behaviour.

    Uses real Basic credentials and a real Sec-Fetch-Site header rather than
    overriding require_admin/require_same_origin, so these tests keep going
    through the actual auth stack instead of around it -- a route test that
    stubbed the guards out couldn't tell a wired-up router from an
    unprotected one.
    """
    # Starlette's TestClient.__init__ takes headers but not auth, so the
    # credentials are set on the underlying httpx.Client afterwards.
    test_client = TestClient(
        app_with_session,
        headers={"sec-fetch-site": "same-origin"},
        base_url="http://localhost",
    )
    test_client.auth = (ADMIN_USER, ADMIN_PASSWORD)
    return test_client


def make_asset(session: Session, **overrides) -> Asset:
    defaults = dict(
        asset_type=AssetType.end_user_device,
        criticality=Criticality.medium,
        lifecycle_status=LifecycleStatus.discovered,
    )
    defaults.update(overrides)
    asset = Asset(**defaults)
    session.add(asset)
    session.commit()
    session.refresh(asset)
    return asset


def make_interface(session: Session, asset_id: int, **overrides) -> AssetInterface:
    defaults = dict(asset_id=asset_id)
    defaults.update(overrides)
    iface = AssetInterface(**defaults)
    session.add(iface)
    session.commit()
    session.refresh(iface)
    return iface
