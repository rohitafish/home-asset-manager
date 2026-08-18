"""add ai_usage and app_setting

Adds the spend ledger (aiusage, one row per billed Claude API call) and a
generic key/value settings store (appsetting, first used for the
"ai_assistant_enabled" kill switch) behind the investigation assistant's
budget enforcement -- see app/assistant.py's budget_block_reason() and
_log_usage(). Before this, per-call token usage only ever reached an INFO
log line (suppressed entirely at LOG_LEVEL=WARNING) and there was no
aggregate spend visibility, no ceiling, and no in-app way to turn the
assistant off short of unsetting its API key and restarting the app.

Revision ID: 0e85da4de324
Revises: b25fed154832
Create Date: 2026-08-18 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel


# revision identifiers, used by Alembic.
revision: str = '0e85da4de324'
down_revision: Union[str, Sequence[str], None] = 'b25fed154832'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'aiusage',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('asset_id', sa.Integer(), nullable=True),
        sa.Column('call_site', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('provider', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('model', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column('iteration', sa.Integer(), nullable=False),
        sa.Column('input_tokens', sa.Integer(), nullable=True),
        sa.Column('output_tokens', sa.Integer(), nullable=True),
        sa.Column('cache_read_tokens', sa.Integer(), nullable=True),
        sa.Column('cache_write_tokens', sa.Integer(), nullable=True),
        sa.Column('stop_reason', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column('cost_usd', sa.Numeric(10, 6), nullable=True),
        sa.ForeignKeyConstraint(['asset_id'], ['asset.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_aiusage_asset_id'), 'aiusage', ['asset_id'], unique=False)
    op.create_index(op.f('ix_aiusage_created_at'), 'aiusage', ['created_at'], unique=False)

    op.create_table(
        'appsetting',
        sa.Column('key', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('value', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('key'),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('appsetting')

    op.drop_index(op.f('ix_aiusage_created_at'), table_name='aiusage')
    op.drop_index(op.f('ix_aiusage_asset_id'), table_name='aiusage')
    op.drop_table('aiusage')
