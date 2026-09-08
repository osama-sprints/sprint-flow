"""Escalation tickets: a learner question, the human it went to, and both conversations."""

from uuid import uuid4

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models import (
    EscalationTicket,
    utcnow,
)
from app.models.enums import (
    EscalationStatus,
    EscalationType,
)
from app.services.database import session_scope


def format_ticket_ref(ticket_id: int) -> str:
    """Render the stable human-facing reference for a ticket id.

    Args:
        ticket_id: ``escalation_tickets.id``.

    Returns:
        str: ``ESC-000042``.
    """
    return f"ESC-{ticket_id:06d}"


async def create_escalation_ticket(
    *,
    cohort_id: int,
    learner_id: int,
    ticket_type: EscalationType,
    question: str,
    learner_channel_id: str,
    learner_thread_id: str,
    assigned_human_id: int | None = None,
    sprint_id: int | None = None,
    session: AsyncSession | None = None,
) -> EscalationTicket:
    """Open a ticket and allocate its reference.

    Args:
        cohort_id: The cohort the learner asked in.
        learner_id: Who asked.
        ticket_type: ``tech`` or ``ops``.
        question: The question, verbatim.
        learner_channel_id: Channel of the learner's conversation.
        learner_thread_id: Root post id the answer must be posted under.
        assigned_human_id: The human it is routed to, if already known.
        sprint_id: The sprint in progress, if any.
        session: Optional session to reuse.

    Returns:
        EscalationTicket: The stored row with its ``ticket_ref`` set.
    """
    ticket = EscalationTicket(
        ticket_ref=f"PENDING-{uuid4().hex[:12]}",
        cohort_id=cohort_id,
        learner_id=learner_id,
        assigned_human_id=assigned_human_id,
        ticket_type=ticket_type.value,
        question=question,
        learner_channel_id=learner_channel_id,
        learner_thread_id=learner_thread_id,
        sprint_id=sprint_id,
    )
    async with session_scope(session) as s:
        s.add(ticket)
        await s.flush()
        assert ticket.id is not None
        ticket.ticket_ref = format_ticket_ref(ticket.id)
        s.add(ticket)
        await s.flush()
        await s.refresh(ticket)
        return ticket


async def get_escalation_ticket(ticket_ref: str, session: AsyncSession | None = None) -> EscalationTicket | None:
    """Fetch a ticket by its reference.

    Args:
        ticket_ref: ``ESC-000042`` (case-insensitive).
        session: Optional session to reuse.

    Returns:
        EscalationTicket | None: The row, or None.
    """
    async with session_scope(session) as s:
        result = await s.exec(
            select(EscalationTicket).where(EscalationTicket.ticket_ref == ticket_ref.strip().upper())
        )
        return result.first()


async def get_escalation_ticket_by_human_thread(
    human_dm_thread_id: str, session: AsyncSession | None = None
) -> EscalationTicket | None:
    """Correlate a reply in a human's DM thread back to its ticket.

    Args:
        human_dm_thread_id: Root post id of the bot's DM to the human.
        session: Optional session to reuse.

    Returns:
        EscalationTicket | None: The row, or None.
    """
    async with session_scope(session) as s:
        result = await s.exec(
            select(EscalationTicket).where(EscalationTicket.human_dm_thread_id == human_dm_thread_id)
        )
        return result.first()


async def list_escalation_tickets(
    cohort_id: int | None = None,
    *,
    status: EscalationStatus | None = None,
    session: AsyncSession | None = None,
) -> list[EscalationTicket]:
    """Tickets, optionally narrowed by cohort and status, oldest first.

    Args:
        cohort_id: Only this cohort, or all cohorts when None.
        status: Only this status.
        session: Optional session to reuse.

    Returns:
        list[EscalationTicket]: Matching rows.
    """
    statement = select(EscalationTicket).order_by(EscalationTicket.created_at, EscalationTicket.id)  # type: ignore[arg-type]
    if cohort_id is not None:
        statement = statement.where(EscalationTicket.cohort_id == cohort_id)
    if status is not None:
        statement = statement.where(EscalationTicket.status == status.value)
    async with session_scope(session) as s:
        result = await s.exec(statement)
        return list(result.all())


async def set_escalation_status(
    ticket_ref: str,
    status: EscalationStatus,
    *,
    answer: str | None = None,
    raw_human_response: str | None = None,
    assigned_human_id: int | None = None,
    human_dm_channel_id: str | None = None,
    human_dm_thread_id: str | None = None,
    session: AsyncSession | None = None,
) -> EscalationTicket | None:
    """Move a ticket through its lifecycle, stamping ``status_changed_at``.

    Args:
        ticket_ref: The ticket.
        status: New status.
        answer: The answer, when resolving.
        assigned_human_id: The human, when routing.
        human_dm_channel_id: The DM channel, when routing.
        human_dm_thread_id: The DM thread root, when routing.
        session: Optional session to reuse.

    Returns:
        EscalationTicket | None: The updated row, or None when absent.
    """
    async with session_scope(session) as s:
        ticket = await get_escalation_ticket(ticket_ref, session=s)
        if ticket is None:
            return None
        now = utcnow()
        if ticket.status != status.value:
            ticket.status = status.value
            ticket.status_changed_at = now
        if answer is not None:
            ticket.answer = answer
        if raw_human_response is not None:            # <-- new
            ticket.raw_human_response = raw_human_response  # <-- new
        if assigned_human_id is not None:
            ticket.assigned_human_id = assigned_human_id
        if human_dm_channel_id is not None:
            ticket.human_dm_channel_id = human_dm_channel_id
        if human_dm_thread_id is not None:
            ticket.human_dm_thread_id = human_dm_thread_id
        if status == EscalationStatus.RESOLVED:
            ticket.resolved_at = now
        ticket.updated_at = now
        s.add(ticket)
        await s.flush()
        await s.refresh(ticket)
        return ticket


async def list_waiting_tickets_for_human(
    assigned_human_id: int, session: AsyncSession | None = None
) -> list[EscalationTicket]:
    """Every ticket currently waiting on a specific human's decision.
 
    Used by the closure attribution logic to decide whether an unthreaded,
    unreferenced reply is genuinely ambiguous (more than one candidate) or
    simply not an escalation reply at all (zero candidates).
 
    Args:
        assigned_human_id: ``users.id`` of the reviewer.
        session: Optional session to reuse.
 
    Returns:
        list[EscalationTicket]: Tickets with status ``waiting_human`` assigned
        to this person, oldest first.
    """
    async with session_scope(session) as s:
        result = await s.exec(
            select(EscalationTicket)
            .where(
                EscalationTicket.assigned_human_id == assigned_human_id,
                EscalationTicket.status == EscalationStatus.WAITING_HUMAN.value,
            )
            .order_by(EscalationTicket.created_at, EscalationTicket.id)  # type: ignore[arg-type]
        )
        return list(result.all())
 