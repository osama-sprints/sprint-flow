"""Who holds which role in which channel — the only place a role is attached to a person."""

from datetime import datetime

from sqlalchemy import UniqueConstraint
from sqlmodel import Field

from app.models.domain_base import (
    TZ_DATETIME,
    DomainBase,
    utcnow,
)
from app.models.enums import MembershipStatus


class ChannelRole(DomainBase, table=True):
    """A person's role inside one channel.

    One row per (user, channel): a person holds exactly one role in a channel and
    may hold a different role in another. Assigning a new role in the same
    channel updates the row rather than adding a second one, so "what is this
    person's role here?" always has one answer.

    Attributes:
        id: Primary key.
        team_id: The Mattermost team ID.
        channel_id: The Mattermost channel ID.
        user_id: The person.
        role_id: The role held in this channel.
        status: ``active`` or ``inactive``; only active roles confer authority.
        joined_at: When the role was created.
        assigned_by_id: Who assigned the current role.
    """

    __tablename__ = "channel_roles"  # pyright: ignore[reportAssignmentType]
    __table_args__ = (UniqueConstraint("user_id", "channel_id", name="uq_channel_roles_user_channel"),)

    id: int | None = Field(default=None, primary_key=True)
    team_id: str = Field(index=True, nullable=False, max_length=64, default="sprints-community")
    channel_id: str = Field(index=True, nullable=False, max_length=64)
    user_id: int = Field(foreign_key="users.id", index=True)
    role_id: int = Field(foreign_key="roles.id")
    status: str = Field(default=MembershipStatus.ACTIVE.value, nullable=False, max_length=32)
    joined_at: datetime = Field(default_factory=utcnow, nullable=False, sa_type=TZ_DATETIME)
    assigned_by_id: int | None = Field(default=None, foreign_key="users.id")
