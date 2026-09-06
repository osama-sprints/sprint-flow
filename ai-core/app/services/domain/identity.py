"""People: create, refresh and look up ``users`` rows.

Every person SprintFlow knows arrives from Mattermost, so the write path is a
single idempotent upsert keyed on ``mattermost_user_id``. Reads resolve a chat
identity (Mattermost id, handle or email) to the one stable internal record.
"""

from datetime import datetime

from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlmodel import (
    col,
    select,
)
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models import (
    User,
    require_aware,
    utcnow,
)
from app.services.database import session_scope


async def get_user(user_id: int, session: AsyncSession | None = None) -> User | None:
    """Fetch a person by internal id.

    Args:
        user_id: ``users.id``.
        session: Optional session to reuse.

    Returns:
        User | None: The row, or None.
    """
    async with session_scope(session) as s:
        return await s.get(User, user_id)


async def get_user_by_mattermost_id(mattermost_user_id: str, session: AsyncSession | None = None) -> User | None:
    """Resolve a Mattermost user id to the internal record.

    Args:
        mattermost_user_id: The id carried on every Mattermost event.
        session: Optional session to reuse.

    Returns:
        User | None: The row, or None when this person was never synced.
    """
    async with session_scope(session) as s:
        result = await s.exec(select(User).where(User.mattermost_user_id == mattermost_user_id))
        return result.first()


async def get_user_by_username(username: str, session: AsyncSession | None = None) -> User | None:
    """Resolve a Mattermost handle (with or without ``@``) to the internal record.

    Args:
        username: The handle as typed.
        session: Optional session to reuse.

    Returns:
        User | None: The most recently synced row with that handle, or None.
    """
    handle = username.strip().lstrip("@").lower()
    if not handle:
        return None
    # PostgreSQL sorts NULL first under DESC, so a never-synced row would win
    # over a freshly synced one without the explicit NULLS LAST.
    statement = (
        select(User)
        .where(func.lower(User.username) == handle)
        .order_by(col(User.last_synced_at).desc().nulls_last(), col(User.id).desc())
    )
    async with session_scope(session) as s:
        result = await s.exec(statement)
        return result.first()


async def get_user_by_email(email: str, session: AsyncSession | None = None) -> User | None:
    """Resolve an email address to the internal record.

    Args:
        email: The address, any case.
        session: Optional session to reuse.

    Returns:
        User | None: The row, or None.
    """
    address = email.strip().lower()
    if not address:
        return None
    async with session_scope(session) as s:
        result = await s.exec(select(User).where(func.lower(User.email) == address))
        return result.first()


async def upsert_mattermost_user(
    *,
    mattermost_user_id: str,
    username: str,
    email: str | None,
    display_name: str | None,
    timezone: str | None,
    is_superadmin: bool,
    synced_at: datetime | None = None,
    session: AsyncSession | None = None,
) -> User:
    """Create or refresh the record for a Mattermost account. Safe to repeat.

    Args:
        mattermost_user_id: The Mattermost user id (conflict key).
        username: Current handle.
        email: Current email, lower-cased by the caller, or None.
        display_name: Friendly name, or None.
        timezone: IANA zone from the profile, or None.
        is_superadmin: Whether the email is on the ``ADMIN_EMAILS`` allowlist.
        synced_at: When the profile was read; defaults to now.
        session: Optional session to reuse.

    Returns:
        User: The stored row after the upsert.
    """
    now = require_aware(synced_at, "synced_at") if synced_at is not None else utcnow()
    values = {
        "mattermost_user_id": mattermost_user_id,
        "username": username,
        "email": email,
        "display_name": display_name,
        "timezone": timezone,
        "is_superadmin": is_superadmin,
        "last_synced_at": now,
        "created_at": now,
        "updated_at": now,
    }
    refresh = {key: value for key, value in values.items() if key not in ("mattermost_user_id", "created_at")}
    statement = (
        pg_insert(User)
        .values(**values)
        .on_conflict_do_update(index_elements=["mattermost_user_id"], set_=refresh)
        .returning(User)
    )
    async with session_scope(session) as s:
        result = await s.exec(statement)
        return result.scalar_one()


async def list_users(session: AsyncSession | None = None) -> list[User]:
    """Return every known person, oldest first.

    Args:
        session: Optional session to reuse.

    Returns:
        list[User]: All rows.
    """
    async with session_scope(session) as s:
        result = await s.exec(select(User).order_by(User.id))  # type: ignore[arg-type]
        return list(result.all())
