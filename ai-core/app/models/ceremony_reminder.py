"""Idempotency record for ceremony reminders sent by the reminder poller.

One row per (ceremony, recipient, window) means "already sent". The poller
queries this table before every DM and inserts a row after a successful send.
A unique constraint makes a double-insert fail safely — the poller catches
the integrity error and moves on without a second DM going out.

The table is the only guarantee against duplicate sends on restart or when
two processes run in parallel. No other mechanism is needed.
"""

from datetime import datetime

from sqlalchemy import UniqueConstraint
from sqlmodel import Field

from app.models.domain_base import (
    TZ_DATETIME,
    DomainBase,
)


class CeremonyReminder(DomainBase, table=True):
    """One sent reminder — the idempotency guard for the ceremony reminder poller.

    Attributes:
        id: Primary key.
        ceremony_id: The ceremony the reminder is for.
        recipient_mm_id: Mattermost user id of the recipient.
        window: Which reminder window — ``"24h"`` or ``"1h"``.
        sent_at: When the DM was successfully posted (UTC).
    """

    __tablename__ = "ceremony_reminders"  # pyright: ignore[reportAssignmentType]
    __table_args__ = (
        UniqueConstraint(
            "ceremony_id",
            "recipient_mm_id",
            "window",
            name="uq_ceremony_reminder_ceremony_recipient_window",
        ),
    )

    id: int | None = Field(default=None, primary_key=True)
    ceremony_id: int = Field(foreign_key="ceremonies.id", index=True, nullable=False)
    recipient_mm_id: str = Field(index=True, max_length=64, nullable=False)
    window: str = Field(max_length=8, nullable=False)  # "24h" or "1h"
    sent_at: datetime = Field(sa_type=TZ_DATETIME, nullable=False)
