"""merge channels branch with resolved_at branch.

Revision ID: 2a5d3be0dbcd
Revises: 5b7026c4fe19, d00000000002
Create Date: 2026-09-16 19:14:39.399376

"""
from typing import Sequence, Union

import sqlmodel  # noqa: F401


# revision identifiers, used by Alembic.
revision: str = '2a5d3be0dbcd'
down_revision: Union[str, Sequence[str], None] = ('5b7026c4fe19', 'd00000000002')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
