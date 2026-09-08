# ai-core/alembic/versions/0003_add_policy_document_chunks.py
"""Add policy_document_chunks table for vector ingestion.

Revision ID: 0003_policy_chunks
Revises: 0002_sprints_name_ci
Create Date: 2026-09-06
"""

from typing import Sequence, Union
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from alembic import op
from pgvector.sqlalchemy import Vector

revision: str = "0003_policy_chunks"
down_revision: Union[str, Sequence[str], None] = "0002_sprints_name_ci"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TZ = sa.DateTime(timezone=True)

def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector;")
    op.create_table(
        "policy_document_chunks",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("document_id", sa.String(length=255), nullable=False),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("audience", sa.String(length=50), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("embedding", Vector(1536), nullable=False),
        sa.Column("metadata", postgresql.JSON(astext_type=sa.Text()), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", TZ, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", TZ, nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id", name="pk_policy_document_chunks"),
    )
    op.create_index("ix_policy_chunks_doc_id", "policy_document_chunks", ["document_id"], unique=False)
    op.create_index("ix_policy_chunks_audience", "policy_document_chunks", ["audience"], unique=False)

def downgrade() -> None:
    op.drop_index("ix_policy_chunks_audience", table_name="policy_document_chunks")
    op.drop_index("ix_policy_chunks_doc_id", table_name="policy_document_chunks")
    op.drop_table("policy_document_chunks")