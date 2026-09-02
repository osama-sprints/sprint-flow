"""add onboarding_state

Revision ID: a1e6c9f0d3b2
Revises: b25d38b0cd7c
Create Date: 2026-08-31 00:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
import sqlmodel  # noqa: F401

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a1e6c9f0d3b2"
down_revision: Union[str, Sequence[str], None] = "eba532c6476d"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "onboarding_state",
        sa.Column("user_id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("role", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("greeted_at", sa.DateTime(), nullable=True),
        sa.Column("next_followup_due_at", sa.DateTime(), nullable=True),
        sa.Column("followups_sent", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("user_id"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("onboarding_state")