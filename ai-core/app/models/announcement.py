from datetime import datetime
from typing import Optional

from sqlmodel import Field

from app.models.domain_base import utcnow
from app.models.domain_base import (
    TZ_DATETIME,
    DomainBase,
)
from app.models.enums import AnnouncementOutcome


class Announcement(DomainBase, table=True):

    __tablename__ = "announcements"  

    id: Optional[int] = Field(default=None, primary_key=True)
    requester_id: int = Field(foreign_key="users.id", index=True)
    cohort_id: int = Field(foreign_key="sprints.id", index=True)
    resolved_channel_id: str = Field(index=True)
    exact_text: str = Field(description="The exact text of the announcement sent or proposed")
    delivery_mode: str = Field(description="e.g. broadcast or targeted")
    confirmation_status: str = Field(default="pending", description="pending, confirmed, cancelled, or refused")
    mattermost_post_id: Optional[str] = Field(default=None, description="Set only on successful post")
    outcome: AnnouncementOutcome = Field(default=AnnouncementOutcome.PENDING)
    status_changed_at: datetime = Field(default_factory=utcnow, sa_type=TZ_DATETIME)

