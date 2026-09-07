"""A cohort: the group of people who work together through a programme."""

from datetime import (
    date,
    datetime,
)

from sqlalchemy import (
    Index,
    text,
)
from sqlmodel import Field

from app.models.domain_base import (
    TZ_DATETIME,
    DomainBase,
)


class Cohort(DomainBase, table=True):
    """A cohort and its kill switch.

    ``is_active`` is the switch every scheduled job filters on: an inactive
    cohort receives no onboarding, reminders or standups.

    Attributes:
        id: Primary key.
        name: Human name, unique case-insensitively (``Backend-01``).
        mattermost_team_id: The Mattermost team the cohort lives in, when known.
        mattermost_channel_id: The cohort's channel for reminders and announcements.
        is_active: The kill switch.
        starts_on: First calendar day of the programme, if known.
        ends_on: Last calendar day of the programme, if known.
        created_by_id: The superadmin who created it.
        deactivated_at: When the kill switch was thrown.
    """

    __tablename__ = "cohorts"  # pyright: ignore[reportAssignmentType]
    __table_args__ = (Index("ux_cohorts_name_lower", text("lower(name)"), unique=True),)

    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(max_length=128)
    mattermost_team_id: str | None = Field(default=None, max_length=64, index=True)
    mattermost_channel_id: str | None = Field(default=None, max_length=64)
    is_active: bool = Field(default=True, nullable=False, index=True)
    starts_on: date | None = Field(default=None)
    ends_on: date | None = Field(default=None)
    created_by_id: int | None = Field(default=None, foreign_key="users.id")
    deactivated_at: datetime | None = Field(default=None, sa_type=TZ_DATETIME)
