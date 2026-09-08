"""Stable import surface for every application-owned ORM model.

Import this package (not the individual modules) wherever complete SQLModel
metadata matters — Alembic's ``env.py`` does — so a table can never be left out
of autogenerate or verification by accident.
"""

from app.models.ceremony import Ceremony
from app.models.ceremony_reminder import CeremonyReminder
from app.models.ceremony_amendment import CeremonyAmendment
from app.models.ceremony_type import CeremonyType
from app.models.cohort import Cohort
from app.models.cohort_membership import CohortMembership
from app.models.daily_standup import DailyStandup
from app.models.domain_base import (
    DomainBase,
    require_aware,
    utcnow,
)
from app.models.escalation_ticket import EscalationTicket
from app.models.onboarding_step import OnboardingStep
from app.models.role import Role
from app.models.sprint import Sprint
from app.models.user import User

# Every application-owned table, in dependency order. Verification scripts and
# the migration compare against this list.
DOMAIN_TABLES: tuple[str, ...] = (
    "users",
    "roles",
    "ceremony_types",
    "cohorts",
    "cohort_memberships",
    "sprints",
    "ceremonies",
    "ceremony_amendments",
    "ceremony_reminders",
    "daily_standups",
    "escalation_tickets",
    "onboarding_steps",
)

__all__ = [
    "DOMAIN_TABLES",
    "Ceremony",
    "CeremonyAmendment",
    "CeremonyReminder",
    "CeremonyType",
    "Cohort",
    "CohortMembership",
    "DailyStandup",
    "DomainBase",
    "EscalationTicket",
    "OnboardingStep",
    "Role",
    "Sprint",
    "User",
    "require_aware",
    "utcnow",
]
