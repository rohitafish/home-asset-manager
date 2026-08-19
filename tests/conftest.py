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
"""

import os
import sys
from pathlib import Path

import pytest
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
