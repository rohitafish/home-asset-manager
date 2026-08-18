"""Single source of truth for "what has an asset_id foreign key pointing at
Asset". There's no ON DELETE CASCADE on any of these FKs (see the initial
schema migration), so deleting or merging an asset requires manually cleaning
up every dependent row first -- and that cleanup used to be hand-written and
duplicated across app/routers/dashboard.py, app/routers/assets.py, and
app/asset_merge.py. Two past commits were both "Fix internal server error
when deleting an asset with dependent rows" because one of those three copies
drifted out of sync with the model list. Adding a new asset-child table now
means adding it to ASSET_CHILD_MODELS once, here.

CIRelationship and SameDeviceDismissal are handled separately (not in this
list) because both are self-referential asset<->asset rows -- CIRelationship
via asset_id/related_asset_id, SameDeviceDismissal via asset_id_a/asset_id_b.
Both are deleted outright on delete AND on merge: unlike a regular child row,
repointing one side onto the merge survivor could produce a self-pair or
collide with SameDeviceDismissal's (asset_id_a, asset_id_b) unique
constraint, and once one side of a dismissal is gone the "these two are
different devices" judgement no longer refers to anything -- there's a fresh
survivor+other pair for find_same_device_candidates() to score from scratch.

AiUsage is also handled separately (not in this list): it's the AI spend
ledger (see app/assistant.py's _log_usage/budget_block_reason), and deleting
an asset must never erase the record of money already spent -- that's a
different meaning than "this row is meaningless without its asset", which is
what every model in ASSET_CHILD_MODELS assumes when delete_asset_cascade
deletes it outright. So on delete, an AiUsage row's asset_id is detached to
NULL instead (the column is nullable for exactly this reason); on merge, it's
reassigned to the survivor like every other child model, since no spend is
lost by that.
"""

from sqlmodel import Session, select

from app.models import (
    AiUsage,
    Asset,
    AssetInterface,
    AssetNote,
    AssetService,
    ChangeProposal,
    ChatMessage,
    CIRelationship,
    Finding,
    ProbeResult,
    SameDeviceDismissal,
)

# Every model with a plain `asset_id` FK back to Asset. Extend this list --
# don't hand-roll another delete/reassign loop -- whenever a new asset-child
# table is introduced.
ASSET_CHILD_MODELS = [
    AssetInterface, AssetService, Finding, AssetNote, ProbeResult, ChatMessage, ChangeProposal,
]


def delete_asset_cascade(session: Session, asset_id: int) -> None:
    """Deletes an asset and every row that references it, in the correct
    order to satisfy foreign key constraints. Safe to call on an asset with
    no dependents at all."""
    for model in ASSET_CHILD_MODELS:
        for row in session.exec(select(model).where(model.asset_id == asset_id)).all():
            session.delete(row)

    for usage in session.exec(select(AiUsage).where(AiUsage.asset_id == asset_id)).all():
        usage.asset_id = None
        session.add(usage)

    for rel in session.exec(
        select(CIRelationship).where(
            (CIRelationship.asset_id == asset_id)
            | (CIRelationship.related_asset_id == asset_id)
        )
    ).all():
        session.delete(rel)

    for dismissal in session.exec(
        select(SameDeviceDismissal).where(
            (SameDeviceDismissal.asset_id_a == asset_id)
            | (SameDeviceDismissal.asset_id_b == asset_id)
        )
    ).all():
        session.delete(dismissal)

    session.flush()
    asset = session.get(Asset, asset_id)
    if asset:
        session.delete(asset)


def reassign_asset_children(session: Session, survivor_id: int, duplicate_id: int) -> None:
    """Re-points every dependent row of `duplicate_id` onto `survivor_id`,
    without deleting the duplicate asset itself -- used by the duplicate-merge
    flow, which always lets the caller delete the now-empty duplicate
    afterwards (see app/asset_merge.py)."""
    if survivor_id == duplicate_id:
        return

    for model in ASSET_CHILD_MODELS:
        for row in session.exec(select(model).where(model.asset_id == duplicate_id)).all():
            row.asset_id = survivor_id
            session.add(row)

    for usage in session.exec(select(AiUsage).where(AiUsage.asset_id == duplicate_id)).all():
        usage.asset_id = survivor_id
        session.add(usage)

    for rel in session.exec(
        select(CIRelationship).where(
            (CIRelationship.asset_id == duplicate_id)
            | (CIRelationship.related_asset_id == duplicate_id)
        )
    ).all():
        if rel.asset_id == duplicate_id:
            rel.asset_id = survivor_id
        if rel.related_asset_id == duplicate_id:
            rel.related_asset_id = survivor_id
        session.add(rel)

    # Deleted, not repointed, unlike every model above: repointing could
    # produce a self-pair or collide with the (asset_id_a, asset_id_b) unique
    # constraint, and once one side of a dismissal is merged away the "these
    # two are different devices" judgement no longer refers to anything --
    # find_same_device_candidates() will score the survivor+other pair fresh.
    for dismissal in session.exec(
        select(SameDeviceDismissal).where(
            (SameDeviceDismissal.asset_id_a == duplicate_id)
            | (SameDeviceDismissal.asset_id_b == duplicate_id)
        )
    ).all():
        session.delete(dismissal)

    session.flush()

    # Repointing can produce a relationship that now points an asset at
    # itself (e.g. the survivor was already linked to the duplicate as
    # "same_physical_device") or a duplicate of a relationship the survivor
    # already had. Both are cleanup artifacts of the merge, not real data.
    seen = set()
    for rel in session.exec(
        select(CIRelationship).where(CIRelationship.asset_id == survivor_id)
    ).all():
        key = (rel.asset_id, rel.related_asset_id, rel.relationship_type)
        if rel.asset_id == rel.related_asset_id or key in seen:
            session.delete(rel)
        else:
            seen.add(key)
