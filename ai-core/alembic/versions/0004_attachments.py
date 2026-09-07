"""Records of files people attach to messages.

Revision ID: 0004_attachments
Revises: 0003_rich_artifacts
Create Date: 2026-09-07

The row is keyed by the Mattermost file id: an upload is one file, and a turn
that is retried after a crash re-ingests the same ids rather than minting new
rows. ``ix_attachments_conversation`` serves the read tools, which are scoped
to the conversation the file arrived in; ``ix_attachments_expires`` serves the
retention sweep.
"""

from typing import (
    Sequence,
    Union,
)

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0004_attachments"
down_revision: Union[str, Sequence[str], None] = "0003_rich_artifacts"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create the attachments table and its indexes."""
    op.create_table(
        "attachments",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("post_id", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("channel_id", sa.String(length=64), nullable=False),
        sa.Column("root_id", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("session_id", sa.String(length=128), nullable=False, server_default=""),
        sa.Column("turn_id", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("mattermost_user_id", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("requester_user_id", sa.Integer(), nullable=True),
        sa.Column("name", sa.String(length=512), nullable=False),
        sa.Column("extension", sa.String(length=16), nullable=False, server_default=""),
        sa.Column("claimed_mime", sa.String(length=128), nullable=False, server_default=""),
        sa.Column("detected_mime", sa.String(length=128), nullable=False, server_default=""),
        sa.Column("kind", sa.String(length=32), nullable=False, server_default=""),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("sha256", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("rejection_reason", sa.Text(), nullable=True),
        sa.Column("extraction", sa.String(length=32), nullable=False, server_default="none"),
        sa.Column("page_count", sa.Integer(), nullable=True),
        sa.Column("text", sa.Text(), nullable=True),
        sa.Column("text_chars", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("text_truncated", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("visual", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["requester_user_id"], ["users.id"], name="fk_attachments_requester"),
        sa.PrimaryKeyConstraint("id", name="pk_attachments"),
    )
    op.create_index("ix_attachments_post", "attachments", ["post_id"])
    op.create_index("ix_attachments_conversation", "attachments", ["session_id", "channel_id"])
    op.create_index("ix_attachments_expires", "attachments", ["expires_at"])


def downgrade() -> None:
    """Drop the table and its indexes."""
    op.drop_index("ix_attachments_expires", table_name="attachments")
    op.drop_index("ix_attachments_conversation", table_name="attachments")
    op.drop_index("ix_attachments_post", table_name="attachments")
    op.drop_table("attachments")
