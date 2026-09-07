"""Per-page cache of what was read from a document, and who read what.

``document_pages`` holds one row per (document revision, page, method, model,
rendering settings): the native text layer extracted once, and each
transcription a vision model produced. A page is rendered and transcribed
at most once per model and settings; every later request is a cache hit.

``document_page_reads`` records which pages a conversation has actually been
shown, so "summarise the whole document" can resume from the first unread
page and the agent can say what it has and has not covered.

Both cascade from ``attachments``: when retention deletes the file's record,
its pages and reads go with it.
"""

import uuid
from datetime import datetime
from typing import (
    Any,
    Dict,
    List,
)

from sqlalchemy import (
    Index,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field

from app.models.domain_base import (
    TZ_DATETIME,
    DomainBase,
)


class DocumentPage(DomainBase, table=True):
    """Text obtained from one page of one document revision by one method.

    Attributes:
        id: Server-generated UUID.
        attachment_id: The document.
        sha256: Revision of the bytes the page came from.
        page_no: Physical page number, 1-based.
        label: Printed page label, when the document defines labels.
        method: native (text layer) or vision (rendered and transcribed).
        model: Model that transcribed it; empty for native.
        render_key: Rendering settings used; empty for native.
        text: The text.
        chars: Visible characters in ``text``.
        usable: Whether ``text`` is good enough to answer from.
        warnings: Notes about the page (unreadable regions, weak text layer).
        usage: Token usage and cost of the transcription, when reported.
        latency_ms: Wall time of the transcription.
    """

    __tablename__ = "document_pages"  # pyright: ignore[reportAssignmentType]
    __table_args__ = (
        UniqueConstraint(
            "attachment_id", "sha256", "page_no", "method", "model", "render_key", name="uq_document_pages_key"
        ),
        Index("ix_document_pages_page", "attachment_id", "page_no"),
    )

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True, max_length=64)
    attachment_id: str = Field(foreign_key="attachments.id", max_length=64, ondelete="CASCADE")
    sha256: str = Field(default="", max_length=64)
    page_no: int = Field(nullable=False)
    label: str | None = Field(default=None, max_length=64)
    method: str = Field(max_length=16)
    model: str = Field(default="", max_length=128)
    render_key: str = Field(default="", max_length=64)
    text: str = Field(default="", sa_type=Text)
    chars: int = Field(default=0, nullable=False)
    usable: bool = Field(default=False, nullable=False)
    warnings: List[str] = Field(default_factory=list, sa_type=JSONB)
    usage: Dict[str, Any] = Field(default_factory=dict, sa_type=JSONB)
    latency_ms: int | None = Field(default=None)


class DocumentPageRead(DomainBase, table=True):
    """A page shown to the model in a conversation.

    Attributes:
        id: Server-generated UUID.
        attachment_id: The document.
        session_id: The conversation.
        page_no: Physical page number, 1-based.
        method: How the text shown was obtained.
        read_at: When it was shown, most recently.
    """

    __tablename__ = "document_page_reads"  # pyright: ignore[reportAssignmentType]
    __table_args__ = (
        UniqueConstraint("attachment_id", "session_id", "page_no", name="uq_document_page_reads_key"),
        Index("ix_document_page_reads_session", "attachment_id", "session_id"),
    )

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True, max_length=64)
    attachment_id: str = Field(foreign_key="attachments.id", max_length=64, ondelete="CASCADE")
    session_id: str = Field(max_length=128)
    page_no: int = Field(nullable=False)
    method: str = Field(default="", max_length=16)
    read_at: datetime | None = Field(default=None, sa_type=TZ_DATETIME)
