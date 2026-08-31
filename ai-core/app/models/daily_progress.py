from sqlmodel import Field, UniqueConstraint
from app.models.domain_base import DomainBase
from datetime import date as Date


class DailyProgress(DomainBase, table=True):
    """DailyProgress represents the daily progress of a person in a sprint"""

    __table_args__ = (UniqueConstraint("sprint_id", "cohort_membership_id", "date"),)

    id: int | None = Field(default=None, primary_key=True)
    sprint_id: int = Field(foreign_key="sprint.id")
    cohort_membership_id: int = Field(foreign_key="cohortmembership.id")
    date: Date = Field(default_factory=Date.today)
    status: str = Field(default="pending")  # pending, completed or skipped
    notes: str | None = Field(default=None)
