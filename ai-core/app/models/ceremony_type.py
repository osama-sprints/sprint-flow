"""This file contains the ceremony type model for the application."""

from sqlmodel import Field
from app.models.domain_base import DomainBase


class CeremonyType(DomainBase, table=True):
    """CeremonyType represents a type of ceremony in SprintFlow."""

    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(unique=True)
