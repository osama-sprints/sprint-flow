from datetime import datetime, timedelta, timezone
from typing import Optional , List
from sqlmodel import Field
from sqlmodel import Session, select, func
from app.models.domain_base import DomainBase
from app.models.enums import AnnouncementOutcome
from pydantic import BaseModel

from sqlmodel import Session
from app.schemas.announcement import RecipientAudience

def resolve_audience_by_role(session: Session, cohort_id: int, role_input: str) -> RecipientAudience:
    # 1. Normalise role key
    # 2. Query active users in cohort matching role
    # 3. Return RecipientAudience(resolution_type="role", ...)
    pass

def resolve_audience_by_username(session: Session, cohort_id: int, username: str) -> RecipientAudience:
    # 1. Query user by username
    # 2. Enforce cohort scoping check (must be active member of cohort_id)
    # 3. Return RecipientAudience(resolution_type="individual", ...)
    pass
    
class Announcement(DomainBase, table=True):
    __tablename__ = "announcements"
    id: Optional[int] = Field(default=None, primary_key=True)
    requester_id: int = Field(foreign_key="users.id", index=True)
    cohort_id: int = Field(foreign_key="sprints.id", index=True)    
    resolved_channel_id: str = Field(index=True)
    exact_text: str = Field(description="The exact text of the announcement sent or proposed")
    delivery_mode: str = Field(description="e.g. broadcast or targeted")
    confirmation_status: str = Field(default="pending", description="pending, confirmed, or declined")
    mattermost_post_id: Optional[str] = Field(default=None, description="Set only on successful post")
    outcome: AnnouncementOutcome = Field(default=AnnouncementOutcome.SENT)
    status_changed_at: datetime = Field(default_factory=datetime.utcnow)

def check_rate_limit(session: Session, cohort_id: int, window_minutes: int = 10, max_allowed: int = 1) -> bool:
    cutoff_time = datetime.now(timezone.utc) - timedelta(minutes=window_minutes)
    
    statement = (
        select(func.count(Announcement.id))
        .where(Announcement.cohort_id == cohort_id)
        .where(Announcement.outcome == AnnouncementOutcome.SENT)
        .where(Announcement.status_changed_at >= cutoff_time)
    )
    
    recent_count = session.exec(statement).one()
    return recent_count >= max_allowed