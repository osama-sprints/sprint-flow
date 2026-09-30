"""add knowledge_candidates

--> Target path: alembic/versions/0005_knowledge_candidates.py

Revision ID: 0005_knowledge_candidates
Revises: e79a5dd80fe3
Create Date: 2026-09-15 00:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "0005_knowledge_candidates"
down_revision: Union[str, Sequence[str], None] = "e79a5dd80fe3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "knowledge_candidates",
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("escalation_id", sa.Integer(), nullable=False),
        sa.Column("statement", sa.Text(), nullable=False),
        sa.Column("audience", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("reviewer_id", sa.Integer(), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rejection_reason", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["escalation_id"], ["escalation_tickets.id"]),
        sa.ForeignKeyConstraint(["reviewer_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("escalation_id", name="uq_knowledge_candidates_escalation_id"),
    )
    op.create_index(op.f("ix_knowledge_candidates_audience"), "knowledge_candidates", ["audience"], unique=False)
    op.create_index(op.f("ix_knowledge_candidates_reviewer_id"), "knowledge_candidates", ["reviewer_id"], unique=False)
    op.create_index(op.f("ix_knowledge_candidates_status"), "knowledge_candidates", ["status"], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f("ix_knowledge_candidates_status"), table_name="knowledge_candidates")
    op.drop_index(op.f("ix_knowledge_candidates_reviewer_id"), table_name="knowledge_candidates")
    op.drop_index(op.f("ix_knowledge_candidates_audience"), table_name="knowledge_candidates")
    op.drop_table("knowledge_candidates")
