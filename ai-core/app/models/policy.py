from datetime import datetime
from typing import Any, Dict
from sqlmodel import SQLModel, Field, Column, JSON, DateTime
from pgvector.sqlalchemy import Vector
from app.models.domain_base import utcnow

class PolicyDocumentChunk(SQLModel, table=True):
    __tablename__ = "policy_document_chunks"

    id: str = Field(primary_key=True, description="SHA256 hash of doc_id, index, and content")
    document_id: str = Field(index=True, nullable=False)
    chunk_index: int = Field(nullable=False)
    audience: str = Field(index=True, nullable=False)  # 'learner' or 'internal_operator'
    content: str = Field(nullable=False)
    embedding: Any = Field(sa_column=Column(Vector(1536), nullable=False))
    doc_metadata: Dict[str, Any] = Field(default_factory=dict, sa_column=Column("metadata", JSON, nullable=False))
    content_hash: str = Field(nullable=False)
    created_at: datetime = Field(default_factory=utcnow, sa_column=Column(DateTime(timezone=True), nullable=False))
    updated_at: datetime = Field(default_factory=utcnow, sa_column=Column(DateTime(timezone=True), nullable=False))