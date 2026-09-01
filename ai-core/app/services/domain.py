"""Typed data-access services for SprintFlow domain models."""

from sqlmodel import Session, select
from datetime import date, datetime, timedelta
from app.models.ceremony_type import CeremonyType
from app.models.ceremony import Ceremony
from app.models.cohort import Cohort
from app.models.cohort_membership import CohortMembership
from app.models.daily_progress import DailyProgress
from app.models.escalation import Escalation
from app.models.sprint import Sprint
from app.services.database import database_service
from app.models.role import Role


def get_cohort(cohort_id: int) -> Cohort | None:
    """Get a cohort by ID."""
    with Session(database_service.engine) as session:
        return session.get(Cohort, cohort_id)


def create_cohort(
    name: str,
    status: str = "active",
    starts_at: datetime | None = None,
    ends_at: datetime | None = None,
) -> Cohort:
    """Create a cohort."""
    with Session(database_service.engine) as session:
        cohort = Cohort(
            name=name,
            status=status,
            starts_at=starts_at,
            ends_at=ends_at,
        )
        session.add(cohort)
        session.commit()
        session.refresh(cohort)
        return cohort


def get_role(role_id: int) -> Role | None:
    """Get a role by ID."""
    with Session(database_service.engine) as session:
        return session.get(Role, role_id)


def create_role(name: str) -> Role:
    """Create a role."""
    with Session(database_service.engine) as session:
        role = Role(name=name)
        session.add(role)
        session.commit()
        session.refresh(role)
        return role

def get_membership(
    user_id: int,
    cohort_id: int,
) -> CohortMembership | None:
    """Get a user's membership in a cohort."""
    with Session(database_service.engine) as session:
        statement = select(CohortMembership).where(
            CohortMembership.user_id == user_id,
            CohortMembership.cohort_id == cohort_id,
        )
        return session.exec(statement).first()


def create_membership(
    user_id: int,
    cohort_id: int,
    role_id: int,
    status: str = "active",
) -> CohortMembership:
    """Create a cohort membership with a cohort-scoped role."""
    with Session(database_service.engine) as session:
        membership = CohortMembership(
            user_id=user_id,
            cohort_id=cohort_id,
            role_id=role_id,
            status=status,
        )
        session.add(membership)
        session.commit()
        session.refresh(membership)
        return membership


def get_sprint(sprint_id: int) -> Sprint | None:
    """Get a sprint by ID."""
    with Session(database_service.engine) as session:
        return session.get(Sprint, sprint_id)


def create_sprint(
    cohort_id: int,
    name: str,
    status: str = "planned",
    starts_at: datetime | None = None,
    ends_at: datetime | None = None,
) -> Sprint:
    """Create a sprint."""
    with Session(database_service.engine) as session:
        sprint = Sprint(
            cohort_id=cohort_id,
            name=name,
            status=status,
            starts_at=starts_at,
            ends_at=ends_at,
        )
        session.add(sprint)
        session.commit()
        session.refresh(sprint)
        return sprint


def create_ceremony(
    cohort_id: int,
    type_id: int,
    scheduled_at: datetime,
    organizer: str,
    raw_input: str | None = None,
    agenda: str | None = None,
    channel_id: str | None = None,
    duration_mins: int | None = None,
) -> Ceremony:
    """Create a scheduled ceremony."""
    with Session(database_service.engine) as session:
        ceremony = Ceremony(
            cohort_id=cohort_id,
            type_id=type_id,
            scheduled_at=scheduled_at,
            organizer=organizer,
            raw_input=raw_input,
            agenda=agenda,
            channel_id=channel_id,
            duration_mins=duration_mins,
        )

        session.add(ceremony)
        session.commit()
        session.refresh(ceremony)

        return ceremony


def get_ceremony(ceremony_id: int) -> Ceremony | None:
    """Get a ceremony by ID."""
    with Session(database_service.engine) as session:
        return session.get(Ceremony, ceremony_id)


def update_ceremony(
    ceremony_id: int,
    scheduled_at: datetime | None = None,
    raw_input: str | None = None,
    agenda: str | None = None,
    status: str | None = None,
) -> Ceremony | None:
    """Update an existing ceremony."""
    with Session(database_service.engine) as session:
        ceremony = session.get(Ceremony, ceremony_id)

        if ceremony is None:
            return None

        if scheduled_at is not None:
            ceremony.scheduled_at = scheduled_at

        if raw_input is not None:
            ceremony.raw_input = raw_input

        if agenda is not None:
            ceremony.agenda = agenda

        if status is not None:
            ceremony.status = status

        session.add(ceremony)
        session.commit()
        session.refresh(ceremony)

        return ceremony


def find_conflicting_ceremony(
    cohort_id: int,
    scheduled_at: datetime,
    exclude_id: int | None = None,
    window_minutes: int = 30,
) -> Ceremony | None:
    """Find an active ceremony within the conflict window."""

    start = scheduled_at - timedelta(minutes=window_minutes)
    end = scheduled_at + timedelta(minutes=window_minutes)

    with Session(database_service.engine) as session:
        statement = select(Ceremony).where(
            Ceremony.cohort_id == cohort_id,
            Ceremony.scheduled_at >= start,
            Ceremony.scheduled_at <= end,
            Ceremony.status != "cancelled",
        )

        if exclude_id is not None:
            statement = statement.where(Ceremony.id != exclude_id)

        return session.exec(statement).first()


def list_ceremonies(
    cohort_id: int,
    include_inactive: bool = False,
) -> list[Ceremony]:
    """List ceremonies for a cohort."""

    with Session(database_service.engine) as session:
        statement = (
            select(Ceremony)
            .where(Ceremony.cohort_id == cohort_id)
            .order_by(Ceremony.scheduled_at)
        )

        if not include_inactive:
            statement = statement.where(Ceremony.status != "cancelled")

        return list(session.exec(statement).all())


def get_or_create_ceremony_type(name: str) -> CeremonyType:
    """Get a ceremony type by name or create it if it does not exist."""

    normalized_name = name.strip().lower()

    with Session(database_service.engine) as session:
        ceremony_type = session.exec(
            select(CeremonyType).where(
                CeremonyType.name == normalized_name
            )
        ).first()

        if ceremony_type is not None:
            return ceremony_type

        ceremony_type = CeremonyType(name=normalized_name)

        session.add(ceremony_type)
        session.commit()
        session.refresh(ceremony_type)

        return ceremony_type


def create_daily_progress(
    sprint_id: int,
    cohort_membership_id: int,
    progress_date: date,
    what_i_did: str,
    what_i_will_do: str,
    blockers: str | None = None,
    status: str = "pending",
) -> DailyProgress:
    """Create a daily progress record."""
    with Session(database_service.engine) as session:
        sprint = session.get(Sprint, sprint_id)
        membership = session.get(CohortMembership, cohort_membership_id)

        if sprint is None:
            raise ValueError(f"Sprint {sprint_id} not found.")

        if membership is None:
            raise ValueError(f"Cohort membership {cohort_membership_id} not found.")

        if sprint.cohort_id != membership.cohort_id:
            raise ValueError(
                "Sprint and cohort membership must belong to the same cohort."
            )
        progress = DailyProgress(
            sprint_id=sprint_id,
            cohort_membership_id=cohort_membership_id,
            date=progress_date,
            status=status,
            what_i_did=what_i_did,
            what_i_will_do=what_i_will_do,
            blockers=blockers,
        )
        session.add(progress)
        session.commit()
        session.refresh(progress)
        return progress

def get_daily_progress(
    progress_id: int,
) -> DailyProgress | None:
    """Get a daily progress record by ID."""
    with Session(database_service.engine) as session:
        return session.get(DailyProgress, progress_id)

def create_escalation(
    cohort_id: int,
    cohort_membership_id: int,
    question: str,
    original_thread_id: str,
    assigned_human_id: int | None = None,
    human_dm_thread_id: str | None = None,
    conversation_id: str | None = None,
    status: str = "pending",
    reason: str | None = None,
    sprint_id: int | None = None,
) -> Escalation:
    """Create an escalation linked to a conversation."""
    with Session(database_service.engine) as session:
        membership = session.get(CohortMembership, cohort_membership_id)
        if membership is None:
            raise ValueError(f"Cohort membership {cohort_membership_id} not found.")

        if membership.cohort_id != cohort_id:
            raise ValueError(
                "Cohort membership must belong to the specified cohort."
            )

        if sprint_id is not None:
            sprint = session.get(Sprint, sprint_id)

            if sprint is None:
                raise ValueError(f"Sprint {sprint_id} not found.")

            if sprint.cohort_id != cohort_id:
                raise ValueError(
                    "Sprint and escalation cohort must belong to the same cohort."
                )
        escalation = Escalation(
            cohort_id=cohort_id,
            cohort_membership_id=cohort_membership_id,
            conversation_id=conversation_id,
            status=status,
            reason=reason,
            sprint_id=sprint_id,
            question=question,
            assigned_human_id=assigned_human_id,
            original_thread_id=original_thread_id,
            human_dm_thread_id=human_dm_thread_id,
        )
        session.add(escalation)
        session.commit()
        session.refresh(escalation)
        return escalation

def get_escalation(
    escalation_id: int,
) -> Escalation | None:
    """Get an escalation by ID."""
    with Session(database_service.engine) as session:
        return session.get(Escalation, escalation_id)

def list_daily_progress(
    sprint_id: int,
    cohort_membership_id: int | None = None,
) -> list[DailyProgress]:
    """List daily progress records for a sprint."""
    with Session(database_service.engine) as session:
        statement = select(DailyProgress).where(
            DailyProgress.sprint_id == sprint_id
        )

        if cohort_membership_id is not None:
            statement = statement.where(
                DailyProgress.cohort_membership_id == cohort_membership_id
            )

        statement = statement.order_by(DailyProgress.date)

        return list(session.exec(statement).all())


def list_escalations(
    cohort_id: int,
    status: str | None = None,
) -> list[Escalation]:
    """List escalations for a cohort."""
    with Session(database_service.engine) as session:
        statement = select(Escalation).where(
            Escalation.cohort_id == cohort_id
        )

        if status is not None:
            statement = statement.where(
                Escalation.status == status
            )

        statement = statement.order_by(Escalation.created_at)

        return list(session.exec(statement).all())


def get_sprint_status(cohort_id: int, sprint_id: int) -> str | None:
    """Get the status of a sprint within a cohort."""
    with Session(database_service.engine) as session:
        sprint = session.get(Sprint, sprint_id)

        if sprint is None or sprint.cohort_id != cohort_id:
            return None

        return sprint.status

def set_sprint_status(
    cohort_id: int,
    sprint_id: int,
    status: str,
) -> Sprint | None:
    """Update the status of a sprint within a cohort."""
    with Session(database_service.engine) as session:
        sprint = session.get(Sprint, sprint_id)

        if sprint is None or sprint.cohort_id != cohort_id:
            return None

        sprint.status = status
        session.add(sprint)
        session.commit()
        session.refresh(sprint)

        return sprint