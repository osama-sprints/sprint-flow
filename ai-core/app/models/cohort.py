"""Cohort represents a group of individuals in the SprintFlow workspace"""

from datetime import datetime
from sqlalchemy import Column, DateTime
from sqlmodel import Field
from app.models.domain_base import DomainBase


class Cohort(DomainBase, table=True):
    id: int | None = Field(default=None, primary_key=True)
    name: str
    status: str = Field(default="active")
    starts_at: datetime | None = Field(default=None, sa_column=Column(DateTime(timezone=True), nullable=True),)
    ends_at: datetime | None = Field(default=None,sa_column=Column(DateTime(timezone=True), nullable=True),)
