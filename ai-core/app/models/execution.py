"""Durable record of one agent turn: what is running, for whom, and how it ended.

A turn used to exist only as an asyncio task; when it took long, nothing told
the person, and nothing could stop or repeat it. This row is the state the
person can see and act on. It is written when the turn starts, touched as it
progresses (``step``, ``heartbeat_at``), and closed with a status. Cancel is a
request recorded here and honoured by the running turn — which may be in
another process — and retry re-runs the stored trigger.
"""

from datetime import datetime
from typing import (
    Any,
    Dict,
)

from sqlalchemy import (
    Index,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field

from app.models.domain_base import (
    TZ_DATETIME,
    DomainBase,
)


class Execution(DomainBase, table=True):
    """One turn of the assistant, from trigger to outcome.

    Attributes:
        id: The turn id — the same id rich artifacts are staged under.
        session_id: LangGraph thread id of the conversation.
        channel_id: Where the trigger arrived.
        root_id: Thread root the turn replies into, when it has one.
        trigger_post_id: The post that started the turn.
        source: Transport label (websocket, webhook, retry).
        mattermost_user_id: Who asked.
        requester_user_id: ``users.id`` of that person, when known.
        status: running, succeeded, failed or cancelled.
        step: Last progress note shown to the person.
        attempt: 1 for a first run, incremented by each retry.
        retry_of: The execution this one re-runs, when it is a retry.
        cancel_requested_at: When a cancel was asked for.
        cancel_requested_by: Mattermost id of who asked.
        started_at: When the turn began.
        heartbeat_at: Last time the running turn touched the row.
        finished_at: When it ended.
        error: Why it failed, when it did.
        reply_post_id: The reply, once posted.
        trigger: The normalised inbound message, so a retry can re-run it.
        worker: Process that ran it, for stale detection after a crash.
    """

    __tablename__ = "executions"  # pyright: ignore[reportAssignmentType]
    __table_args__ = (
        Index("ix_executions_session", "session_id", "created_at"),
        Index("ix_executions_status", "status"),
    )

    id: str = Field(primary_key=True, max_length=64)
    session_id: str = Field(default="", max_length=128)
    channel_id: str = Field(max_length=64)
    root_id: str = Field(default="", max_length=64)
    trigger_post_id: str = Field(default="", max_length=64, index=True)
    source: str = Field(default="", max_length=32)
    mattermost_user_id: str = Field(default="", max_length=64)
    requester_user_id: int | None = Field(default=None, foreign_key="users.id")

    status: str = Field(default="running", max_length=32)
    step: str = Field(default="", max_length=200)
    attempt: int = Field(default=1, nullable=False)
    retry_of: str | None = Field(default=None, max_length=64)
    cancel_requested_at: datetime | None = Field(default=None, sa_type=TZ_DATETIME)
    cancel_requested_by: str = Field(default="", max_length=64)
    started_at: datetime | None = Field(default=None, sa_type=TZ_DATETIME)
    heartbeat_at: datetime | None = Field(default=None, sa_type=TZ_DATETIME)
    finished_at: datetime | None = Field(default=None, sa_type=TZ_DATETIME)
    error: str | None = Field(default=None, sa_type=Text)
    reply_post_id: str | None = Field(default=None, max_length=64)
    trigger: Dict[str, Any] = Field(default_factory=dict, sa_type=JSONB)
    worker: str = Field(default="", max_length=128)
