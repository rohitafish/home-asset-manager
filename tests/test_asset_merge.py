"""Tests for app/asset_merge.py.

Ten statements, but they run at the moment two inventory records are
irreversibly collapsed into one -- a human has just confirmed that two rows
are the same physical device, and the losing row is about to be deleted.
Nothing called merge_asset_into directly before this module.

The delegation to reassign_asset_children is the whole safety property:
there is no ON DELETE CASCADE on the child foreign keys, so a child model
left behind is either a dangling row or a ForeignKeyViolation at delete
time. conftest turns SQLite's foreign_keys pragma on, which is what makes
that failure reproducible here rather than only against Postgres in
production.
"""

from datetime import datetime

from conftest import make_asset, make_interface
from sqlmodel import select

from app.asset_merge import merge_asset_into
from app.models import (
    Asset,
    AssetNote,
    AssetService,
    CIRelationship,
    Exposure,
    Finding,
    FindingStatus,
    Severity,
)


def _with_children(session, hostname):
    asset = make_asset(session, hostname=hostname)
    make_interface(session, asset.id, ip=f"10.0.0.{asset.id}", mac=f"00:00:00:00:00:0{asset.id}")
    session.add(AssetService(asset_id=asset.id, port=443, protocol="tcp"))
    session.add(
        Finding(
            asset_id=asset.id,
            severity=Severity.medium,
            exposure=Exposure.internal,
            detected_date=datetime(2026, 7, 1),
            status=FindingStatus.open,
        )
    )
    session.add(
        AssetNote(
            asset_id=asset.id, created_at=datetime(2026, 7, 1), author="test", body=hostname
        )
    )
    session.commit()
    return asset


def test_merge_moves_the_append_only_children_to_the_survivor(session):
    """Findings and notes are history: a merge combines two devices' records,
    it never collapses them, so both rows survive under the survivor."""
    survivor = _with_children(session, "survivor")
    duplicate = _with_children(session, "duplicate")

    merge_asset_into(session, survivor_id=survivor.id, duplicate_id=duplicate.id)
    session.commit()

    assert session.get(Asset, duplicate.id) is None
    for model in (Finding, AssetNote):
        moved = session.exec(select(model).where(model.asset_id == survivor.id)).all()
        assert len(moved) == 2, f"{model.__name__} rows did not all move to the survivor"


def test_merge_collapses_a_service_the_survivor_already_has(session):
    """Interfaces and services are the two children with a real "duplicate
    row" concept -- the same listening port on what turned out to be one
    device is one service, not two. Getting this wrong is not hypothetical:
    per app/models.py, an earlier merge-path bug left 13 duplicate service
    groups in production before the unique constraint existed."""
    survivor = _with_children(session, "survivor")
    duplicate = _with_children(session, "duplicate")  # also 443/tcp

    merge_asset_into(session, survivor_id=survivor.id, duplicate_id=duplicate.id)
    session.commit()

    services = session.exec(select(AssetService)).all()
    assert [(s.asset_id, s.port) for s in services] == [(survivor.id, 443)]


def test_merge_keeps_a_service_the_survivor_does_not_have(session):
    """The other half: de-duplication must not become "drop the duplicate's
    services"."""
    survivor = _with_children(session, "survivor")
    duplicate = make_asset(session, hostname="duplicate")
    session.add(AssetService(asset_id=duplicate.id, port=8080, protocol="tcp"))
    session.commit()

    merge_asset_into(session, survivor_id=survivor.id, duplicate_id=duplicate.id)
    session.commit()

    ports = sorted(s.port for s in session.exec(select(AssetService)).all())
    assert ports == [443, 8080]


def test_merge_deletes_the_duplicate_row(session):
    survivor = make_asset(session, hostname="keeper")
    duplicate = make_asset(session, hostname="goner")

    merge_asset_into(session, survivor_id=survivor.id, duplicate_id=duplicate.id)
    session.commit()

    remaining = session.exec(select(Asset)).all()
    assert [a.id for a in remaining] == [survivor.id]


def test_merge_leaves_no_dangling_foreign_keys(session):
    """The failure this module's docstring is about: if a child model were
    missed, the commit below would raise rather than pass, because the
    pragma is on."""
    survivor = _with_children(session, "survivor")
    duplicate = _with_children(session, "duplicate")

    merge_asset_into(session, survivor_id=survivor.id, duplicate_id=duplicate.id)
    session.commit()  # would raise IntegrityError on an orphaned child

    orphans = [
        row
        for model in (AssetService, Finding, AssetNote)
        for row in session.exec(select(model)).all()
        if row.asset_id != survivor.id
    ]
    assert orphans == []


def test_merge_moves_ci_relationships(session):
    """Relationships point at assets from both ends, so they're the easiest
    child to forget."""
    survivor = make_asset(session, hostname="survivor")
    duplicate = make_asset(session, hostname="duplicate")
    other = make_asset(session, hostname="bystander")
    session.add(
        CIRelationship(asset_id=duplicate.id, related_asset_id=other.id, relationship_type="connected_via_ap")
    )
    session.commit()

    merge_asset_into(session, survivor_id=survivor.id, duplicate_id=duplicate.id)
    session.commit()

    rels = session.exec(select(CIRelationship)).all()
    assert [r.asset_id for r in rels] == [survivor.id]


def test_merging_an_asset_into_itself_is_a_no_op(session):
    """The guard that stops the UI's radio button from deleting the very row
    it selected as the survivor. Without it, reassign_asset_children would
    move the children onto the same id and the delete would then take both
    the asset and everything hanging off it."""
    asset = _with_children(session, "self")

    merge_asset_into(session, survivor_id=asset.id, duplicate_id=asset.id)
    session.commit()

    assert session.get(Asset, asset.id) is not None
    assert len(session.exec(select(AssetNote)).all()) == 1
    assert len(session.exec(select(Finding)).all()) == 1


def test_merge_tolerates_an_already_deleted_duplicate(session):
    """Two tabs open on /assets/duplicates, both submitting -- the second
    request finds the duplicate already gone. It must not raise."""
    survivor = make_asset(session, hostname="survivor")

    merge_asset_into(session, survivor_id=survivor.id, duplicate_id=9999)
    session.commit()

    assert session.get(Asset, survivor.id) is not None
