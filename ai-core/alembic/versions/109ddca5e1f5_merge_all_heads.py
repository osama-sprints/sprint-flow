"""merge_all_heads.

Revision ID: 109ddca5e1f5
Revises: b7c2e4f19a03, d32cf4519a86
Create Date: 2026-09-22 10:57:40.506208

"""
from typing import Sequence, Union

import sqlmodel  # noqa: F401
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '109ddca5e1f5'
down_revision: Union[str, Sequence[str], None] = ('b7c2e4f19a03', 'd32cf4519a86')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
