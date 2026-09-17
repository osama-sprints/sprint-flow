"""Append-only log of every standup reply received through the DM transport.

Losslessness lives here: the raw text is kept word-for-word, while the
``daily_standups`` entry derived from it stores only the parsed fields. One row
per Mattermost post id, so a re-delivered WebSocket event (or a message seen
twice across transports) is structurally a no-op.

``outcome`` records what the reply became: ``accepted`` (it filled today's
entry), ``duplicate`` (the day was already answered), or ``late`` (the day had
already closed as missed).
"""

from datetime import (
    date,
    datetime,
)

from sqlalchemy import (
    Text,
    UniqueConstraint,
)
from sqlmodel import Field

from app.models.domain_base import (
    TZ_DATETIME,
    DomainBase,
)
from app.models.enums import StandupReplyOutcome


class StandupReply(DomainBase, table=True):
    """One raw standup reply accepted from a learner's DM.

    Attributes:
        id: Primary key.
        prompt_id: The prompt the reply answered for.
        learner_id: The person who wrote it.
        dm_channel_id: The DM channel it arrived in.
        post_id: The Mattermost post id (unique, the redelivery guard).
        post_root_id: The thread root, when the reply was in-thread.
        raw_text: The message verbatim, before any parsing.
        local_date: The day the reply arrived, in the learner's zone.
        outcome: ``accepted``, ``duplicate`` or ``late``.
        received_at: When it was handled (UTC).
    """

    __tablename__ = "standup_replies"  # pyright: ignore[reportAssignmentType]
    __table_args__ = (
        UniqueConstraint("post_id", name="uq_standup_replies_post_id"),
    )

    id: int | None = Field(default=None, primary_key=True)
    prompt_id: int = Field(foreign_key="daily_standup_prompts.id", index=True, nullable=False)
    learner_id: int = Field(foreign_key="users.id", index=True, nullable=False)
    dm_channel_id: str = Field(index=True, nullable=False, max_length=64)
    post_id: str = Field(max_length=128, nullable=False)
    post_root_id: str | None = Field(default=None, max_length=128)
    raw_text: str = Field(sa_type=Text, nullable=False)
    local_date: date | None = Field(default=None)
    outcome: str = Field(
        default=StandupReplyOutcome.ACCEPTED.value,
        index=True,
        nullable=False,
        max_length=32,
    )
    received_at: datetime = Field(sa_type=TZ_DATETIME, nullable=False)