"""Durable record of each agent turn, for progress, cancel and retry.

Revision ID: 0005_executions
Revises: 0004_attachments
Create Date: 2026-09-07

One row per turn, keyed by the turn id. The trigger message is stored whole so
a retry can re-run it after a restart; ``heartbeat_at`` lets start-up tell a
turn that is still running elsewhere from one whose process died.
"""

from typing import (
    Sequence,
    Union,
)

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0005_executions"
down_revision: Union[str, Sequence[str], None] = "0004_attachments"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create the executions table and its indexes."""
    op.create_table(
        "executions",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("session_id", sa.String(length=128), nullable=False, server_default=""),
        sa.Column("channel_id", sa.String(length=64), nullable=False),
        sa.Column("root_id", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("trigger_post_id", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("source", sa.String(length=32), nullable=False, server_default=""),
        sa.Column("mattermost_user_id", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("requester_user_id", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="running"),
        sa.Column("step", sa.String(length=200), nullable=False, server_default=""),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("retry_of", sa.String(length=64), nullable=True),
        sa.Column("cancel_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancel_requested_by", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("reply_post_id", sa.String(length=64), nullable=True),
        sa.Column("trigger", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default="{}"),
        sa.Column("worker", sa.String(length=128), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["requester_user_id"], ["users.id"], name="fk_executions_requester"),
        sa.PrimaryKeyConstraint("id", name="pk_executions"),
    )
    op.create_index("ix_executions_trigger_post", "executions", ["trigger_post_id"])
    op.create_index("ix_executions_session", "executions", ["session_id", "created_at"])
    op.create_index("ix_executions_status", "executions", ["status"])


def downgrade() -> None:
    """Drop the table and its indexes."""
    op.drop_index("ix_executions_status", table_name="executions")
    op.drop_index("ix_executions_session", table_name="executions")
    op.drop_index("ix_executions_trigger_post", table_name="executions")
    op.drop_table("executions")
