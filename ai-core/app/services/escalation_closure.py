"""Escalation closure: turn a human's terse reply into a delivered answer.

--> Target path: app/services/escalation_closure.py

This is the *return* half of the escalation feature (task 1 of the sprint
opened it -- see app.services.escalation and app.services.domain.escalations
for the schema and vocabulary this module builds on directly).

Design, in one paragraph: a reviewer replies in Mattermost. This module never
guesses which ticket that reply resolves. It is attributed one of three ways,
in order of trust: (1) the reply is inside the exact thread the bot opened
for that ticket (root_id matches human_dm_thread_id -- deterministic, no
ambiguity possible since that id is unique per ticket); (2) the reply is not
threaded but explicitly cites a ticket reference (ESC-000042) -- still
deterministic; (3) neither -- this is the case the brief and the mentor
review both flagged: an unthreaded, unreferenced reply must NEVER be assumed
to resolve "the one open ticket" even when there is only one, because that
risks mistaking ordinary chat for a decision. Case (3) always asks the
reviewer to clarify rather than guessing, whether zero, one, or many tickets
are open.

Once a ticket is attributed, the learner's original question and the
reviewer's raw text go to a tightly grounded LLM prompt
(app/core/prompts/escalation_closure/system.md) whose only job is to expand
tone and completeness, never information. The result is posted into the
learner's original thread (never the reviewer's identity, never a ticket
id), the reviewer gets a private confirmation, and the ticket is closed with
the verbatim human input retained.
"""

import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from langchain_core.messages import HumanMessage, SystemMessage

from app.core.logging import logger
from app.models import EscalationTicket, User
from app.models.enums import EscalationStatus
from app.services.domain import escalations as escalation_repo
from app.services.domain import identity as identity_repo
from app.services.llm.service import llm_service
from app.services.mattermost import mattermost_client

# Matches a cited ticket reference anywhere in a message, case-insensitively:
# "yes approve it, ESC-000042", "esc-42" is deliberately NOT matched (the
# stored format is always 6 digits, zero-padded -- see format_ticket_ref in
# app.services.domain.escalations) to avoid false positives on short numbers
# mentioned in casual chat.
_TICKET_REF_RE = re.compile(r"\bESC-(\d{6})\b", re.IGNORECASE)

_SYSTEM_PROMPT_PATH = Path(__file__).resolve().parent.parent / "core" / "prompts" / "escalation_closure" / "system.md"
_SYSTEM_PROMPT = _SYSTEM_PROMPT_PATH.read_text(encoding="utf-8").strip()


class ClosureOutcome(StrEnum):
    """What happened when a reviewer's message was inspected.

    Not a tool result code (nothing here is agent-invoked; this is a direct
    WebSocket-level interception, same reasoning as onboarding's arrival
    handling) -- this is purely an internal control-flow / logging signal.
    """

    NOT_ESCALATION = "not_escalation"  # not from anyone with escalation context; let normal chat handle it
    RESOLVED = "resolved"  # attributed and successfully closed
    AMBIGUOUS = "ambiguous"  # unthreaded, unreferenced, could not be attributed without guessing
    TICKET_NOT_FOUND = "ticket_not_found"  # an explicit ticket ref was cited but does not exist
    WRONG_OWNER = "wrong_owner"  # an explicit ticket ref was cited but belongs to a different reviewer
    ALREADY_RESOLVED = "already_resolved"  # matched a ticket that is no longer waiting_human
    DELIVERY_FAILED = "delivery_failed"  # attributed and synthesized, but posting to the learner failed


@dataclass(frozen=True)
class ClosureResult:
    """Outcome of handling one candidate escalation reply.

    Attributes:
        outcome: What happened.
        ticket: The ticket involved, when one was identified (even if not resolved).
        reply_text: What was sent back into the reviewer's conversation, for logging/tests.
    """

    outcome: ClosureOutcome
    ticket: EscalationTicket | None = None
    reply_text: str | None = None


# ---------------------------------------------------------------------------
# Attribution
# ---------------------------------------------------------------------------


def _extract_ticket_ref(text: str) -> str | None:
    """Find an explicit, well-formed ticket reference in free text.

    Args:
        text: The reviewer's raw message.

    Returns:
        str | None: ``ESC-000042`` (normalised upper-case), or None.
    """
    match = _TICKET_REF_RE.search(text)
    if match is None:
        return None
    return f"ESC-{match.group(1)}"


@dataclass(frozen=True)
class _Attribution:
    """Internal: what attribution decided, before any message is composed."""

    outcome: ClosureOutcome
    ticket: EscalationTicket | None = None
    candidates: tuple[EscalationTicket, ...] = ()


async def _attribute(reviewer: User, root_id: str, text: str) -> _Attribution:
    """Decide which ticket, if any, a reviewer's message resolves.

    Trust order: thread match, then explicit citation, then -- deliberately
    -- nothing else. An unthreaded, unreferenced message is never resolved
    to "the one open ticket" even when there is exactly one; per the design
    review, that would risk treating ordinary chat as a decision.

    Args:
        reviewer: The synced User row for the sender.
        root_id: The incoming post's root id, empty when not in a thread.
        text: The raw message text.

    Returns:
        _Attribution: The decision.
    """
    if root_id:
        threaded = await escalation_repo.get_escalation_ticket_by_human_thread(root_id)
        if threaded is not None:
            if threaded.status == EscalationStatus.WAITING_HUMAN.value:
                return _Attribution(ClosureOutcome.RESOLVED, ticket=threaded)
            return _Attribution(ClosureOutcome.ALREADY_RESOLVED, ticket=threaded)
        # Threaded under something that isn't a tracked escalation DM at all
        # (e.g. a reply inside an unrelated conversation with the bot) --
        # fall through to the citation check rather than assuming NOT_ESCALATION,
        # in case they also typed a ticket ref in that same message.

    cited_ref = _extract_ticket_ref(text)
    if cited_ref is not None:
        cited = await escalation_repo.get_escalation_ticket(cited_ref)
        if cited is None:
            return _Attribution(ClosureOutcome.TICKET_NOT_FOUND)
        if cited.assigned_human_id != reviewer.id:
            return _Attribution(ClosureOutcome.WRONG_OWNER, ticket=cited)
        if cited.status != EscalationStatus.WAITING_HUMAN.value:
            return _Attribution(ClosureOutcome.ALREADY_RESOLVED, ticket=cited)
        return _Attribution(ClosureOutcome.RESOLVED, ticket=cited)

    # No thread match, no citation. Only now do we ask "is this person even
    # a plausible escalation reviewer right now" -- zero candidates means
    # this is just someone chatting with the bot, not an escalation reply.
    candidates = await escalation_repo.list_waiting_tickets_for_human(reviewer.id) if reviewer.id else []
    if not candidates:
        return _Attribution(ClosureOutcome.NOT_ESCALATION)
    return _Attribution(ClosureOutcome.AMBIGUOUS, candidates=tuple(candidates))


# ---------------------------------------------------------------------------
# Grounded synthesis
# ---------------------------------------------------------------------------


async def _synthesize_answer(question: str, raw_human_response: str) -> str:
    """Turn a terse human decision into a complete learner-facing answer.

    Args:
        question: The learner's original question, verbatim.
        raw_human_response: The reviewer's decision, verbatim.

    Returns:
        str: The message to post into the learner's thread.
    """
    user_payload = f"Learner's question:\n{question}\n\nColleague's decision (verbatim):\n{raw_human_response}"
    response = await llm_service.call([SystemMessage(content=_SYSTEM_PROMPT), HumanMessage(content=user_payload)])
    return str(response.content).strip()


# ---------------------------------------------------------------------------
# Operational (non-LLM) message text
# ---------------------------------------------------------------------------


def _reviewer_confirmation(ticket: EscalationTicket) -> str:
    """Confirmation posted back to the reviewer once delivery succeeds."""
    return f"Delivered to the learner. Thanks -- {ticket.ticket_ref} is closed."


def _ambiguous_message(candidates: tuple[EscalationTicket, ...]) -> str:
    """Clarification request when a reply cannot be attributed without guessing."""
    if len(candidates) == 1:
        refs = candidates[0].ticket_ref
        return (
            "I want to make sure I attach this to the right thing -- could you reply directly "
            f"inside that escalation's thread, or mention its reference ({refs}), so I don't "
            "mistake this for something else?"
        )
    refs = ", ".join(t.ticket_ref for t in candidates)
    return (
        "I have more than one open question waiting on you right now, so I don't want to guess "
        f"which one this answers. Could you reply inside the specific thread, or mention its "
        f"reference? Open: {refs}."
    )


def _ticket_not_found_message(cited_ref: str) -> str:
    """Reply when a cited ticket reference does not exist."""
    return f"I couldn't find an escalation with reference {cited_ref} -- could you double-check the number?"


def _wrong_owner_message(ticket: EscalationTicket) -> str:
    """Reply when a cited ticket reference belongs to someone else."""
    return f"{ticket.ticket_ref} isn't assigned to you, so I can't close it from this reply."


def _already_resolved_message(ticket: EscalationTicket) -> str:
    """Reply when a reviewer follows up on a ticket that is no longer open."""
    return f"{ticket.ticket_ref} was already resolved -- thanks anyway, no action needed on this one."


# ---------------------------------------------------------------------------
# Delivery and closure
# ---------------------------------------------------------------------------


async def _close(ticket: EscalationTicket, reviewer: User, raw_human_response: str) -> ClosureResult:
    """Synthesize, deliver, confirm, and close -- in that order, only advancing on success.

    The learner delivery is the critical path: the ticket is only marked
    resolved once the learner has actually received the answer. If that post
    fails, the ticket stays `waiting_human` (never silently lost) and the
    reviewer is told honestly. The reviewer's own confirmation is best-effort:
    if it fails after the learner was already answered, the ticket still
    closes -- the confirmation is a courtesy, not part of the guarantee.

    Args:
        ticket: The attributed, still-open ticket.
        reviewer: The person who sent the decision.
        raw_human_response: Their message, verbatim.

    Returns:
        ClosureResult: RESOLVED on success, DELIVERY_FAILED if the learner
        could not be reached.
    """
    answer = await _synthesize_answer(ticket.question, raw_human_response)

    learner_post = await mattermost_client.create_post(
        ticket.channel_id, answer, root_id=ticket.learner_thread_id
    )
    if not learner_post or not learner_post.get("id"):
        logger.error(
            "escalation_closure_learner_delivery_failed",
            ticket_ref=ticket.ticket_ref,
            reviewer_id=reviewer.id,
        )
        if ticket.human_dm_channel_id:
            await mattermost_client.create_post(
                ticket.human_dm_channel_id,
                f"I have your answer for {ticket.ticket_ref} but couldn't deliver it just now -- "
                "I'll keep this open and retry rather than lose it.",
                root_id=ticket.human_dm_thread_id,
            )
        return ClosureResult(ClosureOutcome.DELIVERY_FAILED, ticket=ticket)

    await escalation_repo.set_escalation_status(ticket.ticket_ref, EscalationStatus.RESOLVED, answer=answer , raw_human_response=raw_human_response,)

    confirmation = _reviewer_confirmation(ticket)
    if ticket.human_dm_channel_id:
        confirmed = await mattermost_client.create_post(
            ticket.human_dm_channel_id, confirmation, root_id=ticket.human_dm_thread_id
        )
        if not confirmed or not confirmed.get("id"):
            logger.warning(
                "escalation_closure_reviewer_confirmation_failed",
                ticket_ref=ticket.ticket_ref,
                reviewer_id=reviewer.id,
            )

    logger.info(
        "escalation_closure_resolved",
        ticket_ref=ticket.ticket_ref,
        reviewer_id=reviewer.id,
        learner_id=ticket.learner_id,
    )
    return ClosureResult(ClosureOutcome.RESOLVED, ticket=ticket, reply_text=confirmation)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


async def handle_reviewer_reply(
    *,
    mattermost_user_id: str,
    channel_id: str,
    channel_type: str,
    root_id: str,
    text: str,
) -> ClosureResult:
    """Inspect one incoming Mattermost message for an escalation-closing reply.

    Called from app.services.mattermost_ws BEFORE the message is dispatched
    to the normal chat pipeline. A NOT_ESCALATION outcome means the caller
    should proceed with normal handling (answer_and_reply); every other
    outcome means this module already replied and the message is consumed.

    Args:
        mattermost_user_id: Sender's Mattermost id.
        channel_id: Where the message arrived.
        channel_type: Mattermost channel type. Escalation replies only ever
            arrive in a direct or group DM (where the bot opened the
            handoff), so anything else is an immediate NOT_ESCALATION.
        root_id: The post's root id, empty when not in a thread.
        text: The raw message text.

    Returns:
        ClosureResult: What happened.
    """
    if channel_type not in {"D", "G"}:
        return ClosureResult(ClosureOutcome.NOT_ESCALATION)

    reviewer = await identity_repo.get_user_by_mattermost_id(mattermost_user_id)
    if reviewer is None or reviewer.id is None:
        # Not a synced user at all -- cannot be a tracked escalation reviewer.
        return ClosureResult(ClosureOutcome.NOT_ESCALATION)

    attribution = await _attribute(reviewer, root_id, text)

    if attribution.outcome == ClosureOutcome.NOT_ESCALATION:
        return ClosureResult(ClosureOutcome.NOT_ESCALATION)

    if attribution.outcome == ClosureOutcome.RESOLVED:
        assert attribution.ticket is not None
        return await _close(attribution.ticket, reviewer, text)

    # Every remaining branch replies into the same conversation the reviewer
    # just used, without touching any ticket's state.
    reply_root = root_id or None
    if attribution.outcome == ClosureOutcome.AMBIGUOUS:
        reply_text = _ambiguous_message(attribution.candidates)
    elif attribution.outcome == ClosureOutcome.TICKET_NOT_FOUND:
        cited = _extract_ticket_ref(text) or "that reference"
        reply_text = _ticket_not_found_message(cited)
    elif attribution.outcome == ClosureOutcome.WRONG_OWNER:
        assert attribution.ticket is not None
        reply_text = _wrong_owner_message(attribution.ticket)
    elif attribution.outcome == ClosureOutcome.ALREADY_RESOLVED:
        assert attribution.ticket is not None
        reply_text = _already_resolved_message(attribution.ticket)
        logger.info(
            "escalation_post_closure_message",
            ticket_ref=attribution.ticket.ticket_ref,
            reviewer_id=reviewer.id,
            raw_text=text,
        )
    else:  # pragma: no cover - exhaustive per ClosureOutcome
        return ClosureResult(ClosureOutcome.NOT_ESCALATION)

    await mattermost_client.create_post(channel_id, reply_text, root_id=reply_root)
    logger.info(
        "escalation_closure_non_resolving_reply",
        outcome=attribution.outcome.value,
        reviewer_id=reviewer.id,
    )
    return ClosureResult(attribution.outcome, ticket=attribution.ticket, reply_text=reply_text)


__all__ = ["ClosureOutcome", "ClosureResult", "handle_reviewer_reply"]