"""This file contains the sprint model for the application."""

from datetime import datetime


from sqlmodel import Field
from app.models.domain_base import DomainBase


class Sprint(DomainBase, table=True):
    """Sprint represents a sprint in the SprintFlow workspace"""

    id: int | None = Field(default=None, primary_key=True)
    cohort_id: int = Field(foreign_key="cohort.id")
    name: str
    status: str = Field(default="planned")  # planned, active, completed
    starts_at: datetime | None = Field(default=None)
    ends_at: datetime | None = Field(default=None)
