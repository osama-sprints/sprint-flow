"""File contains base model for the SprintFlow domain specific models"""

from datetime import datetime, UTC
from sqlmodel import Field, SQLModel


class DomainBase(SQLModel):
    """Domain base model with common fields."""

    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        sa_column_kwargs={"onupdate": lambda: datetime.now(UTC)},
    )
