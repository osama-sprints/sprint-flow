"""Discover candidate institutional knowledge from resolved escalations.

--> Target path: app/services/knowledge_extraction.py

One resolved escalation can produce at most one candidate, ever -- the
unique constraint on KnowledgeCandidate.escalation_id (enforced at the
database level, not just checked in Python) is what makes running this
repeatedly, across restarts, fully idempotent. This module never updates or
overwrites an existing candidate; it only ever proposes new ones for
escalations it hasn't seen yet.

The one property this module has to get right by design, not by luck: an
answer that was a one-off exception ("just this once, because of the fibre
cut") must never turn into a candidate that reads like a standing rule. That
is entirely the extraction prompt's job -- see _SYSTEM_PROMPT.
"""

from typing import Optional

from langchain_core.messages import HumanMessage, SystemMessage
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlmodel import Session

from app.core.logging import logger
from app.models import EscalationTicket
from app.models.enums import EscalationStatus, KnowledgeCandidateStatus
from app.models.knowledge_candidate import KnowledgeCandidate
from app.services.database import database_service
from app.services.llm.service import llm_service
from app.models.enums import RoleKey
from app.services.domain import channels as channel_repo
from app.services.mattermost import mattermost_client
from app.services.domain.channels import find_any_ops_support_user
from app.services.database import session_scope
_SYSTEM_PROMPT = """You extract one reviewable candidate piece of institutional knowledge from a resolved support escalation.
You are given the learner's original question and the human reviewer's verbatim decision.
Your job is to formulate a concise knowledge statement capturing the decision, and classify the target audience.
Audience classification:
- "learner": policy, process, or general information suitable for learners
- "internal_operator": operational guidance suitable for staff/reviewers
You MUST ALWAYS provide both an AUDIENCE and a STATEMENT. Do NOT output NONE or decline to generate a statement.
Respond in exactly this form, two lines, nothing else:
AUDIENCE: learner|internal_operator
STATEMENT: <the clear knowledge statement summarizing the decision>
"""

async def _notify_reviewer(candidate_id: int, ticket: EscalationTicket, statement: str, audience: str) -> None:
    """DM the ops-support reviewer about a new knowledge candidate.

    Resolution order:
    1. Look for an OPS_SUPPORT member in the exact channel the escalation
       came from — that person already has context on this cohort.
    2. Fall back to ANY active OPS_SUPPORT member in the workspace when
       none is assigned to that specific channel.
    3. Log a warning and return when the workspace has no OPS_SUPPORT at all.
    """
    # 1. Try the ticket's own channel first.
    members = await channel_repo.list_channel_roles(ticket.channel_id, active_only=True)
    holder_user = next(
        (m.user for m in members if m.role.key == RoleKey.OPS_SUPPORT.value), None
    )

    # 2. Fallback: any ops-support member system-wide.
    if holder_user is None:
        logger.warning(
            "knowledge_candidate_no_ops_support_in_channel",
            candidate_id=candidate_id,
            channel_id=ticket.channel_id,
        )
        holder_user = await find_any_ops_support_user()

    if holder_user is None:
        logger.warning(
            "knowledge_candidate_no_ops_support_anywhere",
            candidate_id=candidate_id,
        )
        return

    dm_channel = await mattermost_client.create_direct_channel(holder_user.mattermost_user_id)
    if not (dm_channel and dm_channel.get("id")):
        logger.warning(
            "knowledge_candidate_dm_failed",
            candidate_id=candidate_id,
            reviewer_user_id=holder_user.mattermost_user_id,
        )
        return

    await mattermost_client.create_post(
        dm_channel["id"],
        f"**New knowledge candidate KC-{candidate_id}** from {ticket.ticket_ref}\n\n"
        f"Audience: {audience}\n"
        f"Statement: {statement}\n\n"
        f"Original question: {ticket.question}\n"
        f"Original decision: {ticket.raw_human_response}\n\n"
        f"Reply `approve KC-{candidate_id}` or `reject KC-{candidate_id} <reason>`.\n"
        f"Reply `list KC` to see all pending candidates.",
    )
    logger.info(
        "knowledge_candidate_reviewer_notified",
        candidate_id=candidate_id,
        reviewer_user_id=holder_user.mattermost_user_id,
    )

async def _extract_candidate(question: str, raw_human_response: str) -> Optional[tuple[str, str]]:
    payload = f"Question:\n{question}\n\nReviewer's decision (verbatim):\n{raw_human_response}"
    response = await llm_service.call([SystemMessage(content=_SYSTEM_PROMPT), HumanMessage(content=payload)])
    text = str(response.content).strip()
    if text.upper() == "NONE":
        return None
    audience = None
    statement = None
    for line in text.splitlines():
        if line.upper().startswith("AUDIENCE:"):
            audience = line.split(":", 1)[1].strip().lower()
        elif line.upper().startswith("STATEMENT:"):
            statement = line.split(":", 1)[1].strip()
    if not statement or audience not in ("learner", "internal_operator"):
        logger.warning("knowledge_extraction_unparseable_response", raw_response=text[:200])
        return None
    return audience, statement


async def extract_candidate_for_ticket(escalation_id: int) -> bool:
    async with session_scope() as session:
        ticket = await session.get(EscalationTicket, escalation_id)
        if ticket is None or ticket.status != EscalationStatus.RESOLVED.value:
            return False
        if not ticket.answer or not ticket.raw_human_response:
            return False

    extracted = await _extract_candidate(ticket.question, ticket.raw_human_response)
    if extracted is None:
        logger.info("knowledge_extraction_skipped_no_generalizable_content", escalation_id=escalation_id)
        return False

    audience, statement = extracted
    async with session_scope() as session:
        stmt = (
            pg_insert(KnowledgeCandidate)
            .values(
                escalation_id=escalation_id,
                statement=statement,
                audience=audience,
                status=KnowledgeCandidateStatus.PENDING.value,
            )
            .on_conflict_do_nothing(index_elements=["escalation_id"])
            .returning(KnowledgeCandidate.id)
        )
        result = await session.exec(stmt)
        new_id = result.scalar_one_or_none()

    if new_id is not None:
        logger.info("knowledge_candidate_created", escalation_id=escalation_id, audience=audience)
        await _notify_reviewer(candidate_id=new_id, ticket=ticket, statement=statement, audience=audience)
        return True
    return False


async def discover_candidates() -> int:
    """Run one discovery pass over resolved escalations, idempotently."""
    async with session_scope() as session:
        already_seen = select(KnowledgeCandidate.escalation_id)
        stmt = select(EscalationTicket).where(
            EscalationTicket.status == EscalationStatus.RESOLVED.value,
            EscalationTicket.answer.is_not(None),
            EscalationTicket.raw_human_response.is_not(None),
            EscalationTicket.id.not_in(already_seen),
        )
        result = await session.exec(stmt)
        pending_tickets = result.all()

    created = 0
    for ticket in pending_tickets:
        extracted = await _extract_candidate(ticket.question, ticket.raw_human_response)
        if extracted is None:
            logger.info("knowledge_extraction_skipped_no_generalizable_content", escalation_id=ticket.id)
            continue

        audience, statement = extracted
        async with session_scope() as session:
            stmt = (
                pg_insert(KnowledgeCandidate)
                .values(
                    escalation_id=ticket.id,
                    statement=statement,
                    audience=audience,
                    status=KnowledgeCandidateStatus.PENDING.value,
                )
                .on_conflict_do_nothing(index_elements=["escalation_id"])
                .returning(KnowledgeCandidate.id)
            )
            res = await session.exec(stmt)
            new_id = res.scalar_one_or_none()

        if new_id is not None:
            created += 1
            logger.info(
                "knowledge_candidate_created",
                escalation_id=ticket.id,
                audience=audience,
            )
            await _notify_reviewer(candidate_id=new_id, ticket=ticket, statement=statement, audience=audience)

    return created