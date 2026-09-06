"""Durable records for agent-authored rich artifacts.

Revision ID: 0003_rich_artifacts
Revises: 0002_sprints_name_ci
Create Date: 2026-09-06

The table is both the artifact store and the job queue for the work that
finishes after a reply is published (image generation). That is deliberate: the
alternative — a separate jobs table pointing at artifacts — needs a second
transaction to keep the two in step, and the onboarding outbox already proves
the single-row-with-a-lease pattern here.

``operation_id`` is unique so a replayed graph cannot pay for the same image
twice; ``ix_rich_artifacts_pending`` is the claim index the dispatcher scans.
"""

from typing import (
    Sequence,
    Union,
)

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0003_rich_artifacts"
down_revision: Union[str, Sequence[str], None] = "0002_sprints_name_ci"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create the rich_artifacts table and its indexes."""
    op.create_table(
        "rich_artifacts",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("turn_id", sa.String(length=64), nullable=False),
        sa.Column("session_id", sa.String(length=128), nullable=False, server_default=""),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="ready"),
        sa.Column("title", sa.String(length=200), nullable=False, server_default=""),
        sa.Column("description", sa.String(length=500), nullable=False, server_default=""),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("content", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default="{}"),
        sa.Column("requester_user_id", sa.Integer(), nullable=True),
        sa.Column("mattermost_user_id", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("channel_id", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("root_id", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("post_id", sa.String(length=64), nullable=True),
        sa.Column("file_ids", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default="[]"),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("operation_id", sa.String(length=128), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("claimed_by", sa.String(length=128), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["requester_user_id"], ["users.id"], name="fk_rich_artifacts_requester"),
        sa.PrimaryKeyConstraint("id", name="pk_rich_artifacts"),
        sa.UniqueConstraint("operation_id", name="uq_rich_artifacts_operation"),
    )
    op.create_index("ix_rich_artifacts_turn", "rich_artifacts", ["turn_id"])
    op.create_index("ix_rich_artifacts_status", "rich_artifacts", ["status"])
    op.create_index("ix_rich_artifacts_channel", "rich_artifacts", ["channel_id"])
    op.create_index("ix_rich_artifacts_post", "rich_artifacts", ["post_id"])
    op.create_index("ix_rich_artifacts_requester", "rich_artifacts", ["requester_user_id"])
    op.create_index("ix_rich_artifacts_pending", "rich_artifacts", ["status", "next_attempt_at"])


def downgrade() -> None:
    """Drop the table and its indexes."""
    op.drop_index("ix_rich_artifacts_pending", table_name="rich_artifacts")
    op.drop_index("ix_rich_artifacts_requester", table_name="rich_artifacts")
    op.drop_index("ix_rich_artifacts_post", table_name="rich_artifacts")
    op.drop_index("ix_rich_artifacts_channel", table_name="rich_artifacts")
    op.drop_index("ix_rich_artifacts_status", table_name="rich_artifacts")
    op.drop_index("ix_rich_artifacts_turn", table_name="rich_artifacts")
    op.drop_table("rich_artifacts")
