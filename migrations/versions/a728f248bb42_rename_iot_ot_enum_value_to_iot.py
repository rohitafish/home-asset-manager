"""rename iot_ot enum value to iot

Revision ID: a728f248bb42
Revises: 116cb2da1cef
Create Date: 2026-07-04 17:26:04.049399

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel


# revision identifiers, used by Alembic.
revision: str = 'a728f248bb42'
down_revision: Union[str, Sequence[str], None] = '116cb2da1cef'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.execute("ALTER TYPE assettype RENAME VALUE 'iot_ot' TO 'iot'")


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("ALTER TYPE assettype RENAME VALUE 'iot' TO 'iot_ot'")
