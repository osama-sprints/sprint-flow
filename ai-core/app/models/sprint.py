"""Time-boxed sprints per cohort."""

from datetime import date

from sqlalchemy import (
    CheckConstraint,
    Index,
    UniqueConstraint,
    text,
)
from sqlmodel import Field

from app.models.domain_base import DomainBase
from app.models.enums import SprintStatus


class Sprint(DomainBase, table=True):
    """A sprint.

    Dates are calendar dates on purpose: a sprint runs "from Monday to the
    Friday after next" for everyone regardless of timezone, and ceremonies
    inside it carry their own exact instants.

    Attributes:
        id: Primary key.
        cohort_id: The cohort running the sprint.
        name: Unique within the cohort (``Sprint 1``).
        status: ``planned``, ``active`` or ``completed``.
        start_date: First day.
        end_date: Last day, never before ``start_date``.
        opened_by_id: Who opened it.
    """

    __tablename__ = "sprints"  # pyright: ignore[reportAssignmentType]
    __table_args__ = (
        UniqueConstraint("cohort_id", "name", name="uq_sprints_cohort_name"),
        Index("ux_sprints_cohort_name_lower", "cohort_id", text("lower(name)"), unique=True),
        CheckConstraint("end_date >= start_date", name="ck_sprints_dates_ordered"),
    )

    id: int | None = Field(default=None, primary_key=True)
    cohort_id: int = Field(foreign_key="cohorts.id", index=True)
    name: str = Field(max_length=128)
    status: str = Field(default=SprintStatus.PLANNED.value, nullable=False, max_length=32, index=True)
    start_date: date
    end_date: date
    opened_by_id: int | None = Field(default=None, foreign_key="users.id")
