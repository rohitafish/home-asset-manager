"""add same device dismissal

Adds samedevicedismissal, one row per asset-id pair a user has judged NOT the
same physical device on /assets/investigate. Before this, the only way a
same-device candidate pair left that page was by being linked
(find_same_device_candidates()'s already_linked filter) -- a pair judged
"these are different" had no way to record that and was simply re-offered on
the next page load. evidence_fingerprint snapshots the identity fields the
scorer reads (hostname, vendor, asset_type, interface MACs) at dismissal
time, so a dismissal is dropped -- the pair re-offered -- once either asset's
identity actually changes, rather than being permanent regardless of new
evidence. See app/correlate.py's dismiss_same_device_candidate and
_pair_fingerprint.

Revision ID: 7a51b04ce38d
Revises: 0e85da4de324
Create Date: 2026-08-18 14:34:43.792251

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel


# revision identifiers, used by Alembic.
revision: str = '7a51b04ce38d'
down_revision: Union[str, Sequence[str], None] = '0e85da4de324'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'samedevicedismissal',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('asset_id_a', sa.Integer(), nullable=False),
        sa.Column('asset_id_b', sa.Integer(), nullable=False),
        sa.Column('evidence_fingerprint', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('dismissed_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['asset_id_a'], ['asset.id']),
        sa.ForeignKeyConstraint(['asset_id_b'], ['asset.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('asset_id_a', 'asset_id_b'),
    )
    op.create_index(
        op.f('ix_samedevicedismissal_asset_id_a'), 'samedevicedismissal', ['asset_id_a'], unique=False
    )
    op.create_index(
        op.f('ix_samedevicedismissal_asset_id_b'), 'samedevicedismissal', ['asset_id_b'], unique=False
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_samedevicedismissal_asset_id_b'), table_name='samedevicedismissal')
    op.drop_index(op.f('ix_samedevicedismissal_asset_id_a'), table_name='samedevicedismissal')
    op.drop_table('samedevicedismissal')
