"""A learner question routed to a human, with both conversations' identifiers."""

from datetime import datetime

from sqlalchemy import (
    Index,
    Text,
    text,
)
from sqlmodel import Field

from app.models.domain_base import (
    TZ_DATETIME,
    DomainBase,
    utcnow,
)
from app.models.enums import EscalationStatus


class EscalationTicket(DomainBase, table=True):
    """One escalation.

    Carries both thread ids because the proxy flow is impossible without them:
    the answer must be posted back into the learner's original thread, and the
    decision arrives in the human's DM thread. Every status transition updates
    ``status_changed_at`` so the overdue chaser can find tickets stuck in
    ``waiting_human``.

    Attributes:
        id: Primary key.
        ticket_ref: Stable human-facing reference (``ESC-000042``).
        team_id: The Mattermost team.
        channel_id: The Mattermost channel the learner asked in.
        learner_id: Who asked.
        assigned_human_id: The human it was routed to, once known.
        ticket_type: ``tech`` or ``ops``.
        status: ``open``, ``waiting_human`` or ``resolved``.
        status_changed_at: When the status last changed.
        question: The learner's question, verbatim.
        answer: The answer posted back, once resolved.
        raw_human_response: The reviewer's decision, verbatim, kept separately from the synthesized `answer` shown to the learner.
        learner_thread_id: Root post id the answer must be posted under.
        human_dm_channel_id: The DM channel opened with the human.
        human_dm_thread_id: Root post id of the bot's DM to the human.
        sprint_id: The sprint in progress at the time, if any.
    """

    __tablename__ = "escalation_tickets"  # pyright: ignore[reportAssignmentType]
    __table_args__ = (
        Index(
            "ux_escalation_tickets_open_thread",
            "learner_thread_id",
            unique=True,
            postgresql_where=text("status <> 'resolved'"),
        ),
    )

    id: int | None = Field(default=None, primary_key=True)
    ticket_ref: str = Field(unique=True, index=True, max_length=32)
    team_id: str = Field(index=True, nullable=False, max_length=64, default="sprints-community")
    channel_id: str = Field(index=True, nullable=False, max_length=64)
    learner_id: int = Field(foreign_key="users.id", index=True)
    assigned_human_id: int | None = Field(default=None, foreign_key="users.id", index=True)
    ticket_type: str = Field(max_length=16)
    status: str = Field(default=EscalationStatus.OPEN.value, nullable=False, max_length=32, index=True)
    status_changed_at: datetime = Field(default_factory=utcnow, nullable=False, sa_type=TZ_DATETIME)
    question: str = Field(sa_type=Text)
    answer: str | None = Field(default=None, sa_type=Text)
    raw_human_response: str | None = Field(default=None, sa_type=Text)
    learner_thread_id: str = Field(max_length=64)
    human_dm_channel_id: str | None = Field(default=None, max_length=64)
    human_dm_thread_id: str | None = Field(default=None, max_length=64)
    sprint_id: int | None = Field(default=None, foreign_key="sprints.id")
