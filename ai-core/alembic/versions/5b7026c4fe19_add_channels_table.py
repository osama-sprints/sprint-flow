"""add channels table.

Revision ID: 5b7026c4fe19
Revises: 5fe198326da8
Create Date: 2026-09-16 14:08:01.249122

"""
from typing import Sequence, Union

import sqlmodel
from alembic import op
import sqlalchemy as sa


revision: str = '5b7026c4fe19'
down_revision: Union[str, Sequence[str], None] = '5fe198326da8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'channels',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('name', sa.String(length=255), nullable=True),
        sa.Column('mattermost_channel_id', sa.String(length=64), nullable=True),
        sa.Column('team_id', sa.String(length=64), nullable=True),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_channels_name'), 'channels', ['name'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_channels_name'), table_name='channels')
    op.drop_table('channels')