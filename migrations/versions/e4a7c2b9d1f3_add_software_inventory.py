"""add software inventory

Adds the Software tab's three tables: softwareitem (one row per distinct
piece of software across every machine), softwareinstall (that item on one
machine, with the facts the red/amber/green grade is computed from) and
softwarehost (when each machine was last collected). Filled by
`python -m discovery.cli software-import`, driven from a dev machine by
scripts/collect-software.sh -- see README's "Software inventory".

Revision ID: e4a7c2b9d1f3
Revises: 91c7c4445614
Create Date: 2026-11-22 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel


# revision identifiers, used by Alembic.
revision: str = 'e4a7c2b9d1f3'
down_revision: Union[str, Sequence[str], None] = '91c7c4445614'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'softwareitem',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('key', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('name', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('publisher', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column('description', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column('homepage', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column('eol_product', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column('first_seen', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_softwareitem_key'), 'softwareitem', ['key'], unique=True)

    op.create_table(
        'softwareinstall',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('software_id', sa.Integer(), nullable=False),
        sa.Column('asset_id', sa.Integer(), nullable=False),
        sa.Column('source', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('package_name', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('category', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('version', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column('published_date', sa.Date(), nullable=True),
        sa.Column('published_source', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column('installed_date', sa.Date(), nullable=True),
        sa.Column('update_available', sa.Boolean(), nullable=False),
        sa.Column('latest_version', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column('security_update', sa.Boolean(), nullable=False),
        sa.Column('deprecated', sa.Boolean(), nullable=False),
        sa.Column('disabled', sa.Boolean(), nullable=False),
        sa.Column('orphaned', sa.Boolean(), nullable=False),
        sa.Column('intel_only', sa.Boolean(), nullable=False),
        sa.Column('vulns_json', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column('eol_cycle', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column('eol_date', sa.Date(), nullable=True),
        sa.Column('upstream_latest', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column('upstream_latest_date', sa.Date(), nullable=True),
        sa.Column('first_seen', sa.DateTime(), nullable=False),
        sa.Column('last_seen', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['asset_id'], ['asset.id']),
        sa.ForeignKeyConstraint(['software_id'], ['softwareitem.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_softwareinstall_asset_id'), 'softwareinstall', ['asset_id'], unique=False)
    op.create_index(op.f('ix_softwareinstall_software_id'), 'softwareinstall', ['software_id'], unique=False)

    op.create_table(
        'softwarehost',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('asset_id', sa.Integer(), nullable=False),
        sa.Column('collected_at', sa.DateTime(), nullable=False),
        sa.Column('hostname', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column('platform', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('os_name', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column('os_version', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column('package_count', sa.Integer(), nullable=False),
        sa.Column('notes_json', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.ForeignKeyConstraint(['asset_id'], ['asset.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_softwarehost_asset_id'), 'softwarehost', ['asset_id'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_softwarehost_asset_id'), table_name='softwarehost')
    op.drop_table('softwarehost')
    op.drop_index(op.f('ix_softwareinstall_software_id'), table_name='softwareinstall')
    op.drop_index(op.f('ix_softwareinstall_asset_id'), table_name='softwareinstall')
    op.drop_table('softwareinstall')
    op.drop_index(op.f('ix_softwareitem_key'), table_name='softwareitem')
    op.drop_table('softwareitem')
