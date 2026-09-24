from datetime import datetime

from sqlalchemy import Text
from sqlmodel import Field

from app.models.domain_base import TZ_DATETIME, DomainBase
from app.models.enums import KnowledgeCandidateStatus


class KnowledgeCandidate(DomainBase, table=True):
    """One extracted, reviewable statement derived from a resolved escalation.

    Attributes:
        id: Primary key.
        escalation_id: The source EscalationTicket.id this was extracted
            from. Unique -- one candidate per escalation, forever; this is
            what makes repeated discovery passes idempotent.
        statement: The proposed knowledge text, already narrowed by the
            extraction prompt to avoid generalizing a one-off exception into
            a blanket rule. This is what gets embedded and indexed if
            approved -- never the raw escalation question/answer pair.
        audience: Who this should be reachable by once approved --
            "learner" or "internal_operator" (matching the values Task 3's
            ingestion already uses; see the report's open question on this).
        status: pending / approved / rejected. Set once by discovery
            (always "pending"), then transitioned exactly once by a reviewer.
        reviewer_id: Who made the approve/reject decision. Null while pending.
        reviewed_at: When the decision was made. Null while pending.
        rejection_reason: Optional free text a reviewer can leave when
            rejecting, for later audit -- not shown to anyone but other
            reviewers/auditors.
    """

    __tablename__ = "knowledge_candidates"  # pyright: ignore[reportAssignmentType]

    id: int | None = Field(default=None, primary_key=True)
    escalation_id: int = Field(foreign_key="escalation_tickets.id", unique=True, index=True)
    statement: str = Field(sa_type=Text)
    audience: str = Field(max_length=32, index=True)
    status: str = Field(default=KnowledgeCandidateStatus.PENDING.value, max_length=16, index=True)
    reviewer_id: int | None = Field(default=None, foreign_key="users.id", index=True)
    reviewed_at: datetime | None = Field(default=None, sa_type=TZ_DATETIME)
    rejection_reason: str | None = Field(default=None, sa_type=Text)
