"""Shared "find an asset by name/model" text search, used by both the Assets
page's search box (app/routers/dashboard.py's assets_list) and the AI
assistant's search_assets tool (app/assistant.py's _tool_search_assets) --
kept in one place so the two don't quietly drift out of sync on which fields
count as a match. A standalone module rather than living in either caller,
matching the existing pattern of small focused Asset-operation modules
(app/asset_children.py, app/asset_merge.py) instead of one importing from
the other -- dashboard.py already imports assistant.py, so the reverse would
be circular.
"""

from sqlalchemy import or_

from app.models import Asset

# Fields a human would actually search by name/model -- e.g. "ultra" for a
# UniFi U6-Ultra, "move" for a Sonos Move, "kitchen" for a custodian/position
# note. Deliberately Asset's own columns only, no join into notes/
# interfaces/services -- same scope as every other filter on the Assets page.
ASSET_SEARCH_FIELDS = (
    Asset.hostname,
    Asset.vendor,
    Asset.model,
    Asset.model_number,
    Asset.model_identifier,
    Asset.owner,
    Asset.custodian,
    Asset.position,
)


def asset_search_filter(q: str):
    """A case-insensitive substring match across ASSET_SEARCH_FIELDS,
    suitable for `query.where(asset_search_filter(q))`. Callers should skip
    calling this at all when q is blank (an empty pattern still matches
    everything, just uselessly)."""
    like = f"%{q.strip()}%"
    return or_(*(field.ilike(like) for field in ASSET_SEARCH_FIELDS))
