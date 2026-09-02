"""This file contains the database service for the application."""

from typing import (
    Any,
    Dict,
    List,
    Optional,
)

from fastapi import HTTPException
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.pool import QueuePool
from sqlmodel import (
    Session,
    col,
    create_engine,
    select,
)

from app.core.config import (
    Environment,
    settings,
)
from app.core.logging import logger
from app.models.cohort import Cohort
from app.models.cohort_membership import CohortMembership
from app.models.role import Role
from app.models.session import Session as ChatSession
from app.models.sprint import Sprint
from app.models.user import User


class DatabaseService:
    """Service class for database operations.

    This class handles all database operations for Users, Sessions, Cohorts, Roles, and Sprints.
    It uses SQLModel for ORM operations and maintains a connection pool.
    """

    def __init__(self):
        """Initialize database service with connection pool."""
        try:
            # Configure environment-specific database connection pool settings
            pool_size = settings.POSTGRES_POOL_SIZE
            max_overflow = settings.POSTGRES_MAX_OVERFLOW

            # Create engine with appropriate pool configuration
            connection_url = (
                f"postgresql://{settings.POSTGRES_USER}:{settings.POSTGRES_PASSWORD}"
                f"@{settings.POSTGRES_HOST}:{settings.POSTGRES_PORT}/{settings.POSTGRES_DB}"
            )

            self.engine = create_engine(
                connection_url,
                pool_pre_ping=True,
                poolclass=QueuePool,
                pool_size=pool_size,
                max_overflow=max_overflow,
                pool_timeout=30,  # Connection timeout (seconds)
                pool_recycle=1800,  # Recycle connections after 30 minutes
            )

            logger.info(
                "database_initialized",
                environment=settings.ENVIRONMENT.value,
                pool_size=pool_size,
                max_overflow=max_overflow,
            )
        except SQLAlchemyError as e:
            logger.error("database_initialization_error", error=str(e), environment=settings.ENVIRONMENT.value)
            # In production, don't raise - allow app to start even with DB issues
            if settings.ENVIRONMENT != Environment.PRODUCTION:
                raise

    # -------------------------------------------------------------------------
    # USER & SESSION OPERATIONS
    # -------------------------------------------------------------------------

    async def create_user(self, email: str, password: str, username: str | None = None) -> User:
        """Create a new user."""
        with Session(self.engine) as session:
            user = User(email=email, hashed_password=password, username=username)
            session.add(user)
            session.commit()
            session.refresh(user)
            logger.info("user_created", email=email)
            return user

    async def get_user(self, user_id: int | str) -> Optional[User]:
        """Get a user by ID."""
        with Session(self.engine) as session:
            user = session.get(User, str(user_id))
            return user

    async def get_user_by_email(self, email: str) -> Optional[User]:
        """Get a user by email."""
        with Session(self.engine) as session:
            statement = select(User).where(User.email == email)
            user = session.exec(statement).first()
            return user

    async def delete_user_by_email(self, email: str) -> bool:
        """Delete a user by email."""
        with Session(self.engine) as session:
            user = session.exec(select(User).where(User.email == email)).first()
            if not user:
                return False

            session.delete(user)
            session.commit()
            logger.info("user_deleted", email=email)
            return True

    async def create_session(
        self, session_id: str, user_id: int, name: str = "", username: str | None = None
    ) -> ChatSession:
        """Create a new chat session."""
        with Session(self.engine) as session:
            chat_session = ChatSession(id=session_id, user_id=user_id, name=name, username=username)
            session.add(chat_session)
            session.commit()
            session.refresh(chat_session)
            logger.info("session_created", session_id=session_id, user_id=user_id, name=name)
            return chat_session

    async def delete_session(self, session_id: str) -> bool:
        """Delete a session by ID."""
        with Session(self.engine) as session:
            chat_session = session.get(ChatSession, session_id)
            if not chat_session:
                return False

            session.delete(chat_session)
            session.commit()
            logger.info("session_deleted", session_id=session_id)
            return True

    async def get_session(self, session_id: str) -> Optional[ChatSession]:
        """Get a session by ID."""
        with Session(self.engine) as session:
            chat_session = session.get(ChatSession, session_id)
            return chat_session

    async def get_user_sessions(self, user_id: int) -> List[ChatSession]:
        """Get all sessions for a user."""
        with Session(self.engine) as session:
            statement = (
                select(ChatSession).where(col(ChatSession.user_id) == user_id).order_by(col(ChatSession.created_at))
            )
            sessions = session.exec(statement).all()
            return list(sessions)

    async def update_session_name(self, session_id: str, name: str) -> ChatSession:
        """Update a session's name."""
        with Session(self.engine) as session:
            chat_session = session.get(ChatSession, session_id)
            if not chat_session:
                raise HTTPException(status_code=404, detail="Session not found")

            chat_session.name = name
            session.add(chat_session)
            session.commit()
            session.refresh(chat_session)
            logger.info("session_name_updated", session_id=session_id, name=name)
            return chat_session

    # -------------------------------------------------------------------------
    # AUTHORISATION & BACK-OFFICE ADMIN OPERATIONS
    # -------------------------------------------------------------------------

    def get_user_roles(
        self, requester_id: str, cohort_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """Queries user and cohort roles from PostgreSQL."""
        with Session(self.engine) as session:
            statement = select(User).where(
                (User.mattermost_user_id == requester_id) | (User.id == requester_id)
            )
            user = session.exec(statement).first()

            if not user:
                return {"global": [], "cohort_roles": {}}

            global_roles = ["admin"] if getattr(user, "username", "") == "admin" else []

            cohort_roles: Dict[str, list] = {}
            if cohort_id and user.id:
                membership_stmt = select(CohortMembership).where(
                    (CohortMembership.user_id == user.id)
                    & (CohortMembership.cohort_id == cohort_id)
                )
                membership = session.exec(membership_stmt).first()
                if membership:
                    role_obj = session.get(Role, membership.role_id)
                    if role_obj:
                        cohort_roles[cohort_id] = [role_obj.name]

            return {"global": global_roles, "cohort_roles": cohort_roles}

    def get_cohort(self, cohort_id: str) -> Optional[Dict[str, Any]]:
        """Retrieves a cohort by ID."""
        with Session(self.engine) as session:
            cohort = session.get(Cohort, cohort_id)
            if cohort:
                return {"id": cohort.id, "name": cohort.name, "status": cohort.status}
            return None

    def create_cohort(self, cohort_id: str, name: str) -> Dict[str, Any]:
        """Inserts a new cohort record."""
        with Session(self.engine) as session:
            cohort = Cohort(id=cohort_id, name=name, status="ACTIVE")
            session.add(cohort)
            session.commit()
            session.refresh(cohort)
            return {"id": cohort.id, "name": cohort.name, "status": cohort.status}

    def check_user_has_role(self, user_id: str, role: str, cohort_id: str) -> bool:
        """Checks if user holds a specific role in a cohort."""
        with Session(self.engine) as session:
            statement = select(User).where(
                (User.id == user_id) | (User.mattermost_user_id == user_id)
            )
            user = session.exec(statement).first()
            if not user or not user.id:
                return False

            role_obj = session.exec(select(Role).where(Role.name == role)).first()
            if not role_obj or not role_obj.id:
                return False

            membership = session.exec(
                select(CohortMembership).where(
                    (CohortMembership.user_id == user.id)
                    & (CohortMembership.cohort_id == cohort_id)
                    & (CohortMembership.role_id == role_obj.id)
                )
            ).first()

            return membership is not None

    def add_user_role(self, user_id: str, role: str, cohort_id: str) -> Dict[str, Any]:
        """Assigns a role to a user within a cohort via CohortMembership."""
        with Session(self.engine) as session:
            user = session.exec(
                select(User).where((User.id == user_id) | (User.mattermost_user_id == user_id))
            ).first()
            if not user or not user.id:
                raise ValueError(f"User '{user_id}' not found")

            role_obj = session.exec(select(Role).where(Role.name == role)).first()
            if not role_obj or not role_obj.id:
                raise ValueError(f"Role '{role}' not found")

            membership = CohortMembership(
                user_id=user.id, cohort_id=cohort_id, role_id=role_obj.id
            )
            session.add(membership)
            session.commit()
            return {"user_id": user.id, "role": role, "cohort_id": cohort_id}

    def get_sprint_status(self, cohort_id: str, sprint_id: str) -> Optional[str]:
        """Queries sprint status."""
        with Session(self.engine) as session:
            sprint = session.exec(
                select(Sprint).where(
                    (Sprint.id == sprint_id) & (Sprint.cohort_id == cohort_id)
                )
            ).first()
            return sprint.status if sprint else None

    def set_sprint_status(
        self, cohort_id: str, sprint_id: str, status: str
    ) -> Dict[str, Any]:
        """Updates or creates a sprint status."""
        with Session(self.engine) as session:
            sprint = session.exec(
                select(Sprint).where(
                    (Sprint.id == sprint_id) & (Sprint.cohort_id == cohort_id)
                )
            ).first()

            if sprint:
                sprint.status = status
            else:
                sprint = Sprint(
                    id=sprint_id,
                    cohort_id=cohort_id,
                    name=f"Sprint {sprint_id}",
                    status=status,
                )
                session.add(sprint)

            session.commit()
            session.refresh(sprint)
            return {"id": sprint.id, "cohort_id": sprint.cohort_id, "status": sprint.status}

    # -------------------------------------------------------------------------
    # UTILITY METHODS
    # -------------------------------------------------------------------------

    def get_session_maker(self):
        """Get a session maker for creating database sessions."""
        return Session(self.engine)

    async def health_check(self) -> bool:
        """Check database connection health."""
        try:
            with Session(self.engine) as session:
                session.exec(select(1)).first()
                return True
        except Exception as e:
            logger.error("database_health_check_failed", error=str(e))
            return False


# Create a singleton instance
database_service = DatabaseService()