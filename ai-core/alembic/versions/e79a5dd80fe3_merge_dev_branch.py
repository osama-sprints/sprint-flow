"""merge dev branch.

Revision ID: e79a5dd80fe3
Revises: 0004_escalation_raw_response, f3a7c288ced4
Create Date: 2026-09-09 13:47:34.989105

"""
from typing import Sequence, Union

import sqlmodel  # noqa: F401
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e79a5dd80fe3'
down_revision: Union[str, Sequence[str], None] = ('0004_escalation_raw_response', 'f3a7c288ced4')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
