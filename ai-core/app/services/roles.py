"""Services for resolving cohort-scoped roles."""

from sqlmodel import Session, select
from app.models.cohort_membership import CohortMembership
from app.models.role import Role
from app.services.database import database_service

def get_role_for_person_in_cohort(
    person_id: int,
    cohort_id: int,
) -> Role | None:
    """Resolve a person's role within a specific cohort."""
    with Session(database_service.engine) as session:
        statement = (
            select(Role)
            .join(CohortMembership)
            .where(
                CohortMembership.person_id == person_id,
                CohortMembership.cohort_id == cohort_id,
            )
        )
        return session.exec(statement).first()