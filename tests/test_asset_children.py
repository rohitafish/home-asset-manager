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
from app.correlate import dismiss_same_device_candidate
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
    SameDeviceDismissal,
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


def test_delete_asset_cascade_removes_same_device_dismissals(session):
    """SameDeviceDismissal is self-referential like CIRelationship (asset_id_a
    / asset_id_b instead of a plain asset_id), so it isn't in
    ASSET_CHILD_MODELS and needs its own cleanup -- pin that it happens."""
    asset = make_asset(session, hostname="doomed")
    other = make_asset(session, hostname="bystander")
    dismiss_same_device_candidate(session, asset.id, other.id)
    session.commit()

    delete_asset_cascade(session, asset.id)
    session.commit()

    assert session.exec(select(SameDeviceDismissal)).all() == []
    assert session.get(Asset, other.id) is not None


def test_delete_asset_cascade_detaches_origin_asset_id_on_cross_asset_proposal(session):
    """ChangeProposal has a SECOND FK to Asset (origin_asset_id -- which chat
    a proposal was drafted from, distinct from asset_id -- the change's
    target -- for a cross-asset proposal, e.g. one invoice analysed on asset
    A proposing a field on asset B). Deleting the origin asset must not
    delete the proposal (its target is still alive and the change record is
    still real) or leave a dangling FK -- it must null origin_asset_id.
    Regression test for a real ForeignKeyViolation on delete; only catchable
    now that conftest.py's engine enables PRAGMA foreign_keys=ON."""
    origin = make_asset(session, hostname="origin-of-the-chat")
    target = make_asset(session, hostname="target-of-the-change")
    proposal = ChangeProposal(
        asset_id=target.id, origin_asset_id=origin.id, kind="set_field", payload_json="{}"
    )
    session.add(proposal)
    session.commit()

    delete_asset_cascade(session, origin.id)
    session.commit()  # must not raise ForeignKeyViolation

    assert session.get(Asset, target.id) is not None  # target survives
    session.refresh(proposal)
    assert proposal.origin_asset_id is None  # dangling reference detached, not the row deleted


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


def test_reassign_asset_children_deletes_same_device_dismissals(session):
    """Unlike every model in ASSET_CHILD_MODELS, a dismissal referencing the
    merged-away duplicate is deleted, not repointed onto the survivor --
    repointing could collide with the unique constraint or produce a
    self-pair, and the "these are different devices" judgement no longer
    refers to anything once one side is gone."""
    survivor = make_asset(session, hostname="survivor")
    duplicate = make_asset(session, hostname="duplicate")
    other = make_asset(session, hostname="other")
    dismiss_same_device_candidate(session, duplicate.id, other.id)
    session.commit()

    reassign_asset_children(session, survivor_id=survivor.id, duplicate_id=duplicate.id)
    session.commit()

    assert session.exec(select(SameDeviceDismissal)).all() == []


def test_reassign_asset_children_repoints_origin_asset_id_on_cross_asset_proposal(session):
    """Mirror of the delete-side test: a proposal whose target survived the
    merge on a DIFFERENT asset, but was drafted from the duplicate's chat,
    must have origin_asset_id repointed to the survivor -- not left dangling
    (the general ASSET_CHILD_MODELS loop only repoints asset_id, and this
    proposal's asset_id was never the duplicate, so that loop never touches
    it)."""
    survivor = make_asset(session, hostname="survivor")
    duplicate = make_asset(session, hostname="duplicate")
    target = make_asset(session, hostname="unrelated-target")
    proposal = ChangeProposal(
        asset_id=target.id, origin_asset_id=duplicate.id, kind="set_field", payload_json="{}"
    )
    session.add(proposal)
    session.commit()

    reassign_asset_children(session, survivor_id=survivor.id, duplicate_id=duplicate.id)
    session.commit()

    session.refresh(proposal)
    assert proposal.origin_asset_id == survivor.id
    assert proposal.asset_id == target.id  # target FK untouched -- it was never the duplicate


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
