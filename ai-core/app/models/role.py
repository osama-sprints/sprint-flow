"""This file contains the role model for the application."""

from sqlmodel import Field
from app.models.domain_base import DomainBase


class Role(DomainBase, table=True):
    """Role represents a user role in the SprintFlow workspace"""

    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(unique=True)
