"""Who holds which role in which cohort — the only place a role is attached to a person."""

from datetime import datetime

from sqlalchemy import UniqueConstraint
from sqlmodel import Field

from app.models.domain_base import (
    TZ_DATETIME,
    DomainBase,
    utcnow,
)
from app.models.enums import MembershipStatus


class CohortMembership(DomainBase, table=True):
    """A person's role inside one cohort.

    One row per (user, cohort): a person holds exactly one role in a cohort and
    may hold a different role in another. Assigning a new role in the same
    cohort updates the row rather than adding a second one, so "what is this
    person's role here?" always has one answer.

    Attributes:
        id: Primary key.
        cohort_id: The cohort.
        user_id: The person.
        role_id: The role held in this cohort.
        status: ``active`` or ``inactive``; only active memberships confer authority.
        joined_at: When the membership was created.
        assigned_by_id: Who assigned the current role.
    """

    __tablename__ = "cohort_memberships"  # pyright: ignore[reportAssignmentType]
    __table_args__ = (UniqueConstraint("user_id", "cohort_id", name="uq_cohort_memberships_user_cohort"),)

    id: int | None = Field(default=None, primary_key=True)
    cohort_id: int = Field(foreign_key="cohorts.id", index=True)
    user_id: int = Field(foreign_key="users.id", index=True)
    role_id: int = Field(foreign_key="roles.id")
    status: str = Field(default=MembershipStatus.ACTIVE.value, nullable=False, max_length=32)
    joined_at: datetime = Field(default_factory=utcnow, nullable=False, sa_type=TZ_DATETIME)
    assigned_by_id: int | None = Field(default=None, foreign_key="users.id")
