"""add target_usernames to announcements.

Revision ID: c1a4f8e2d905
Revises: b7c2e4f19a03
Create Date: 2026-09-23

The resolved audience (from resolve_recipients_by_role/usernames) was
computed correctly at preview time and shown correctly in the preview text,
but never persisted anywhere — confirm_and_dispatch_announcement had no way
to know who the real recipients were, and DM delivery silently fell back to
a hardcoded "learner" for every announcement regardless of the real target.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c1a4f8e2d905'
#down_revision: Union[str, Sequence[str], None] = 'b7c2e4f19a03'
down_revision = '109ddca5e1f5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('announcements', sa.Column('target_usernames', sa.String(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('announcements', 'target_usernames')
