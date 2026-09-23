"""merge announcements and knowledge capture.

Revision ID: 29f10e715dff
Revises: 0005_knowledge_candidates, 5b7026c4fe19
Create Date: 2026-09-16 19:13:27.662126

"""
from typing import Sequence, Union

import sqlmodel  # noqa: F401


# revision identifiers, used by Alembic.
revision: str = '29f10e715dff'
down_revision: Union[str, Sequence[str], None] = ('0005_knowledge_candidates', '5b7026c4fe19')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
