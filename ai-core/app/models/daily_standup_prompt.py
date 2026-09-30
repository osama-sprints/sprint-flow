"""The durable dispatch record behind one proactive daily standup prompt.

The whole "exactly once per learner per local day" guarantee lives here: the
row is created with ``ON CONFLICT DO NOTHING`` on ``(sprint, learner, local
day)`` *before* the DM is ever sent, so two dispatcher passes (or two ai-core
containers) can never prompt the same person twice for the same day.

A prompt then moves through a small status machine, mirrored in
``app/services/standups.py``:

    pending  --local hour reached, DM posted-->  dispatched
    dispatched --reply parsed and stored-->      answered
    dispatched --local day closed, silent-->     missed
    pending --retries exhausted-->               failed

``local_date`` and ``dispatch_at`` are computed in the learner's own IANA zone
(resolved from ``users.timezone``) and stored on the row so the record stays
truthful even if the person later changes their zone.
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
from app.models.enums import StandupPromptStatus


class DailyStandupPrompt(DomainBase, table=True):
    """One day's prompt owed to one learner in one sprint.

    Attributes:
        id: Primary key.
        sprint_id: The sprint the day belongs to.
        learner_id: The person prompted.
        channel_id: The sprint's channel (the scope the prompt was issued for).
        dm_channel_id: The bot<->learner DM channel, set once the prompt is posted.
        local_date: The calendar day the prompt covers, in the learner's zone.
        timezone: The IANA zone ``local_date`` and ``dispatch_at`` were derived from.
        dispatch_at: Earliest instant the DM may go out (UTC, the local hour).
        status: ``pending``, ``dispatched``, ``answered``, ``missed`` or ``failed``.
        prompt_post_id: The Mattermost post id of the sent DM.
        dispatch_count: Posting attempts made.
        last_error: Why the last attempt failed.
    """

    __tablename__ = "daily_standup_prompts"  # pyright: ignore[reportAssignmentType]
    __table_args__ = (
        UniqueConstraint(
            "sprint_id",
            "learner_id",
            "local_date",
            name="uq_daily_standup_prompts_sprint_learner_day",
        ),
    )

    id: int | None = Field(default=None, primary_key=True)
    sprint_id: int = Field(foreign_key="sprints.id", index=True, nullable=False)
    learner_id: int = Field(foreign_key="users.id", index=True, nullable=False)
    channel_id: str = Field(index=True, nullable=False, max_length=64)
    dm_channel_id: str | None = Field(default=None, index=True, max_length=64)
    local_date: date = Field(index=True, nullable=False)
    timezone: str = Field(max_length=64, nullable=False)
    dispatch_at: datetime = Field(sa_type=TZ_DATETIME, nullable=False)
    status: str = Field(default=StandupPromptStatus.PENDING.value, index=True, nullable=False, max_length=32)
    prompt_post_id: str | None = Field(default=None, max_length=128)
    dispatch_count: int = Field(default=0, nullable=False)
    last_error: str | None = Field(default=None, sa_type=Text)
    next_attempt_at: datetime | None = Field(default=None, sa_type=TZ_DATETIME)
    claimed_at: datetime | None = Field(default=None, sa_type=TZ_DATETIME)
    claimed_by: str | None = Field(default=None, max_length=128)
    dispatched_at: datetime | None = Field(default=None, sa_type=TZ_DATETIME)
    answered_at: datetime | None = Field(default=None, sa_type=TZ_DATETIME)
    closed_at: datetime | None = Field(default=None, sa_type=TZ_DATETIME)