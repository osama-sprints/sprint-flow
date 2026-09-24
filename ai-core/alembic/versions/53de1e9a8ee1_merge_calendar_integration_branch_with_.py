"""merge calendar-integration branch with knowledge-capture branch.

Revision ID: 53de1e9a8ee1
Revises: 29f10e715dff, 2a5d3be0dbcd
Create Date: 2026-09-16 20:05:39.729290

"""

from typing import Sequence, Union

import sqlmodel  # noqa: F401


# revision identifiers, used by Alembic.
revision: str = "53de1e9a8ee1"
down_revision: Union[str, Sequence[str], None] = ("29f10e715dff", "2a5d3be0dbcd")
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
