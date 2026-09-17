"""merge standup prompts migration into calendar merge head.

Revision ID: d32cf4519a86
Revises: 53de1e9a8ee1, 0005_standup_prompts
Create Date: 2026-09-17 08:49:45.319002

"""
from typing import Sequence, Union

import sqlmodel  # noqa: F401
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd32cf4519a86'
down_revision: Union[str, Sequence[str], None] = ('53de1e9a8ee1', '0005_standup_prompts')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
