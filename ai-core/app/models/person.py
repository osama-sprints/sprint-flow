"""This file contains the Person class definition."""

from sqlmodel import Field
from app.models.domain_base import DomainBase


class Person(DomainBase, table=True):
    """Person represents an individual in the SprintFlow workspace"""

    id: int | None = Field(default=None, primary_key=True)
    mattermost_user_id: str = Field(unique=True)
    handle: str = Field(unique=True)
    # display_name: str
