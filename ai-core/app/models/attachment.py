"""Durable record of a file a person attached to a message.

The bytes stay in Mattermost; this row keeps what the assistant learned from
them — what the file turned out to be, whether it was accepted, and the text
that was extracted — so a later turn can read the document again through a
tool instead of the model having to carry it in every prompt.

Rows expire. ``expires_at`` is set at intake from the retention setting and a
background sweep deletes what has lapsed, extracted text included.
"""

from datetime import datetime
from typing import (
    Any,
    Dict,
)

from sqlalchemy import (
    BigInteger,
    Index,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field

from app.models.domain_base import (
    TZ_DATETIME,
    DomainBase,
)


class Attachment(DomainBase, table=True):
    """One attached file: identity, verdict, provenance and extracted text.

    Attributes:
        id: The Mattermost file id. Natural key; an upload has exactly one.
        post_id: The post it arrived on.
        channel_id: Where it arrived — the authorisation anchor for reads.
        root_id: Thread root of that post, when it has one.
        session_id: LangGraph thread id of the conversation it belongs to.
        turn_id: The turn that ingested it.
        mattermost_user_id: Who attached it.
        requester_user_id: ``users.id`` of that person, when known.
        name: File name as uploaded.
        extension: Lower-cased extension without the dot.
        claimed_mime: Content type Mattermost recorded.
        detected_mime: Content type derived from the bytes.
        kind: pdf, docx, xlsx, csv, text, markdown or image.
        size_bytes: Size reported by Mattermost.
        sha256: Digest of the bytes that were read.
        status: accepted or rejected.
        rejection_reason: Why it was rejected, when it was.
        extraction: pypdf, pages, docx, openpyxl, csv, text, image or none.
        page_count: Pages (PDF) or sheets (XLSX), when meaningful.
        text: Extracted text, capped at the stored-characters setting.
        text_chars: Length of the text before the cap.
        text_truncated: Whether the cap cut it.
        visual: Whether the content was handed to the model as an image or pages.
        metadata_: Kind-specific facts — for a PDF its title, author, table of
            contents and how many pages carry a text layer.
        expires_at: When the retention sweep may delete this row.
    """

    __tablename__ = "attachments"  # pyright: ignore[reportAssignmentType]
    __table_args__ = (
        Index("ix_attachments_conversation", "session_id", "channel_id"),
        Index("ix_attachments_expires", "expires_at"),
    )

    id: str = Field(primary_key=True, max_length=64)
    post_id: str = Field(default="", max_length=64, index=True)
    channel_id: str = Field(max_length=64)
    root_id: str = Field(default="", max_length=64)
    session_id: str = Field(default="", max_length=128)
    turn_id: str = Field(default="", max_length=64)
    mattermost_user_id: str = Field(default="", max_length=64)
    requester_user_id: int | None = Field(default=None, foreign_key="users.id")

    name: str = Field(max_length=512)
    extension: str = Field(default="", max_length=16)
    claimed_mime: str = Field(default="", max_length=128)
    detected_mime: str = Field(default="", max_length=128)
    kind: str = Field(default="", max_length=32)
    size_bytes: int = Field(default=0, sa_type=BigInteger)
    sha256: str = Field(default="", max_length=64)

    status: str = Field(max_length=32)
    rejection_reason: str | None = Field(default=None, sa_type=Text)
    extraction: str = Field(default="none", max_length=32)
    page_count: int | None = Field(default=None)
    text: str | None = Field(default=None, sa_type=Text)
    text_chars: int = Field(default=0, nullable=False)
    text_truncated: bool = Field(default=False, nullable=False)
    visual: bool = Field(default=False, nullable=False)
    metadata_: Dict[str, Any] = Field(default_factory=dict, sa_type=JSONB, sa_column_kwargs={"name": "metadata"})
    expires_at: datetime | None = Field(default=None, sa_type=TZ_DATETIME)
