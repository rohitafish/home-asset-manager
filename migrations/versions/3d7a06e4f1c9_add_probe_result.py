"""add probe result

Revision ID: 3d7a06e4f1c9
Revises: 8f3c1a9d2b44
Create Date: 2026-07-26 09:20:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel


# revision identifiers, used by Alembic.
revision: str = '3d7a06e4f1c9'
down_revision: Union[str, Sequence[str], None] = '8f3c1a9d2b44'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'proberesult',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('asset_id', sa.Integer(), nullable=False),
        sa.Column('probe_name', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('target_ip', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column('ran_at', sa.DateTime(), nullable=False),
        sa.Column('ok', sa.Boolean(), nullable=False),
        sa.Column('summary', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('facts_json', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column('suggestions_json', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column('raw', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.ForeignKeyConstraint(['asset_id'], ['asset.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_proberesult_asset_id'), 'proberesult', ['asset_id'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_proberesult_asset_id'), table_name='proberesult')
    op.drop_table('proberesult')
