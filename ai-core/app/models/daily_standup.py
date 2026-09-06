"""Per-person daily progress inside a sprint."""

from datetime import date

from sqlalchemy import (
    Text,
    UniqueConstraint,
)
from sqlmodel import Field

from app.models.domain_base import DomainBase


class DailyStandup(DomainBase, table=True):
    """One learner's standup entry for one day of one sprint.

    The three explicit fields exist because the standup-summary task needs to
    highlight blockers without parsing free text.

    Attributes:
        id: Primary key.
        sprint_id: The sprint the entry belongs to.
        learner_id: The person who submitted it.
        log_date: The calendar day it covers.
        what_i_did: Progress since the previous entry.
        what_i_will_do: Plan until the next one.
        blockers: Anything in the way, or None.
    """

    __tablename__ = "daily_standups"  # pyright: ignore[reportAssignmentType]
    __table_args__ = (
        UniqueConstraint("sprint_id", "learner_id", "log_date", name="uq_daily_standups_sprint_learner_day"),
    )

    id: int | None = Field(default=None, primary_key=True)
    sprint_id: int = Field(foreign_key="sprints.id", index=True)
    learner_id: int = Field(foreign_key="users.id", index=True)
    log_date: date = Field(index=True)
    what_i_did: str = Field(sa_type=Text)
    what_i_will_do: str = Field(sa_type=Text)
    blockers: str | None = Field(default=None, sa_type=Text)
