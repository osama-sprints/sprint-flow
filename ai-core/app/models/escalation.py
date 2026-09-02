"""this file contains the escalation model for the application."""

from sqlmodel import Field
from app.models.domain_base import DomainBase


class Escalation(DomainBase, table=True):
    """Escalation represents an escalation in the SprintFlow workspace"""

    id: int | None = Field(default=None, primary_key=True)
    cohort_id: int = Field(foreign_key="cohort.id")
    cohort_membership_id: int = Field(foreign_key="cohortmembership.id")
    conversation_id: str | None = Field(default=None)
    status: str = Field(default="pending")  # pending, resolved or ignored
    reason: str | None = Field(default=None)
    sprint_id: int | None = Field(default=None, foreign_key="sprint.id")
    question: str
    assigned_human_id: int | None = Field(
        default=None,
        foreign_key="user.id",
    )
    original_thread_id: str
    human_dm_thread_id: str | None
