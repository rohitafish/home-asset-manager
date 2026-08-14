"""add asset identity and support data

Revision ID: 9f1c4a6e7b23
Revises: 6b2e91c8a0d5
Create Date: 2026-07-29 16:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel


# revision identifiers, used by Alembic.
revision: str = '9f1c4a6e7b23'
down_revision: Union[str, Sequence[str], None] = '6b2e91c8a0d5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('asset', sa.Column('serial_number', sqlmodel.sql.sqltypes.AutoString(), nullable=True))
    op.create_index(op.f('ix_asset_serial_number'), 'asset', ['serial_number'], unique=False)
    op.add_column('asset', sa.Column('model_number', sqlmodel.sql.sqltypes.AutoString(), nullable=True))
    op.add_column('asset', sa.Column('model_identifier', sqlmodel.sql.sqltypes.AutoString(), nullable=True))
    op.add_column('asset', sa.Column('identity_locked', sa.Boolean(), nullable=False, server_default=sa.false()))
    op.alter_column('asset', 'identity_locked', server_default=None)
    op.add_column('asset', sa.Column('purchase_date', sa.Date(), nullable=True))
    op.add_column('asset', sa.Column('purchase_price', sa.Numeric(12, 2), nullable=True))
    op.add_column('asset', sa.Column('replacement_value', sa.Numeric(12, 2), nullable=True))
    op.add_column('asset', sa.Column('warranty_expiry', sa.Date(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('asset', 'warranty_expiry')
    op.drop_column('asset', 'replacement_value')
    op.drop_column('asset', 'purchase_price')
    op.drop_column('asset', 'purchase_date')
    op.drop_column('asset', 'identity_locked')
    op.drop_column('asset', 'model_identifier')
    op.drop_column('asset', 'model_number')
    op.drop_index(op.f('ix_asset_serial_number'), table_name='asset')
    op.drop_column('asset', 'serial_number')
