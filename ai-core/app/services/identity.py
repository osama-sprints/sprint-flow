"""Services for resolving Mattermost identities to domain persons """
from sqlmodel import Session, select
from app.models.person import Person
from app.services.database import database_service

def get_person_by_handle(handle: str) -> Person | None:
    """Resolve a person by their Mattermost handle."""
    with Session(database_service.engine) as session:
        return session.exec(
            select(Person).where(Person.handle == handle)
        ).first()

def get_person_by_mattermost_id(mattermost_user_id: str) -> Person | None:
    """Resolve a person by their Mattermost user ID."""
    with Session(database_service.engine) as session:
        return session.exec(
            select(Person).where(Person.mattermost_user_id == mattermost_user_id)
        ).first()