"""This file contains the ceremony model for the application."""
from sqlalchemy import Column, DateTime
from sqlmodel import Field
from app.models.domain_base import DomainBase
from datetime import datetime


class Ceremony(DomainBase, table=True):
    """Ceremony represents a ceremony in the SprintFlow workspace"""

    id: int | None = Field(default=None, primary_key=True)
    cohort_id: int = Field(foreign_key="cohort.id", index=True)
    type_id: int = Field(foreign_key="ceremonytype.id")
    status: str = Field(default="scheduled",nullable=False,index=True,)
    scheduled_at: datetime = Field(sa_column=Column(DateTime(timezone=True),nullable=False,index=True,))
    duration_mins: int | None = Field(default=None)

    organizer: str = Field(index=True)

    agenda: str | None = Field(default=None)

    channel_id: str | None = Field(default=None)

    raw_input: str | None = Field(default=None)
