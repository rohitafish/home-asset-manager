"""add locations and asset notes timeline

Revision ID: 8f3c1a9d2b44
Revises: 2a8bdd9a266d
Create Date: 2026-07-26 09:00:00.000000

Adds the Location table and an append-only AssetNote table, then migrates
every existing non-null asset.notes value into an AssetNote row (author
"imported", timestamped at the asset's first_seen) before dropping the old
notes column. Also adds location_id/position/model/firmware_version to
asset.

Caveat: downgrade() re-adds an empty notes column -- the imported note text
is not written back to it. Acceptable for this personal-use app; if you need
to reverse this migration and keep the text, read it back out of assetnote
first.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel


# revision identifiers, used by Alembic.
revision: str = '8f3c1a9d2b44'
down_revision: Union[str, Sequence[str], None] = '2a8bdd9a266d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'location',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('name', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('description', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column('sort_order', sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_location_name'), 'location', ['name'], unique=True)

    op.create_table(
        'assetnote',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('asset_id', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('author', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('body', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.ForeignKeyConstraint(['asset_id'], ['asset.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_assetnote_asset_id'), 'assetnote', ['asset_id'], unique=False)

    op.add_column('asset', sa.Column('model', sqlmodel.sql.sqltypes.AutoString(), nullable=True))
    op.add_column('asset', sa.Column('firmware_version', sqlmodel.sql.sqltypes.AutoString(), nullable=True))
    op.add_column('asset', sa.Column('location_id', sa.Integer(), nullable=True))
    op.add_column('asset', sa.Column('position', sqlmodel.sql.sqltypes.AutoString(), nullable=True))
    op.create_index(op.f('ix_asset_location_id'), 'asset', ['location_id'], unique=False)
    op.create_foreign_key(
        'fk_asset_location_id_location', 'asset', 'location', ['location_id'], ['id']
    )

    # Data migration: carry every existing note forward into the new timeline
    # before the column disappears.
    op.execute(
        """
        INSERT INTO assetnote (asset_id, created_at, author, body)
        SELECT id, first_seen, 'imported', notes
        FROM asset
        WHERE notes IS NOT NULL
        """
    )

    op.drop_column('asset', 'notes')


def downgrade() -> None:
    """Downgrade schema."""
    op.add_column('asset', sa.Column('notes', sqlmodel.sql.sqltypes.AutoString(), nullable=True))

    op.drop_constraint('fk_asset_location_id_location', 'asset', type_='foreignkey')
    op.drop_index(op.f('ix_asset_location_id'), table_name='asset')
    op.drop_column('asset', 'position')
    op.drop_column('asset', 'location_id')
    op.drop_column('asset', 'firmware_version')
    op.drop_column('asset', 'model')

    op.drop_index(op.f('ix_assetnote_asset_id'), table_name='assetnote')
    op.drop_table('assetnote')

    op.drop_index(op.f('ix_location_name'), table_name='location')
    op.drop_table('location')
