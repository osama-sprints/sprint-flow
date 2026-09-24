from datetime import datetime
from typing import Optional

from sqlalchemy import Text
from sqlmodel import Field

from app.models.domain_base import (
    TZ_DATETIME,
    DomainBase,
    utcnow,
)
from app.models.enums import AnnouncementOutcome


class Announcement(DomainBase, table=True):
    __tablename__ = "announcements"  # pyright: ignore[reportAssignmentType]

    id: Optional[int] = Field(default=None, primary_key=True)
    requester_id: int = Field(foreign_key="users.id", index=True)
    team_id: str = Field(index=True, nullable=False, max_length=64, default="sprints-community")
    channel_id: str = Field(index=True, nullable=False, max_length=64)
    resolved_channel_id: str = Field(index=True, nullable=False, max_length=64)
    exact_text: str = Field(sa_type=Text, description="The exact text of the announcement sent or proposed")
    delivery_mode: str = Field(max_length=32, description="e.g. broadcast or targeted")
    confirmation_status: str = Field(default="pending", nullable=False, max_length=32, description="pending, confirmed, or declined")
    mattermost_post_id: Optional[str] = Field(default=None, max_length=64, description="Set only on successful post")
    outcome: str = Field(default=AnnouncementOutcome.PENDING.value, nullable=False, max_length=32)
    status_changed_at: datetime = Field(default_factory=utcnow, nullable=False, sa_type=TZ_DATETIME)