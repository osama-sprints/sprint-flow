"""This file contains the ceremony model for the application."""

from sqlmodel import Field
from app.models.domain_base import DomainBase
from datetime import datetime


class Ceremony(DomainBase, table=True):
    """Ceremony represents a ceremony in the SprintFlow workspace"""

    id: int | None = Field(default=None, primary_key=True)
    cohort_id: int = Field(foreign_key="cohort.id")
    type_id: int = Field(foreign_key="ceremonytype.id")
    status: str | None = Field(default=None)
    scheduled_at: datetime | None = Field(default=None)
    duration_mins: int | None = Field(default=None)
