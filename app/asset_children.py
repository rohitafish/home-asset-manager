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

ChangeProposal is a THIRD shape again: it's in ASSET_CHILD_MODELS (so the
generic loop below handles its `asset_id` -- the change's target), but it
also carries a second FK, `origin_asset_id` (which asset's chat the proposal
was drafted from -- differs from asset_id for a cross-asset proposal, e.g.
one invoice analysed on asset A proposing a field on asset B). Unlike
CIRelationship/SameDeviceDismissal, a proposal whose *origin* asset is gone
but whose *target* asset is still alive is still a meaningful record, so
it's detached (origin_asset_id -> NULL, the column is nullable) rather than
deleted on delete, and repointed rather than dropped on merge. This was
missed for a long time -- both FKs point at the same table, but only one of
them is named asset_id, and the generic loop only ever looks at that one
column name. If a future model has more than one FK to Asset, it needs the
same explicit treatment; don't assume `model.asset_id` is the only column
that matters.

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

    # ChangeProposal's second FK -- see the module docstring. The loop above
    # already deleted any proposal whose *target* (asset_id) was this asset;
    # a surviving proposal whose *origin* was this asset still targets a
    # different, still-live asset, so detach the now-dangling reference
    # instead of deleting a real change record.
    for proposal in session.exec(
        select(ChangeProposal).where(ChangeProposal.origin_asset_id == asset_id)
    ).all():
        proposal.origin_asset_id = None
        session.add(proposal)

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
        # AssetInterface/AssetService are the two child tables with a real
        # "duplicate row" concept (a NIC or a listening port either matches
        # one the survivor already has, or it doesn't) -- unlike
        # Finding/AssetNote/ProbeResult/ChatMessage/ChangeProposal, which
        # are append-only logs where a merge is never expected to collapse
        # rows. Without this, merging two assets that both have e.g. an
        # interface for the same IP (exactly the case correlate.py scores
        # as a duplicate signal) left the survivor with the same NIC twice,
        # forever -- no unique constraint on either table catches it, and
        # reconcile_into_db's own `.first()` lookup then refreshes only one
        # of the two copies on every subsequent discovery run.
        if model is AssetInterface:
            existing = {
                (row.mac, row.ip)
                for row in session.exec(
                    select(AssetInterface).where(AssetInterface.asset_id == survivor_id)
                ).all()
            }
            for row in session.exec(
                select(AssetInterface).where(AssetInterface.asset_id == duplicate_id)
            ).all():
                key = (row.mac, row.ip)
                if key in existing:
                    session.delete(row)
                    continue
                row.asset_id = survivor_id
                session.add(row)
                existing.add(key)
        elif model is AssetService:
            existing = {
                (row.port, row.protocol)
                for row in session.exec(
                    select(AssetService).where(AssetService.asset_id == survivor_id)
                ).all()
            }
            for row in session.exec(
                select(AssetService).where(AssetService.asset_id == duplicate_id)
            ).all():
                key = (row.port, row.protocol)
                if key in existing:
                    session.delete(row)
                    continue
                row.asset_id = survivor_id
                session.add(row)
                existing.add(key)
        else:
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

    # ChangeProposal's second FK -- see the module docstring and the mirror
    # block in delete_asset_cascade. The loop above already repointed
    # asset_id == duplicate_id; repoint origin_asset_id too, for a proposal
    # whose target survived this merge but was drafted from the duplicate's
    # chat.
    for proposal in session.exec(
        select(ChangeProposal).where(ChangeProposal.origin_asset_id == duplicate_id)
    ).all():
        proposal.origin_asset_id = survivor_id
        session.add(proposal)

    session.flush()

    # Repointing can produce a relationship that now points an asset at
    # itself (e.g. the survivor was already linked to the duplicate as
    # "same_physical_device") or a duplicate of a relationship the survivor
    # already had. Both are cleanup artifacts of the merge, not real data.
    #
    # Must check BOTH asset_id and related_asset_id == survivor_id. Example:
    # survivor S and duplicate D were each already linked to some third asset
    # X, i.e. mirror pairs (S,X)/(X,S) and (D,X)/(X,D) all pre-exist. The
    # repoint loop above turns (D,X)->(S,X) and (X,D)->(X,S), leaving TWO
    # (S,X) rows and TWO (X,S) rows. Querying asset_id == survivor_id alone
    # only ever sees the (S,X)-shaped rows -- the (X,S) duplicate pair is
    # invisible to it and survives uncaught. The dedupe key stays DIRECTED
    # (not normalized to (min,max,type)): (S,X) and (X,S) are the two real,
    # legitimate halves of one mirrored link (see correlate.py's
    # link_assets), not duplicates of each other -- only two rows sharing
    # the exact same (asset_id, related_asset_id, type) are.
    seen = set()
    for rel in session.exec(
        select(CIRelationship).where(
            (CIRelationship.asset_id == survivor_id)
            | (CIRelationship.related_asset_id == survivor_id)
        )
    ).all():
        key = (rel.asset_id, rel.related_asset_id, rel.relationship_type)
        if rel.asset_id == rel.related_asset_id or key in seen:
            session.delete(rel)
        else:
            seen.add(key)
