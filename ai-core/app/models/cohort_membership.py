"""This file contains the cohort membership model for the application."""

from datetime import UTC, datetime
from sqlmodel import Field, UniqueConstraint
from app.models.domain_base import DomainBase


class CohortMembership(DomainBase, table=True):
    """CohortMembership represents the association between a person and a cohort."""

    __table_args__ = (UniqueConstraint("person_id", "cohort_id"),)

    id: int | None = Field(default=None, primary_key=True)
    cohort_id: int = Field(foreign_key="cohort.id")
    person_id: int = Field(foreign_key="person.id")
    role_id: int = Field(foreign_key="role.id")
    status: str = Field(default="active")
    joined_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
