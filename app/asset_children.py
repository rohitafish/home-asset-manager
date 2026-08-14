"""Single source of truth for "what has an asset_id foreign key pointing at
Asset". There's no ON DELETE CASCADE on any of these FKs (see the initial
schema migration), so deleting or merging an asset requires manually cleaning
up every dependent row first -- and that cleanup used to be hand-written and
duplicated across app/routers/dashboard.py, app/routers/assets.py, and
app/asset_merge.py. Two past commits were both "Fix internal server error
when deleting an asset with dependent rows" because one of those three copies
drifted out of sync with the model list. Adding a new asset-child table now
means adding it to ASSET_CHILD_MODELS once, here.

CIRelationship is handled separately (not in this list) because it's a
self-referential asset<->asset link -- a row can reference a given asset via
either asset_id or related_asset_id, not just asset_id.
"""

from sqlmodel import Session, select

from app.models import (
    Asset,
    AssetInterface,
    AssetNote,
    AssetService,
    ChangeProposal,
    ChatMessage,
    CIRelationship,
    Finding,
    ProbeResult,
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

    for rel in session.exec(
        select(CIRelationship).where(
            (CIRelationship.asset_id == asset_id)
            | (CIRelationship.related_asset_id == asset_id)
        )
    ).all():
        session.delete(rel)

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
