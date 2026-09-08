"""Escalation handoff: quietly put a learner's ungrounded question in a human's hands.

This is the *send* half of the escalation feature (task 0/1 of the sprint).
The other half — reading a human's terse DM reply, correlating it back to a
ticket, and posting a courteous answer into the learner's thread — lives
elsewhere and consumes exactly what this module writes to
``escalation_tickets`` (see ``app.services.domain.escalations``).

Design, in one paragraph: a learner's message arrives in a cohort's channel.
When the agent has no grounded answer, it calls ``open_escalation``. This
module resolves the cohort from the channel (never from anything the learner
typed), resolves the human from the **stored** cohort-role mapping for a
fixed role (see ``choosing_the_human`` in ``reports/escalation_report.md`` for
why this does not vary per-message), opens a private DM with that human, and
writes an ``EscalationTicket`` carrying both conversations' identifiers. The
learner is told, generically, that a colleague is looking into it — never
who, never that a specific person was messaged.

Every step after cohort/human resolution is best-effort in the sense that it
never raises past this module for reasons outside the learner's control (a
missing human, a failed DM): the ticket is still created and the learner still
gets an honest, non-alarming acknowledgement. See ``missing_human`` in
``reports/escalation_report.md``.
"""

from dataclasses import dataclass
from sqlalchemy.exc import IntegrityError
from app.core.langgraph.tools.results import ResultCode
from app.core.logging import logger
from app.core.requester import RequesterContext
from app.models import EscalationTicket
from app.models.enums import (
    ROLE_LABELS,
    EscalationStatus,
    EscalationType,
    RoleKey,
)
from app.services.authorisation import (
    ValidationFailed,
    require_active_cohort,
    require_requester,
)
from app.services.domain import cohorts as cohort_repo
from app.services.domain import escalations as escalation_repo
from app.services.domain import identity as identity_repo
from app.services.mattermost import mattermost_client

# The role each ticket type is routed to. A cohort holds at most one active
# membership per (user, role), but nothing stops two people from separately
# holding the same role in one cohort; when that happens the earliest-assigned
# holder (list_cohort_members is ordered by joined_at) is used, so routing is
# still deterministic rather than picking whichever the query happens to
# return first.
ROLE_FOR_TICKET_TYPE: dict[EscalationType, RoleKey] = {
    EscalationType.TECH: RoleKey.TECH_LEAD,
    EscalationType.OPS: RoleKey.OPS_SUPPORT,
}

# See `choosing_the_human` in reports/escalation_report.md: this task does not
# split tech/ops:questions are not classified by content, the refusal signal
# carries no category, and splitting is not required by the brief. Every
# escalation routes to the cohort's tech lead. A future caller (e.g. an
# escalation opened from a distinctly operational flow) can still pass
# ticket_type=EscalationType.OPS explicitly — the split is supported end to
# end, just never inferred from a message.
DEFAULT_TICKET_TYPE = EscalationType.TECH


@dataclass(frozen=True)
class EscalationResult:
    """Outcome of ``open_escalation``.

    Attributes:
        code: The result code — always one of the two ``ESCALATION_*`` codes;
            other failures are raised, not returned (see module docstring).
        message: The sentence to relay to the learner, verbatim.
        ticket: The stored ticket, always present when code is an
            ``ESCALATION_*`` code.
    """

    code: ResultCode
    message: str
    ticket: EscalationTicket | None = None


def _dm_prompt(ticket: EscalationTicket, cohort_name: str, learner_label: str, question: str) -> str:
    """Compose the message posted into the human's DM thread.

    Args:
        ticket: The freshly created ticket (for its reference).
        cohort_name: The cohort's display name.
        learner_label: How to refer to the learner (display name or handle).
        question: The learner's question, verbatim.

    Returns:
        str: The DM body.
    """
    return (
        f"**New escalation {ticket.ticket_ref}** from *{cohort_name}*.\n\n"
        f"{learner_label} asked:\n> {question}\n\n"
        f"Reply in this thread with your answer — it will be turned into a reply "
        f"for {learner_label} automatically. No need to mention {ticket.ticket_ref}."
    )


def _no_human_message(ticket: EscalationTicket, role_key: RoleKey) -> str:
    """Compose the honest acknowledgement for a cohort with nobody in the required role.

    Args:
        ticket: The ticket that was still created.
        role_key: The role that has nobody assigned.

    Returns:
        str: The sentence relayed to the learner.
    """
    label = ROLE_LABELS[role_key].lower()
    return (
        f"I don't have a confident answer for that. I've logged it as {ticket.ticket_ref}, but this "
        f"cohort doesn't have a {label} assigned yet, so I can't hand it to someone right now — it's "
        f"on record and will be picked up once one is."
    )


def _handed_off_message(ticket: EscalationTicket) -> str:
    """Compose the honest, name-free acknowledgement once a human has actually been messaged.

    Args:
        ticket: The ticket, now routed and DM'd.

    Returns:
        str: The sentence relayed to the learner.
    """
    return (
        f"I don't have a confident answer for that, so I've looped in a colleague — they'll follow up "
        f"here. (Ref: {ticket.ticket_ref})"
    )


def _assigned_but_unreachable_message(ticket: EscalationTicket) -> str:
    """Compose the honest acknowledgement when a human is assigned but the DM could not be sent.

    Deliberately distinct from ``_handed_off_message``: that sentence would
    claim a colleague has already been contacted, which would not be true
    here — the same honesty requirement that shapes ``_no_human_message``.

    Args:
        ticket: The ticket (assigned, still ``open``).

    Returns:
        str: The sentence relayed to the learner.
    """
    return (
        f"I don't have a confident answer for that. I've logged it as {ticket.ticket_ref} and assigned "
        f"it to the right person, but couldn't reach them just now — it's on record and you'll hear back "
        f"once it's picked up."
    )


def _message_for_existing_ticket(ticket: EscalationTicket, ticket_type: EscalationType) -> str:
    """Compose the acknowledgement for a replayed trigger, matching the existing ticket's real state.

    Used for both the check-then-act idempotency path and the raced-insert
    path, just describing what's already true for this ticket, exactly as if it were freshly opened.

    Args:
        ticket: The pre-existing, non-resolved ticket for this thread.
        ticket_type: The type ``open_escalation`` was called with, only used
            to pick the right role label if the ticket has no human at all.

    Returns:
        str: The sentence relayed to the learner.
    """
    if ticket.status == EscalationStatus.WAITING_HUMAN.value:
        return _handed_off_message(ticket)
    if ticket.assigned_human_id is not None:
        return _assigned_but_unreachable_message(ticket)
    return _no_human_message(ticket, ROLE_FOR_TICKET_TYPE[ticket_type])

async def open_escalation(
    question: str,
    *,
    ticket_type: EscalationType = DEFAULT_TICKET_TYPE,
    requester: RequesterContext | None = None,
) -> EscalationResult:
    """Route an ungrounded learner question to the cohort's designated human, privately.

    The human is resolved strictly from the stored cohort-role mapping
    (``cohort_memberships`` joined to ``roles``) for the role ``ticket_type``
    maps to — never from ``question`` or any other message content. When the
    cohort has nobody in that role, the ticket is still created (unassigned)
    and the learner is told honestly rather than the request failing; see
    ``missing_human`` in ``reports/escalation_report.md``.

    Args:
        question: The learner's question, verbatim.
        ticket_type: Which human queue this routes to. Defaults to ``TECH`` —
            see ``DEFAULT_TICKET_TYPE``.
        requester: The context; defaults to the bound one.

    Returns:
        EscalationResult: Always one of the two ``ESCALATION_*`` codes.

    Raises:
        ValidationFailed: Empty question, no cohort resolves for the channel,
            or the cohort is deactivated. A different exception from a refusal
            deliberately, per ``app.services.authorisation``: this is not a
            permission decision, so it is never masked as one.
    """
    context = requester or require_requester()

    cleaned_question = question.strip()
    if not cleaned_question:
        raise ValidationFailed("There is no question to escalate.")

    learner = await identity_repo.get_user_by_mattermost_id(context.mattermost_user_id)
    if learner is None or learner.id is None:
        # Every turn is synced into `users` before the graph runs (see
        # app.services.conversation); reaching here means that invariant
        # broke, not that the learner did anything wrong.
        raise RuntimeError("escalation_attempted_for_unsynced_learner")

    effective_thread_id = context.learner_thread_id or context.channel_id
    existing = await escalation_repo.get_open_escalation_ticket_for_learner_thread(effective_thread_id)
    if existing is not None:
        # Idempotency: a retried webhook delivery, or the model calling this
        # tool twice for one turn, must not open a second ticket or send a
        # second DM. Same thread, already in flight — describe its real
        # state rather than doing the work again.
        logger.info(
            "escalation_idempotent_replay", ticket_ref=existing.ticket_ref, learner_thread_id=effective_thread_id
        )
        return EscalationResult(
            ResultCode.ESCALATION_ALREADY_OPEN,
            _message_for_existing_ticket(existing, EscalationType(existing.ticket_type)),
            existing,
        )

    cohort = await cohort_repo.get_cohort_by_channel_id(context.channel_id)

    if cohort is None or cohort.id is None:
        raise ValidationFailed("I can only escalate a question asked inside a cohort's own channel.")
    require_active_cohort(cohort)

    role_key = ROLE_FOR_TICKET_TYPE[ticket_type]
    members = await cohort_repo.list_cohort_members(cohort.id, active_only=True)
    holder = next((member for member in members if member.role.key == role_key.value), None)

    try:
        ticket = await escalation_repo.create_escalation_ticket(
            cohort_id=cohort.id,
            learner_id=learner.id,
            ticket_type=ticket_type,
            question=cleaned_question,
            learner_channel_id=context.channel_id,
            learner_thread_id=effective_thread_id,
            assigned_human_id=holder.user.id if holder else None,
        )
    except IntegrityError:
        # Lost a race with an identical, concurrent trigger for the same
        # thread: the partial unique index (0003_escalation_open_thread_unique)
        # held, so report the winner instead of opening a second ticket —
        # the same shape as `back_office.create_cohort`'s race handling.
        raced = await escalation_repo.get_open_escalation_ticket_for_learner_thread(effective_thread_id)
        if raced is None:
            raise
        logger.info(
            "escalation_idempotent_replay",
            ticket_ref=raced.ticket_ref,
            learner_thread_id=effective_thread_id,
            raced=True,
        )
        return EscalationResult(
            ResultCode.ESCALATION_ALREADY_OPEN,
            _message_for_existing_ticket(raced, ticket_type),
            raced,
        )

    if holder is None:
        logger.warning(
            "escalation_no_human_available",
            cohort_id=cohort.id,
            role_key=role_key.value,
            ticket_ref=ticket.ticket_ref,
        )
        return EscalationResult(ResultCode.ESCALATION_OPENED_NO_HUMAN, _no_human_message(ticket, role_key), ticket)

    learner_label = learner.display_name or f"@{learner.username}"
    dm_channel = await mattermost_client.create_direct_channel(holder.user.mattermost_user_id)
    dm_post = None
    if dm_channel and dm_channel.get("id"):
        dm_post = await mattermost_client.create_post(
            dm_channel["id"],
            _dm_prompt(ticket, cohort.name, learner_label, cleaned_question),
        )

    if dm_channel and dm_channel.get("id") and dm_post and dm_post.get("id"):
        await escalation_repo.set_escalation_status(
            ticket.ticket_ref,
            EscalationStatus.WAITING_HUMAN,
            assigned_human_id=holder.user.id,
            human_dm_channel_id=dm_channel["id"],
            human_dm_thread_id=dm_post["id"],
        )
        return EscalationResult(ResultCode.ESCALATION_OPENED, _handed_off_message(ticket), ticket)

    # Mattermost was unreachable, or the DM failed to open/post. The ticket
    # stays `open` with a human already assigned so a chaser or an operator
    # can retry the handoff without the learner's question being lost —
    # logged at `error` because, unlike `no_human`, this is a system fault
    # worth paging on, not a cohort configuration gap. The learner is told
    # honestly: a human is assigned but has not actually been contacted yet,
    # never that the handoff already happened.
    logger.error(
        "escalation_dm_handoff_failed",
        cohort_id=cohort.id,
        ticket_ref=ticket.ticket_ref,
        assigned_human_id=holder.user.id,
    )
    return EscalationResult(ResultCode.ESCALATION_OPENED, _assigned_but_unreachable_message(ticket), ticket)


__all__ = [
    "DEFAULT_TICKET_TYPE",
    "ROLE_FOR_TICKET_TYPE",
    "EscalationResult",
    "open_escalation",
]
