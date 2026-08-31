"""normalize sonos serial numbers

The local API (probes/sonos_api.py's device_description.xml/status-zp
parsing) and the account import (discovery/account_import.py, from
devices/accounts.json) used to write the same physical Sonos player's serial
in two different formats -- "AA-BB-CC-DD-EE-FF:1" from the former,
"AABBCCDDEEFF1" from the latter -- so whichever collector ran most recently
won, and account_import.plan_changes kept re-planning the same "change"
forever since it compared the two forms as raw strings. Both ingest paths now
normalize to the separator-less, uppercase form via the new
probes.sonos_api.normalize_sonos_serial (see that function's docstring for
the exact contract); this backfills the rows written before that existed.

The regex matches only the dashes-plus-colon local-API shape -- six
dash-separated hex pairs, a colon, one trailing character -- so it can't
touch any other vendor's serial (in particular Ubiquiti's, which are bare
12-hex-digit MACs with no separators at all, and so already look like the
target format).

Revision ID: 91c7c4445614
Revises: d2b597cf4d6a
Create Date: 2026-08-31 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = '91c7c4445614'
down_revision: Union[str, Sequence[str], None] = 'd2b597cf4d6a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.execute(r"""
        UPDATE asset
        SET serial_number = upper(replace(replace(serial_number, '-', ''), ':', ''))
        WHERE serial_number ~ '^[0-9A-Fa-f]{2}(-[0-9A-Fa-f]{2}){5}:[0-9A-Za-z]$'
    """)


def downgrade() -> None:
    """Downgrade schema.

    Deliberately a no-op: the canonical (separator-less) form this migration
    produces is itself a valid Sonos serial -- both formats have always been
    accepted by every reader in this codebase -- and a blind reverse (re-
    inserting dashes/colon) can't tell a normalized Sonos serial apart from
    any other vendor's bare-hex serial that happens to be 12-13 characters
    (e.g. Ubiquiti's), so it would corrupt those instead of only undoing this
    change.
    """
    pass
