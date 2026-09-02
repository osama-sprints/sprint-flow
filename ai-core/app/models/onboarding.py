

from datetime import datetime
from typing import Optional

from sqlmodel import Field, SQLModel


class OnboardingState(SQLModel, table=True):

    __tablename__ = "onboarding_state"

    user_id: str = Field(primary_key=True)
    role: Optional[str] = Field(default=None)
    greeted_at: Optional[datetime] = Field(default=None)
    next_followup_due_at: Optional[datetime] = Field(default=None)
    followups_sent: int = Field(default=0)