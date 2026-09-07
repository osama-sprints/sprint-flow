"""Per-page document cache and read coverage; document metadata on attachments.

Revision ID: 0006_document_pages
Revises: 0005_executions
Create Date: 2026-09-07

A PDF is no longer extracted whole at intake. Its native text is stored per
page here, transcriptions of rendered pages join the same table keyed by
model and rendering settings, and reads are recorded per conversation so a
long document can be covered across turns. Both tables cascade from
``attachments`` so retention keeps working unchanged.
"""

from typing import (
    Sequence,
    Union,
)

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0006_document_pages"
down_revision: Union[str, Sequence[str], None] = "0005_executions"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create document_pages and document_page_reads; add attachments.metadata."""
    op.add_column(
        "attachments",
        sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default="{}"),
    )
    op.create_table(
        "document_pages",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("attachment_id", sa.String(length=64), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("page_no", sa.Integer(), nullable=False),
        sa.Column("label", sa.String(length=64), nullable=True),
        sa.Column("method", sa.String(length=16), nullable=False),
        sa.Column("model", sa.String(length=128), nullable=False, server_default=""),
        sa.Column("render_key", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("text", sa.Text(), nullable=False, server_default=""),
        sa.Column("chars", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("usable", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("warnings", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default="[]"),
        sa.Column("usage", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default="{}"),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["attachment_id"], ["attachments.id"], name="fk_document_pages_attachment", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_document_pages"),
        sa.UniqueConstraint(
            "attachment_id", "sha256", "page_no", "method", "model", "render_key", name="uq_document_pages_key"
        ),
    )
    op.create_index("ix_document_pages_page", "document_pages", ["attachment_id", "page_no"])
    op.create_table(
        "document_page_reads",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("attachment_id", sa.String(length=64), nullable=False),
        sa.Column("session_id", sa.String(length=128), nullable=False),
        sa.Column("page_no", sa.Integer(), nullable=False),
        sa.Column("method", sa.String(length=16), nullable=False, server_default=""),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["attachment_id"], ["attachments.id"], name="fk_document_page_reads_attachment", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_document_page_reads"),
        sa.UniqueConstraint("attachment_id", "session_id", "page_no", name="uq_document_page_reads_key"),
    )
    op.create_index("ix_document_page_reads_session", "document_page_reads", ["attachment_id", "session_id"])


def downgrade() -> None:
    """Drop the two tables and the metadata column."""
    op.drop_index("ix_document_page_reads_session", table_name="document_page_reads")
    op.drop_table("document_page_reads")
    op.drop_index("ix_document_pages_page", table_name="document_pages")
    op.drop_table("document_pages")
    op.drop_column("attachments", "metadata")
