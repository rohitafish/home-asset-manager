"""add chat and proposals

Revision ID: 6b2e91c8a0d5
Revises: 3d7a06e4f1c9
Create Date: 2026-07-26 09:40:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel


# revision identifiers, used by Alembic.
revision: str = '6b2e91c8a0d5'
down_revision: Union[str, Sequence[str], None] = '3d7a06e4f1c9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'chatmessage',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('asset_id', sa.Integer(), nullable=False),
        sa.Column('role', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('content_json', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['asset_id'], ['asset.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_chatmessage_asset_id'), 'chatmessage', ['asset_id'], unique=False)

    op.create_table(
        'changeproposal',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('asset_id', sa.Integer(), nullable=False),
        sa.Column('kind', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('payload_json', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('rationale', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column('status', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('applied_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['asset_id'], ['asset.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_changeproposal_asset_id'), 'changeproposal', ['asset_id'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_changeproposal_asset_id'), table_name='changeproposal')
    op.drop_table('changeproposal')

    op.drop_index(op.f('ix_chatmessage_asset_id'), table_name='chatmessage')
    op.drop_table('chatmessage')
