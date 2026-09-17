from app.models.ceremony import Ceremony
from app.models.ceremony_reminder import CeremonyReminder
from app.models.ceremony_amendment import CeremonyAmendment
from app.models.ceremony_type import CeremonyType
from app.models.channel_role import ChannelRole
from app.models.daily_standup import DailyStandup
from app.models.daily_standup_prompt import DailyStandupPrompt
from app.models.domain_base import (
    DomainBase,
    require_aware,
    utcnow,
)
from app.models.escalation_ticket import EscalationTicket
from app.models.onboarding_step import OnboardingStep
from app.models.policy import PolicyDocumentChunk
from app.models.role import Role
from app.models.sprint import Sprint
from app.models.standup_reply import StandupReply
from app.models.user import User

DOMAIN_TABLES: tuple[str, ...] = (
    "users",
    "roles",
    "ceremony_types",
    "channel_roles",
    "sprints",
    "ceremonies",
    "ceremony_amendments",
    "ceremony_reminders",
    "daily_standup_prompts",
    "daily_standups",
    "escalation_tickets",
    "onboarding_steps",
    "policy_document_chunks",
    "standup_replies",
)

__all__ = [
    "DOMAIN_TABLES",
    "Ceremony",
    "CeremonyAmendment",
    "CeremonyReminder",
    "CeremonyType",
    "ChannelRole",
    "DailyStandup",
    "DailyStandupPrompt",
    "DomainBase",
    "EscalationTicket",
    "OnboardingStep",
    "PolicyDocumentChunk",
    "Role",
    "Sprint",
    "StandupReply",
    "User",
    "require_aware",
    "utcnow",
]
