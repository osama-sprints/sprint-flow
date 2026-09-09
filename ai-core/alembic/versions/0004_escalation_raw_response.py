"""add raw_human_response to escalation_tickets

--> Target path: alembic/versions/0004_escalation_raw_human_response.py
    (confirm the actual current head revision id and set down_revision to it
    -- check `alembic heads` first, don't assume 0003_escalation_open_thread_unique
    is still the tip by the time you apply this)

Revision ID: 0004_escalation_raw_human_response
Revises: 0003_escalation_open_thread_unique
Create Date: 2026-09-04 00:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "0004_escalation_raw_response"
down_revision: Union[str, Sequence[str], None] = ("0003_escalation_open_thread", "0003_policy_chunks")
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "escalation_tickets",
        sa.Column("raw_human_response", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("escalation_tickets", "raw_human_response")
