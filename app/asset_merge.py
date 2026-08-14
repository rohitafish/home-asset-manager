"""Merges one asset into another: reassigns interfaces, services, findings,
notes, and CI relationships, then deletes the now-empty duplicate. Always a
deliberate, human-confirmed action (see app/routers/dashboard.py's
/assets/duplicates page) -- there's no reliable signal to do this safely
without a person confirming two records really are the same physical asset.
"""

from sqlmodel import Session

from app.asset_children import reassign_asset_children
from app.models import Asset


def merge_asset_into(session: Session, survivor_id: int, duplicate_id: int) -> None:
    if survivor_id == duplicate_id:
        return

    reassign_asset_children(session, survivor_id, duplicate_id)

    duplicate = session.get(Asset, duplicate_id)
    if duplicate:
        session.delete(duplicate)
