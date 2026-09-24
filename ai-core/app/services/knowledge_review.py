import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
import re

from sqlmodel import col, select
from app.core.logging import logger
from app.core.requester import RequesterContext
from app.models import EscalationTicket
from app.models.enums import KnowledgeCandidateStatus
from app.models.knowledge_candidate import KnowledgeCandidate
from app.services.authorisation import AuthorisationRefused, require_requester_user
from app.services.database import session_scope
from app.services.document_ingestion.embeddings import generate_embeddings
from app.services.document_ingestion.vector_store import PolicyVectorStore
from app.services.identity import resolve_requester
from app.services.mattermost import mattermost_client

_KC_REF = re.compile(r"\b(approve|reject)\s+KC-(\d+)(?:\s+(.*))?", re.IGNORECASE)


class ReviewOutcome(StrEnum):
    APPROVED = "approved"
    REJECTED = "rejected"
    NOT_FOUND = "not_found"
    ALREADY_DECIDED = "already_decided"


@dataclass(frozen=True)
class CandidateForReview:
    candidate_id: int
    statement: str
    audience: str
    escalation_ref: str
    original_question: str
    original_decision: str


@dataclass(frozen=True)
class ReviewResult:
    outcome: ReviewOutcome
    candidate: KnowledgeCandidate | None = None


def _deterministic_chunk_id(escalation_id: int, content_hash: str) -> str:
    return hashlib.sha256(f"escalation:{escalation_id}_0_{content_hash}".encode("utf-8")).hexdigest()


async def list_pending_for_review() -> list[CandidateForReview]:
    async with session_scope() as session:
        stmt = (
            select(KnowledgeCandidate, EscalationTicket)
            .join(EscalationTicket, col(EscalationTicket.id) == col(KnowledgeCandidate.escalation_id))
            .where(col(KnowledgeCandidate.status) == KnowledgeCandidateStatus.PENDING.value)
            .order_by(col(KnowledgeCandidate.created_at))
        )
        result = await session.exec(stmt)
        rows = result.all()

    return [
        CandidateForReview(
            candidate_id=candidate.id or 0,
            statement=candidate.statement,
            audience=candidate.audience,
            escalation_ref=ticket.ticket_ref,
            original_question=ticket.question,
            original_decision=ticket.raw_human_response or "",
        )
        for candidate, ticket in rows
    ]


async def approve_candidate(
    candidate_id: int, *, requester: RequesterContext | None = None, audience_override: str | None = None
) -> ReviewResult:
    reviewer = await require_requester_user(requester, action="approve knowledge candidate")
    assert reviewer.id is not None  # narrowed for pyright: require_requester_user returns a persisted user

    async with session_scope() as session:
        candidate = await session.get(KnowledgeCandidate, candidate_id)
        if candidate is None:
            return ReviewResult(ReviewOutcome.NOT_FOUND)
        if candidate.status != KnowledgeCandidateStatus.PENDING.value:
            return ReviewResult(ReviewOutcome.ALREADY_DECIDED, candidate=candidate)

        final_audience = audience_override or candidate.audience

        candidate.status = KnowledgeCandidateStatus.APPROVED.value
        candidate.audience = final_audience
        candidate.reviewer_id = reviewer.id
        candidate.reviewed_at = datetime.now(UTC)
        session.add(candidate)

    await _index_candidate(candidate)

    logger.info(
        "knowledge_candidate_approved",
        candidate_id=candidate.id,
        escalation_id=candidate.escalation_id,
        audience=final_audience,
        reviewer_id=reviewer.id,
    )
    return ReviewResult(ReviewOutcome.APPROVED, candidate=candidate)


async def reject_candidate(
    candidate_id: int, *, requester: RequesterContext | None = None, reason: str | None = None
) -> ReviewResult:
    reviewer = await require_requester_user(requester, action="reject knowledge candidate")

    async with session_scope() as session:
        candidate = await session.get(KnowledgeCandidate, candidate_id)
        if candidate is None:
            return ReviewResult(ReviewOutcome.NOT_FOUND)
        if candidate.status != KnowledgeCandidateStatus.PENDING.value:
            return ReviewResult(ReviewOutcome.ALREADY_DECIDED, candidate=candidate)

        candidate.status = KnowledgeCandidateStatus.REJECTED.value
        assert reviewer.id is not None  # narrowed for pyright: require_requester_user returns a persisted user
        candidate.reviewer_id = reviewer.id
        candidate.reviewed_at = datetime.now(UTC)
        candidate.rejection_reason = reason
        session.add(candidate)

    logger.info("knowledge_candidate_rejected", candidate_id=candidate.id, reviewer_id=reviewer.id)
    return ReviewResult(ReviewOutcome.REJECTED, candidate=candidate)


async def _index_candidate(candidate: KnowledgeCandidate) -> None:
    content_hash = hashlib.sha256(candidate.statement.encode("utf-8")).hexdigest()
    chunk_id = _deterministic_chunk_id(candidate.escalation_id, content_hash)

    chunk = {
        "id": chunk_id,
        "document_id": f"escalation:{candidate.escalation_id}",
        "chunk_index": 0,
        "audience": candidate.audience,
        "content": candidate.statement,
        "content_hash": content_hash,
        "metadata": {
            "source": "knowledge_candidate",
            "escalation_id": candidate.escalation_id,
            "candidate_id": candidate.id,
            "reviewer_id": candidate.reviewer_id,
        },
    }
    embedding = await generate_embeddings(candidate.statement)
    await PolicyVectorStore().upsert_chunks([chunk], [embedding])


async def handle_reviewer_reply(*, mattermost_user_id: str, channel_id: str, channel_type: str, text: str) -> bool:
    if channel_type not in {"D", "G"}:
        return False

    stripped = text.strip()

    if re.search(r"\blist\s+KC\b", stripped, re.IGNORECASE):
        requester = await resolve_requester(
            mattermost_user_id=mattermost_user_id,
            channel_id=channel_id,
            channel_type=channel_type,
        )
        pending = await list_pending_for_review()
        if not pending:
            await mattermost_client.create_post(channel_id, "No knowledge candidates are currently pending review.")
        else:
            lines = ["**Pending knowledge candidates:**\n"]
            for c in pending:
                lines.append(
                    f"• **KC-{c.candidate_id}** ({c.audience}) from {c.escalation_ref}\n"
                    f"  Statement: {c.statement}\n"
                    f"  Question: {c.original_question}\n"
                    f"  Decision: {c.original_decision}\n"
                    f"  → Reply `approve KC-{c.candidate_id}` or `reject KC-{c.candidate_id} <reason>`"
                )
            await mattermost_client.create_post(channel_id, "\n\n".join(lines))
        return True

    match = _KC_REF.search(stripped)
    if not match:
        return False

    action = match.group(1).lower()
    candidate_id = int(match.group(2))
    reason = match.group(3)

    requester = await resolve_requester(
        mattermost_user_id=mattermost_user_id,
        channel_id=channel_id,
        channel_type=channel_type,
    )

    try:
        if action == "approve":
            result = await approve_candidate(candidate_id, requester=requester)
            if result.outcome == ReviewOutcome.APPROVED:
                reply = f"✅ KC-{candidate_id} approved and indexed into the knowledge base."
            elif result.outcome == ReviewOutcome.ALREADY_DECIDED:
                reply = f"KC-{candidate_id} was already decided (status: {result.candidate.status if result.candidate else 'unknown'})."
            else:
                reply = f"KC-{candidate_id} not found."
        else:
            result = await reject_candidate(candidate_id, requester=requester, reason=reason)
            if result.outcome == ReviewOutcome.REJECTED:
                reply = f"❌ KC-{candidate_id} rejected — it will not be indexed."
            elif result.outcome == ReviewOutcome.ALREADY_DECIDED:
                reply = f"KC-{candidate_id} was already decided (status: {result.candidate.status if result.candidate else 'unknown'})."
            else:
                reply = f"KC-{candidate_id} not found."
    except AuthorisationRefused as e:
        reply = f"❌ Could not process decision: {str(e)}"

    await mattermost_client.create_post(channel_id, reply)
    return True
