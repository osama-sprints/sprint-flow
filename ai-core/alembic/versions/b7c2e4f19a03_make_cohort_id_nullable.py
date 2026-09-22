"""make announcements.cohort_id nullable.

Revision ID: b7c2e4f19a03
Revises: a3f9d1c88b4e
Create Date: 2026-09-22

A refusal audit row for a cohort that does not exist at all cannot
reference it without violating the FK to sprints.id. cohort_id is now
nullable for that one case; every other outcome always carries a real id.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'b7c2e4f19a03'
down_revision: Union[str, Sequence[str], None] = 'a3f9d1c88b4e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.alter_column('announcements', 'cohort_id', existing_type=sa.Integer(), nullable=True)


def downgrade() -> None:
    """Downgrade schema.

    Reverting to NOT NULL will fail if any row already has cohort_id=NULL
    (the "cohort not found" refusal case) — delete or backfill those rows
    first if you actually need to downgrade past this point.
    """
    op.alter_column('announcements', 'cohort_id', existing_type=sa.Integer(), nullable=False)
