"""Machine keys and lifecycle values shared by models, migrations, services, tools and seeds.

The database stores plain strings (no PostgreSQL enum types) so a new value never
needs a migration; these enumerations are the single place where the allowed
values, their display labels and the aliases people type are defined.
"""

from enum import StrEnum


class RoleKey(StrEnum):
    """Cohort-scoped role keys. A person holds one of these per cohort."""

    LEARNER = "learner"
    TECH_LEAD = "tech_lead"
    OPS_SUPPORT = "ops_support"
    SCRUM_MASTER = "scrum_master"


ROLE_LABELS: dict[RoleKey, str] = {
    RoleKey.LEARNER: "Learner",
    RoleKey.TECH_LEAD: "Tech Lead",
    RoleKey.OPS_SUPPORT: "Ops Support",
    RoleKey.SCRUM_MASTER: "Scrum Master",
}

ROLE_DESCRIPTIONS: dict[RoleKey, str] = {
    RoleKey.LEARNER: "Takes part in the cohort's sprints, standups and ceremonies.",
    RoleKey.TECH_LEAD: "Resolves technical escalations and may administer the cohort.",
    RoleKey.OPS_SUPPORT: "Resolves policy and operational escalations for the cohort.",
    RoleKey.SCRUM_MASTER: "Runs the agile ceremonies and may administer the cohort.",
}

# Words people use for a role, lower-cased, mapped to the machine key.
ROLE_ALIASES: dict[str, RoleKey] = {
    "learner": RoleKey.LEARNER,
    "student": RoleKey.LEARNER,
    "tech_lead": RoleKey.TECH_LEAD,
    "tech lead": RoleKey.TECH_LEAD,
    "techlead": RoleKey.TECH_LEAD,
    "technical lead": RoleKey.TECH_LEAD,
    "ops_support": RoleKey.OPS_SUPPORT,
    "ops support": RoleKey.OPS_SUPPORT,
    "ops": RoleKey.OPS_SUPPORT,
    "operations": RoleKey.OPS_SUPPORT,
    "operations support": RoleKey.OPS_SUPPORT,
    "scrum_master": RoleKey.SCRUM_MASTER,
    "scrum master": RoleKey.SCRUM_MASTER,
    "scrummaster": RoleKey.SCRUM_MASTER,
}

# Roles that may administer the cohort they hold the role in: assign roles,
# open sprints and schedule ceremonies. Everything else is read-only.
COHORT_ADMIN_ROLES: frozenset[RoleKey] = frozenset({RoleKey.TECH_LEAD, RoleKey.SCRUM_MASTER})


class CeremonyTypeKey(StrEnum):
    """Seeded ceremony types. Open Q&A is included per the product brief."""

    DAILY_STANDUP = "daily_standup"
    SPRINT_PLANNING = "sprint_planning"
    SPRINT_REVIEW = "sprint_review"
    RETROSPECTIVE = "retrospective"
    OPEN_QA = "open_qa"


CEREMONY_TYPE_LABELS: dict[CeremonyTypeKey, str] = {
    CeremonyTypeKey.DAILY_STANDUP: "Daily Standup",
    CeremonyTypeKey.SPRINT_PLANNING: "Sprint Planning",
    CeremonyTypeKey.SPRINT_REVIEW: "Sprint Review",
    CeremonyTypeKey.RETROSPECTIVE: "Retrospective",
    CeremonyTypeKey.OPEN_QA: "Open Q&A",
}

CEREMONY_TYPE_DEFAULT_DURATION_MINUTES: dict[CeremonyTypeKey, int] = {
    CeremonyTypeKey.DAILY_STANDUP: 15,
    CeremonyTypeKey.SPRINT_PLANNING: 90,
    CeremonyTypeKey.SPRINT_REVIEW: 60,
    CeremonyTypeKey.RETROSPECTIVE: 60,
    CeremonyTypeKey.OPEN_QA: 60,
}

CEREMONY_TYPE_ALIASES: dict[str, CeremonyTypeKey] = {
    "daily_standup": CeremonyTypeKey.DAILY_STANDUP,
    "daily standup": CeremonyTypeKey.DAILY_STANDUP,
    "standup": CeremonyTypeKey.DAILY_STANDUP,
    "stand-up": CeremonyTypeKey.DAILY_STANDUP,
    "stand up": CeremonyTypeKey.DAILY_STANDUP,
    "daily": CeremonyTypeKey.DAILY_STANDUP,
    "sprint_planning": CeremonyTypeKey.SPRINT_PLANNING,
    "sprint planning": CeremonyTypeKey.SPRINT_PLANNING,
    "planning": CeremonyTypeKey.SPRINT_PLANNING,
    "sprint_review": CeremonyTypeKey.SPRINT_REVIEW,
    "sprint review": CeremonyTypeKey.SPRINT_REVIEW,
    "review": CeremonyTypeKey.SPRINT_REVIEW,
    "demo": CeremonyTypeKey.SPRINT_REVIEW,
    "sprint demo": CeremonyTypeKey.SPRINT_REVIEW,
    "retrospective": CeremonyTypeKey.RETROSPECTIVE,
    "retro": CeremonyTypeKey.RETROSPECTIVE,
    "sprint retro": CeremonyTypeKey.RETROSPECTIVE,
    "sprint retrospective": CeremonyTypeKey.RETROSPECTIVE,
    "open_qa": CeremonyTypeKey.OPEN_QA,
    "open qa": CeremonyTypeKey.OPEN_QA,
    "open q&a": CeremonyTypeKey.OPEN_QA,
    "q&a": CeremonyTypeKey.OPEN_QA,
    "qa": CeremonyTypeKey.OPEN_QA,
    "q and a": CeremonyTypeKey.OPEN_QA,
    "question and answer": CeremonyTypeKey.OPEN_QA,
    "questions and answers": CeremonyTypeKey.OPEN_QA,
    "office hours": CeremonyTypeKey.OPEN_QA,
}


class MembershipStatus(StrEnum):
    """Lifecycle of a cohort membership."""

    ACTIVE = "active"
    INACTIVE = "inactive"


class SprintStatus(StrEnum):
    """Lifecycle of a sprint."""

    PLANNED = "planned"
    ACTIVE = "active"
    COMPLETED = "completed"


class CeremonyStatus(StrEnum):
    """Lifecycle of a scheduled ceremony."""

    SCHEDULED = "scheduled"
    CANCELLED = "cancelled"
    COMPLETED = "completed"


class EscalationType(StrEnum):
    """Which kind of human an escalation is routed to."""

    TECH = "tech"
    OPS = "ops"


class EscalationStatus(StrEnum):
    """Closed lifecycle set; every transition is timestamped in ``status_changed_at``."""

    OPEN = "open"
    WAITING_HUMAN = "waiting_human"
    RESOLVED = "resolved"


class OnboardingStepKind(StrEnum):
    """The deliveries that make up an onboarding journey."""

    WELCOME = "welcome"
    ORIENTATION = "orientation"
    FOLLOW_UP = "follow_up"


class OnboardingStepStatus(StrEnum):
    """Delivery state of one onboarding step."""

    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"
    HALTED = "halted"


def normalise_role_key(value: str) -> RoleKey | None:
    """Map a typed role name to its machine key.

    Args:
        value: Anything a person might write: ``Tech Lead``, ``tech_lead``, ``scrum master``.

    Returns:
        RoleKey | None: The key, or None when the text matches no known role.
    """
    return ROLE_ALIASES.get(value.strip().lower().replace("-", " "))


def normalise_ceremony_type_key(value: str) -> CeremonyTypeKey | None:
    """Map a typed ceremony type to its machine key.

    Args:
        value: Anything a person might write: ``retro``, ``Sprint Planning``, ``q&a``.

    Returns:
        CeremonyTypeKey | None: The key, or None when the text matches no known type.
    """
    return CEREMONY_TYPE_ALIASES.get(value.strip().lower())
