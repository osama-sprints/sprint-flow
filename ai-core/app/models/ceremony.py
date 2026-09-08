"""A scheduled ceremony — the record the reminder and reporting tasks consume."""

from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    Text,
)
from sqlmodel import Field

from app.models.domain_base import (
    TZ_DATETIME,
    DomainBase,
)
from app.models.enums import CeremonyStatus


class Ceremony(DomainBase, table=True):
    """One scheduled ceremony.

    ``scheduled_at`` is the unambiguous instant (timezone-aware, stored in UTC).
    ``time_zone`` and ``time_expression`` record how that instant was arrived at
    so an amendment can be understood later; consumers never need to re-parse
    anything.

    Attributes:
        id: Primary key.
        cohort_id: The cohort the ceremony belongs to.
        sprint_id: The sprint it belongs to, when one was named.
        ceremony_type_id: The kind of ceremony.
        organizer_id: The person who scheduled it (resolved from stored identity).
        scheduled_at: The start instant.
        duration_minutes: Length; conflicts are computed from start + duration.
        agenda: Free text shown in reminders.
        notes: Free text added after the fact (outcomes, links).
        status: ``scheduled``, ``cancelled`` or ``completed``.
        time_expression: What the organiser typed (``tomorrow at 2pm``).
        time_zone: IANA zone the expression was interpreted in.
        channel_id: Mattermost channel reminders should be posted to, if any.
    """

    __tablename__ = "ceremonies"  # pyright: ignore[reportAssignmentType]
    __table_args__ = (CheckConstraint("duration_minutes > 0", name="ck_ceremonies_duration_positive"),)

    id: int | None = Field(default=None, primary_key=True)
    # Scoping for distributed/channel-native design
    team_id: str = Field(index=True, nullable=False, max_length=64, default="sprints-community")
    channel_id: str = Field(index=True, nullable=False, max_length=64)
    
    ceremony_type_id: int = Field(foreign_key="ceremony_types.id")
    organizer_id: int = Field(foreign_key="users.id", index=True)
    scheduled_at: datetime = Field(sa_type=TZ_DATETIME, nullable=False, index=True)
    duration_minutes: int = Field(default=60, nullable=False)
    agenda: str | None = Field(default=None, sa_type=Text)
    notes: str | None = Field(default=None, sa_type=Text)
    status: str = Field(default=CeremonyStatus.SCHEDULED.value, nullable=False, max_length=32, index=True)
    time_expression: str | None = Field(default=None, max_length=512)
    time_zone: str | None = Field(default=None, max_length=64)
    # Google Meet join URL, set after a Meet event is created for this ceremony.
    meet_link: str | None = Field(default=None, max_length=512)
