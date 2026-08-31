"""Typed data-access services for SprintFlow domain models."""

from datetime import date, datetime

from sqlmodel import Session, select

from app.models.ceremony import Ceremony
from app.models.cohort import Cohort
from app.models.cohort_membership import CohortMembership
from app.models.daily_progress import DailyProgress
from app.models.escalation import Escalation
from app.models.sprint import Sprint
from app.services.database import database_service


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


def get_membership(
    person_id: int,
    cohort_id: int,
) -> CohortMembership | None:
    """Get a person's membership in a cohort."""
    with Session(database_service.engine) as session:
        statement = select(CohortMembership).where(
            CohortMembership.person_id == person_id,
            CohortMembership.cohort_id == cohort_id,
        )
        return session.exec(statement).first()


def create_membership(
    person_id: int,
    cohort_id: int,
    role_id: int,
    status: str = "active",
) -> CohortMembership:
    """Create a cohort membership with a cohort-scoped role."""
    with Session(database_service.engine) as session:
        membership = CohortMembership(
            person_id=person_id,
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
    status: str | None = None,
    scheduled_at: datetime | None = None,
    duration_mins: int | None = None,
) -> Ceremony:
    """Create a scheduled ceremony."""
    with Session(database_service.engine) as session:
        ceremony = Ceremony(
            cohort_id=cohort_id,
            type_id=type_id,
            status=status,
            scheduled_at=scheduled_at,
            duration_mins=duration_mins,
        )
        session.add(ceremony)
        session.commit()
        session.refresh(ceremony)
        return ceremony


def create_daily_progress(
    sprint_id: int,
    cohort_membership_id: int,
    progress_date: date,
    status: str = "pending",
    notes: str | None = None,
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
            notes=notes,
        )
        session.add(progress)
        session.commit()
        session.refresh(progress)
        return progress


def create_escalation(
    cohort_id: int,
    cohort_membership_id: int,
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
        )
        session.add(escalation)
        session.commit()
        session.refresh(escalation)
        return escalation
