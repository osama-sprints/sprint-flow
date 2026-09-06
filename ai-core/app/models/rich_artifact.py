"""Durable records for agent-authored artifacts.

An artifact outlives the turn that made it, for three reasons:

* the browser fetches large content (React source, chart rows) by id rather
  than carrying it in post props;
* an image is generated after its reply is already published, so the row is
  also the job — claimed with a lease exactly like the onboarding outbox;
* publication must survive a crash. ``turn_id`` identifies the turn that staged
  the artifact and is stamped into the post's props, so a restart can find an
  already-published reply instead of posting it twice.

Ownership columns (``requester_user_id``, ``channel_id``, ``root_id``) are
copied from the turn's trusted context. They are what the server plugin
authorises a viewer against; they are never taken from a model argument.
"""

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


class RichArtifact(DomainBase, table=True):
    """One artifact: its content, who may see it, and where it was published.

    Attributes:
        id: Server-generated UUID. An identifier, never an authorisation token.
        turn_id: The turn that staged it. Publication and replay both key on this.
        session_id: LangGraph thread id, for tracing a turn back to its conversation.
        kind: mermaid, chart, react or image.
        schema_version: Envelope version the content was written for.
        revision: Incremented whenever content changes after publication.
        status: pending, running, ready or failed.
        title: Card heading.
        description: Card caption.
        error: Why it failed, when status is failed.
        content: The kind-specific payload.
        requester_user_id: ``users.id`` of the person the turn answered.
        mattermost_user_id: Their Mattermost id.
        channel_id: Channel the reply belongs to — the authorisation anchor.
        root_id: Thread root of the reply, when it has one.
        post_id: The published post, once it exists.
        file_ids: Mattermost file ids attached to the reply.
        published_at: When the reply was published.
        operation_id: Stable key for the external side effect (image generation),
            so a replayed turn reuses the result instead of paying again.
        attempt_count: Generation attempts so far.
        next_attempt_at: Earliest retry after a failure.
        claimed_at: Lease start; a crashed worker releases it by timeout.
        claimed_by: Worker holding the lease.
        last_error: Most recent failure, for operators.
    """

    __tablename__ = "rich_artifacts"  # pyright: ignore[reportAssignmentType]
    __table_args__ = (
        # Named explicitly: an anonymous constraint cannot be referred to in a
        # later migration, and the schema tests refuse unnamed ones.
        UniqueConstraint("operation_id", name="uq_rich_artifacts_operation"),
        Index("ix_rich_artifacts_turn", "turn_id"),
        Index("ix_rich_artifacts_pending", "status", "next_attempt_at"),
    )

    id: str = Field(primary_key=True, max_length=64)
    turn_id: str = Field(max_length=64, index=True)
    session_id: str = Field(default="", max_length=128)

    kind: str = Field(max_length=32)
    schema_version: int = Field(default=1, nullable=False)
    revision: int = Field(default=1, nullable=False)
    status: str = Field(default="ready", max_length=32, index=True)
    title: str = Field(default="", max_length=200)
    description: str = Field(default="", max_length=500)
    error: str | None = Field(default=None, sa_type=Text)
    content: Dict[str, Any] = Field(default_factory=dict, sa_type=JSONB)

    requester_user_id: int | None = Field(default=None, foreign_key="users.id", index=True)
    mattermost_user_id: str = Field(default="", max_length=64)
    channel_id: str = Field(default="", max_length=64, index=True)
    root_id: str = Field(default="", max_length=64)
    post_id: str | None = Field(default=None, max_length=64, index=True)
    file_ids: List[str] = Field(default_factory=list, sa_type=JSONB)
    published_at: datetime | None = Field(default=None, sa_type=TZ_DATETIME)

    operation_id: str | None = Field(default=None, max_length=128)
    attempt_count: int = Field(default=0, nullable=False)
    next_attempt_at: datetime | None = Field(default=None, sa_type=TZ_DATETIME)
    claimed_at: datetime | None = Field(default=None, sa_type=TZ_DATETIME)
    claimed_by: str | None = Field(default=None, max_length=128)
    last_error: str | None = Field(default=None, sa_type=Text)
