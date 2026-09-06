"""Shared base for every SprintFlow domain model.

Both timestamps are timezone-aware UTC instants. Later tasks schedule reminders
from stored times across timezones, so a naive timestamp is never acceptable.
"""

from datetime import (
    UTC,
    datetime,
)
from typing import (
    Any,
    cast,
)

from sqlalchemy import DateTime
from sqlmodel import (
    Field,
    SQLModel,
)

# The one column type for instants. SQLModel's ``sa_type`` is annotated as a
# class but accepts a configured type instance; the cast keeps pyright quiet
# without weakening the runtime behaviour (timestamptz everywhere).
TZ_DATETIME: type[Any] = cast(type[Any], DateTime(timezone=True))


def utcnow() -> datetime:
    """Return the current instant as a timezone-aware UTC datetime.

    Returns:
        datetime: Now, in UTC, with tzinfo set.
    """
    return datetime.now(UTC)


def require_aware(value: datetime, field: str) -> datetime:
    """Refuse a naive datetime before it reaches a ``timestamptz`` column.

    PostgreSQL would silently interpret a naive value in the session's zone,
    which makes the stored instant depend on server configuration. Every
    data-access function that stores an instant calls this first.

    Args:
        value: The datetime a caller supplied.
        field: The column or argument name, for the error message.

    Returns:
        datetime: ``value`` unchanged when it carries an offset.

    Raises:
        ValueError: If ``value`` is naive.
    """
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value


class DomainBase(SQLModel):
    """Common audit columns for domain tables."""

    created_at: datetime = Field(
        default_factory=utcnow,
        nullable=False,
        sa_type=TZ_DATETIME,
    )
    updated_at: datetime = Field(
        default_factory=utcnow,
        nullable=False,
        sa_type=TZ_DATETIME,
        sa_column_kwargs={"onupdate": utcnow},
    )
