from datetime import date
from sqlalchemy import func
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.sprint import Sprint
from app.models.user import User
from app.models import utcnow
from app.models.enums import SprintStatus
from app.services.database import session_scope


async def get_sprint(
    sprint_id: int, 
    session: AsyncSession | None = None
) -> Sprint | None:
    async with session_scope(session) as s:
        return await s.get(Sprint, sprint_id)


async def get_sprint_members_by_role(
    session: AsyncSession | None, 
    sprint_id: int, 
    role: str
) -> list[User]:
    async with session_scope(session) as s:
        statement = select(User).where(
            User.sprint_id == sprint_id, 
            User.role == role,
        )
        result = await s.exec(statement)
        return list(result.all())


async def is_user_in_sprint(
    session: AsyncSession | None, 
    user_id: int, 
    sprint_id: int
) -> bool:
    async with session_scope(session) as s:
        statement = select(User).where(
            User.id == user_id,
            User.sprint_id == sprint_id,  
        )
        result = await s.exec(statement)
        return result.first() is not None