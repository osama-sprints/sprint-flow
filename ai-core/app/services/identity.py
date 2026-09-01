"""Services for resolving Mattermost identities to domain persons """
from sqlmodel import Session, select
from app.models.user import User
from app.services.database import database_service

def get_user_by_handle(handle: str) -> User | None:
    """Resolve a user by their Mattermost handle."""
    with Session(database_service.engine) as session:
        return session.exec(
            select(User).where(User.handle == handle)
        ).first()

def get_user_by_mattermost_id(mattermost_user_id: str) -> User | None:
    """Resolve a user by their Mattermost user ID."""
    with Session(database_service.engine) as session:
        return session.exec(
            select(User).where(User.mattermost_user_id == mattermost_user_id)
        ).first()
