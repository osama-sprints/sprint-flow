"""Services for resolving cohort-scoped roles."""

from sqlmodel import Session, select
from app.models.cohort_membership import CohortMembership
from app.models.role import Role
from app.services.database import database_service

def get_user_roles(
    user_id: int,
    cohort_id: int,
) -> list[Role]:
    """Get all roles assigned to a user within a cohort."""
    with Session(database_service.engine) as session:
        statement = (
            select(Role)
            .join(CohortMembership)
            .where(
                CohortMembership.user_id == user_id,
                CohortMembership.cohort_id == cohort_id,
            )
        )
        return list(session.exec(statement).all())
