"""add software notes

An append-only review log per piece of software (softwarenote), shown in the
Software tab's Notes column: what was checked and decided about a red or
amber row, so the next review starts from it. New table only; nothing
existing changes, so downgrading just drops it (and the notes with it).

Revision ID: 932bd017edaf
Revises: e4a7c2b9d1f3
Create Date: 2026-11-22 20:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel


# revision identifiers, used by Alembic.
revision: str = '932bd017edaf'
down_revision: Union[str, Sequence[str], None] = 'e4a7c2b9d1f3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'softwarenote',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('software_id', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('author', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('body', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.ForeignKeyConstraint(['software_id'], ['softwareitem.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_softwarenote_software_id'), 'softwarenote', ['software_id'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_softwarenote_software_id'), table_name='softwarenote')
    op.drop_table('softwarenote')
