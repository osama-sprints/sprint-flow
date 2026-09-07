"""Audit trail for ceremony changes, so an accidental reschedule can be understood afterwards."""

from sqlalchemy import Text
from sqlmodel import Field

from app.models.domain_base import DomainBase


class CeremonyAmendment(DomainBase, table=True):
    """One changed field on one ceremony.

    Attributes:
        id: Primary key.
        ceremony_id: The ceremony that changed.
        amended_by_id: Who changed it (resolved from stored identity).
        field: Column name that changed (``scheduled_at``, ``agenda``, ``status``...).
        old_value: Previous value rendered as text.
        new_value: New value rendered as text.
        reason: Free text the requester gave, if any.
    """

    __tablename__ = "ceremony_amendments"  # pyright: ignore[reportAssignmentType]

    id: int | None = Field(default=None, primary_key=True)
    ceremony_id: int = Field(foreign_key="ceremonies.id", index=True)
    amended_by_id: int = Field(foreign_key="users.id")
    field: str = Field(max_length=64)
    old_value: str | None = Field(default=None, sa_type=Text)
    new_value: str | None = Field(default=None, sa_type=Text)
    reason: str | None = Field(default=None, sa_type=Text)
