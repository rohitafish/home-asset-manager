"""app/asset_children.py has already shipped the same bug twice: a hand-
rolled delete/reassign loop drifting out of sync with the real set of
asset-child tables. test_asset_child_models_completeness below reflects over
every SQLModel table and fails if a new asset_id-FK table is ever added
without also being added to ASSET_CHILD_MODELS -- that's the test this
module exists for; the rest pin delete_asset_cascade/reassign_asset_children
themselves.
"""

from decimal import Decimal

from conftest import make_asset, make_interface
from sqlmodel import SQLModel, select

from app.asset_children import (
    ASSET_CHILD_MODELS,
    delete_asset_cascade,
    reassign_asset_children,
)
from app.models import (
    AiUsage,
    Asset,
    AssetNote,
    AssetService,
    ChangeProposal,
    ChatMessage,
    CIRelationship,
    Finding,
    ProbeResult,
    Severity,
)


def test_asset_child_models_completeness():
    """Every SQLModel table with a plain `asset_id` FK back to asset.id,
    other than Asset itself, the self-referential CIRelationship, and the
    AiUsage spend ledger (all three handled separately by design -- see
    asset_children.py's module docstring), must be in ASSET_CHILD_MODELS."""
    discovered = set()
    for cls in SQLModel.__subclasses__():
        table = getattr(cls, "__table__", None)
        if table is None or cls in (Asset, CIRelationship, AiUsage):
            continue
        column = table.columns.get("asset_id")
        if column is None:
            continue
        targets = {f"{fk.column.table.name}.{fk.column.name}" for fk in column.foreign_keys}
        if "asset.id" in targets:
            discovered.add(cls)

    assert discovered == set(ASSET_CHILD_MODELS)


def _populate_children(session, asset_id):
    session.add(AssetNote(asset_id=asset_id, body="a note"))
    session.add(AssetService(asset_id=asset_id, port=80))
    session.add(ProbeResult(asset_id=asset_id, probe_name="ping", ok=True, summary="alive"))
    session.add(ChatMessage(asset_id=asset_id, role="user", content_json="[]"))
    session.add(ChangeProposal(asset_id=asset_id, kind="add_note", payload_json="{}"))
    session.add(Finding(asset_id=asset_id, severity=Severity.low))
    make_interface(session, asset_id)
    session.commit()


def test_delete_asset_cascade_removes_every_dependent_row(session):
    asset = make_asset(session, hostname="doomed")
    other = make_asset(session, hostname="bystander")
    _populate_children(session, asset.id)

    # Relationship rows referencing the doomed asset from either side must
    # also go, since CIRelationship is self-referential and isn't in
    # ASSET_CHILD_MODELS.
    session.add(CIRelationship(asset_id=asset.id, related_asset_id=other.id, relationship_type="x"))
    session.add(CIRelationship(asset_id=other.id, related_asset_id=asset.id, relationship_type="x"))
    session.commit()

    delete_asset_cascade(session, asset.id)
    session.commit()

    assert session.get(Asset, asset.id) is None
    for model in ASSET_CHILD_MODELS:
        assert session.exec(select(model).where(model.asset_id == asset.id)).all() == []
    assert session.exec(select(CIRelationship)).all() == []
    # The unrelated asset survives untouched.
    assert session.get(Asset, other.id) is not None


def test_delete_asset_cascade_safe_with_no_dependents(session):
    asset = make_asset(session, hostname="lonely")
    delete_asset_cascade(session, asset.id)
    session.commit()
    assert session.get(Asset, asset.id) is None


def test_delete_asset_cascade_detaches_ai_usage_instead_of_deleting_it(session):
    """AiUsage is the AI spend ledger -- deleting an asset must not erase the
    record of money already spent against it, unlike every model in
    ASSET_CHILD_MODELS (which IS meaningless without its asset)."""
    asset = make_asset(session, hostname="doomed")
    usage = AiUsage(asset_id=asset.id, call_site="chat", provider="anthropic", cost_usd=Decimal("0.05"))
    session.add(usage)
    session.commit()

    delete_asset_cascade(session, asset.id)
    session.commit()

    session.refresh(usage)
    assert usage.asset_id is None  # detached, not deleted
    assert usage.cost_usd == Decimal("0.05")  # the spend record survives intact


def test_reassign_asset_children_moves_every_dependent_row(session):
    survivor = make_asset(session, hostname="survivor")
    duplicate = make_asset(session, hostname="duplicate")
    third = make_asset(session, hostname="third")
    _populate_children(session, duplicate.id)

    # A relationship from the duplicate to some unrelated third asset must
    # be repointed to reference the survivor instead.
    session.add(CIRelationship(asset_id=duplicate.id, related_asset_id=third.id, relationship_type="x"))
    session.add(CIRelationship(asset_id=third.id, related_asset_id=duplicate.id, relationship_type="x"))
    session.commit()

    reassign_asset_children(session, survivor_id=survivor.id, duplicate_id=duplicate.id)
    session.commit()

    for model in ASSET_CHILD_MODELS:
        assert session.exec(select(model).where(model.asset_id == duplicate.id)).all() == []
        moved = session.exec(select(model).where(model.asset_id == survivor.id)).all()
        assert len(moved) == 1

    rels = session.exec(select(CIRelationship)).all()
    assert len(rels) == 2
    for rel in rels:
        assert duplicate.id not in (rel.asset_id, rel.related_asset_id)
        assert survivor.id in (rel.asset_id, rel.related_asset_id)
        assert third.id in (rel.asset_id, rel.related_asset_id)

    # The duplicate asset row itself is left for the caller to delete.
    assert session.get(Asset, duplicate.id) is not None


def test_reassign_asset_children_drops_self_reference_and_dupes(session):
    survivor = make_asset(session, hostname="survivor")
    duplicate = make_asset(session, hostname="duplicate")

    # survivor was already linked to duplicate -- after repointing,
    # duplicate's side becomes a self-reference (survivor -> survivor) that
    # must be dropped, not kept as a relationship to itself.
    session.add(CIRelationship(asset_id=survivor.id, related_asset_id=duplicate.id, relationship_type="same_physical_device"))
    session.add(CIRelationship(asset_id=duplicate.id, related_asset_id=survivor.id, relationship_type="same_physical_device"))
    session.commit()

    reassign_asset_children(session, survivor_id=survivor.id, duplicate_id=duplicate.id)
    session.commit()

    rels = session.exec(select(CIRelationship)).all()
    assert rels == []


def test_reassign_asset_children_noop_when_ids_match(session):
    asset = make_asset(session, hostname="solo")
    _populate_children(session, asset.id)
    reassign_asset_children(session, survivor_id=asset.id, duplicate_id=asset.id)
    session.commit()
    for model in ASSET_CHILD_MODELS:
        assert len(session.exec(select(model).where(model.asset_id == asset.id)).all()) == 1


def test_reassign_asset_children_moves_ai_usage_to_survivor(session):
    """Unlike delete (which detaches to NULL), a merge reassigns AiUsage rows
    to the survivor same as every other child model -- no spend is lost by
    that, since the survivor is the asset going forward."""
    survivor = make_asset(session, hostname="survivor")
    duplicate = make_asset(session, hostname="duplicate")
    usage = AiUsage(asset_id=duplicate.id, call_site="chat", provider="anthropic", cost_usd=Decimal("0.10"))
    session.add(usage)
    session.commit()

    reassign_asset_children(session, survivor_id=survivor.id, duplicate_id=duplicate.id)
    session.commit()

    session.refresh(usage)
    assert usage.asset_id == survivor.id
