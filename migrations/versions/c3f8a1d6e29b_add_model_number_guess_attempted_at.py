"""add model_number_guess_attempted_at to asset

Records when the model_number auto-guess last ran for an asset (on a success or
a no-answer miss), so _autofill_model_number runs at most once per asset instead
of paying for a fresh LLM call on every save when the guess comes back unknown.
Nullable with no backfill: an existing asset that has never been guessed for
stays NULL and is eligible for its one attempt on next save.

Revision ID: c3f8a1d6e29b
Revises: b7d2e5f1a3c9
Create Date: 2026-08-14 09:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c3f8a1d6e29b'
down_revision: Union[str, Sequence[str], None] = 'b7d2e5f1a3c9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'asset',
        sa.Column('model_number_guess_attempted_at', sa.DateTime(), nullable=True),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('asset', 'model_number_guess_attempted_at')
