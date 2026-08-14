"""add origin_asset_id to changeproposal

Records which asset's chat produced a proposal, so a multi-asset document (one
invoice covering several devices) analysed on one asset's page can surface the
proposals it made against *other* assets. Existing rows are backfilled to
origin == target (asset_id), i.e. same-asset proposals, so they keep showing
only on their own asset's page.

Revision ID: b7d2e5f1a3c9
Revises: 9f1c4a6e7b23
Create Date: 2026-08-13 10:20:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b7d2e5f1a3c9'
down_revision: Union[str, Sequence[str], None] = '9f1c4a6e7b23'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('changeproposal', sa.Column('origin_asset_id', sa.Integer(), nullable=True))
    op.create_index(
        op.f('ix_changeproposal_origin_asset_id'), 'changeproposal', ['origin_asset_id'], unique=False
    )
    op.create_foreign_key(
        'fk_changeproposal_origin_asset_id_asset', 'changeproposal', 'asset',
        ['origin_asset_id'], ['id'],
    )
    # Backfill: an existing proposal originated on its own target's page.
    op.execute('UPDATE changeproposal SET origin_asset_id = asset_id WHERE origin_asset_id IS NULL')


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint('fk_changeproposal_origin_asset_id_asset', 'changeproposal', type_='foreignkey')
    op.drop_index(op.f('ix_changeproposal_origin_asset_id'), table_name='changeproposal')
    op.drop_column('changeproposal', 'origin_asset_id')
