"""add is_valuable to asset

Lets a specific asset be excluded from the Valuables page's default view
regardless of criticality or identity/purchase data -- e.g. a smart plug
that's legitimately high-criticality (attack-surface concern) but worth
nothing for insurance. Criticality and "worth showing on Valuables" are
different axes this schema previously had no way to tell apart. Defaults
True via server_default, so every existing asset's visibility is unchanged
until explicitly unchecked on its Edit page.

Revision ID: b25fed154832
Revises: c3f8a1d6e29b
Create Date: 2026-08-15 16:59:34.666697

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b25fed154832'
down_revision: Union[str, Sequence[str], None] = 'c3f8a1d6e29b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'asset',
        sa.Column('is_valuable', sa.Boolean(), nullable=False, server_default=sa.true()),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('asset', 'is_valuable')
