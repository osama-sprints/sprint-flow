"""merge target_usernames into main.

Revision ID: 8b4f7bf3a875
Revises: 109ddca5e1f5, c1a4f8e2d905
Create Date: 2026-09-28 00:19:45.594353

"""
from typing import Sequence, Union

import sqlmodel  # noqa: F401
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '8b4f7bf3a875'
down_revision: Union[str, Sequence[str], None] = ('109ddca5e1f5', 'c1a4f8e2d905')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
