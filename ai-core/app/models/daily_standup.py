"""Per-person daily progress inside a sprint."""

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


class DailyStandup(DomainBase, table=True):
    """One learner's standup entry for one day of one sprint.

    The three explicit fields exist because the standup-summary task needs to
    highlight blockers without parsing free text. When the entry was collected
    through the proactive daily prompt, ``prompt_id`` points back at the prompt
    row, ``raw_response`` preserves the reply verbatim and ``submitted_at`` /
    ``timezone`` record when and in which zone it arrived — so the stored entry
    is never better than the raw material it was built from.

    Attributes:
        id: Primary key.
        sprint_id: The sprint the entry belongs to.
        learner_id: The person who submitted it.
        log_date: The calendar day it covers.
        what_i_did: Progress since the previous entry.
        what_i_will_do: Plan until the next one.
        blockers: Anything in the way, or None.
        prompt_id: The daily_standup_prompts row the reply answered, if any.
        raw_response: The raw reply text the entry was parsed from, if any.
        submitted_at: When the reply arrived, if collected proactively.
        timezone: The zone the reply was attributed to, if collected proactively.
    """

    __tablename__ = "daily_standups"  # pyright: ignore[reportAssignmentType]
    __table_args__ = (
        UniqueConstraint("sprint_id", "learner_id", "log_date", name="uq_daily_standups_sprint_learner_day"),
        UniqueConstraint("prompt_id", name="uq_daily_standups_prompt"),
    )

    id: int | None = Field(default=None, primary_key=True)
    sprint_id: int = Field(foreign_key="sprints.id", index=True)
    learner_id: int = Field(foreign_key="users.id", index=True)
    log_date: date = Field(index=True)
    what_i_did: str = Field(sa_type=Text)
    what_i_will_do: str = Field(sa_type=Text)
    blockers: str | None = Field(default=None, sa_type=Text)
    prompt_id: int | None = Field(default=None, foreign_key="daily_standup_prompts.id", index=True)
    raw_response: str | None = Field(default=None, sa_type=Text)
    submitted_at: datetime | None = Field(default=None, sa_type=TZ_DATETIME)
    timezone: str | None = Field(default=None, max_length=64)
