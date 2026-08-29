"""dedupe and constrain assetservice

Before app/asset_children.py's reassign_asset_children gained its
(port, protocol) dedupe (commit fbd9b17), merging two assets that shared a
listening port silently duplicated it -- discovery/reconcile.py's own
upsert (on (asset_id, port, protocol)) has always been correct, so this was
purely a merge-path bug. Real production data confirmed 13 groups of
duplicate rows with divergent last_seen timestamps (one refreshed by every
subsequent scan, the others frozen at merge time).

Cleans those up here, then adds the unique constraint neither table has ever
had, so the invariant is enforced at the DB level for good -- not just by
whichever application code path happens to remember to dedupe. Not extended
to assetinterface: its upsert key (mac, falling back to ip) is nullable on
both fields, and no duplicates were found there.

Revision ID: d2b597cf4d6a
Revises: 7a51b04ce38d
Create Date: 2026-08-29 10:15:00.000000

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'd2b597cf4d6a'
down_revision: Union[str, Sequence[str], None] = '7a51b04ce38d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Collapse each (asset_id, port, protocol) duplicate group down to the
    # row with the latest last_seen (highest id as tiebreak). Standard
    # Postgres "delete losing duplicates" self-join: every row that has a
    # same-group sibling with a strictly later (last_seen, id) gets deleted;
    # the true latest row in each group never matches anything and survives.
    # Must run before the constraint below -- it would otherwise fail to
    # create against the violating rows this cleans up.
    op.execute("""
        DELETE FROM assetservice a
        USING assetservice b
        WHERE a.asset_id = b.asset_id
          AND a.port = b.port
          AND a.protocol = b.protocol
          AND (a.last_seen, a.id) < (b.last_seen, b.id)
    """)
    op.create_unique_constraint(
        'uq_assetservice_asset_port_protocol', 'assetservice',
        ['asset_id', 'port', 'protocol'],
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint('uq_assetservice_asset_port_protocol', 'assetservice', type_='unique')
