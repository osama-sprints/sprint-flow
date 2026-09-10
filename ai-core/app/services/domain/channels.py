"""Channels, roles and channel roles.

This module answers the two questions every privileged action asks first:
"which channel is meant?" and "what role does this person hold there?".
"""

from typing import NamedTuple

from sqlalchemy.exc import IntegrityError
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models import (
    ChannelRole,
    Role,
    User,
    utcnow,
)
from app.models.enums import (
    MembershipStatus,
    RoleKey,
)
from app.services.database import session_scope


class ChannelMember(NamedTuple):
    """One row of a channel's role list."""

    role_assignment: ChannelRole
    user: User
    role: Role


class RoleChange(NamedTuple):
    """Outcome of an idempotent role assignment."""

    role_assignment: ChannelRole
    created: bool
    previous_role_id: int | None
    reactivated: bool = False


# ---------------------------------------------------------------------------
# Roles
# ---------------------------------------------------------------------------


async def get_role_by_key(key: str | RoleKey, session: AsyncSession | None = None) -> Role | None:
    """Fetch a role by machine key.

    Args:
        key: ``learner``, ``tech_lead``, ...
        session: Optional session to reuse.

    Returns:
        Role | None: The row, or None.
    """
    async with session_scope(session) as s:
        result = await s.exec(select(Role).where(Role.key == str(key)))
        return result.first()


async def get_role(role_id: int, session: AsyncSession | None = None) -> Role | None:
    """Fetch a role by id.

    Args:
        role_id: ``roles.id``.
        session: Optional session to reuse.

    Returns:
        Role | None: The row, or None.
    """
    async with session_scope(session) as s:
        return await s.get(Role, role_id)


async def list_roles(session: AsyncSession | None = None) -> list[Role]:
    """List every role.

    Args:
        session: Optional session to reuse.

    Returns:
        list[Role]: All rows, by id.
    """
    async with session_scope(session) as s:
        result = await s.exec(select(Role).order_by(Role.id))  # type: ignore[arg-type]
        return list(result.all())


# ---------------------------------------------------------------------------
# Memberships / Channel Roles
# ---------------------------------------------------------------------------


async def get_channel_role(user_id: int, channel_id: str, session: AsyncSession | None = None) -> ChannelRole | None:
    """Fetch a person's role row in a channel, whatever its status.

    Args:
        user_id: The person.
        channel_id: The channel.
        session: Optional session to reuse.

    Returns:
        ChannelRole | None: The row, or None.
    """
    async with session_scope(session) as s:
        result = await s.exec(
            select(ChannelRole).where(
                ChannelRole.user_id == user_id,
                ChannelRole.channel_id == channel_id,
            )
        )
        return result.first()


async def get_role_for_user_in_channel(
    user_id: int,
    channel_id: str,
    *,
    active_only: bool = True,
    session: AsyncSession | None = None,
) -> Role | None:
    """Answer "what role does this person hold in this channel?".

    Args:
        user_id: The person.
        channel_id: The channel.
        active_only: Ignore inactive roles (the default for authorisation).
        session: Optional session to reuse.

    Returns:
        Role | None: The role, or None when the person is not assigned one.
    """
    statement = (
        select(Role)
        .join(ChannelRole, ChannelRole.role_id == Role.id)  # type: ignore[arg-type]
        .where(ChannelRole.user_id == user_id, ChannelRole.channel_id == channel_id)
    )
    if active_only:
        statement = statement.where(ChannelRole.status == MembershipStatus.ACTIVE.value)
    async with session_scope(session) as s:
        result = await s.exec(statement)
        return result.first()


async def list_roles_for_user(
    user_id: int,
    *,
    active_only: bool = True,
    session: AsyncSession | None = None,
) -> list[tuple[ChannelRole, Role]]:
    """Every channel role a person holds.

    Args:
        user_id: The person.
        active_only: Ignore inactive roles.
        session: Optional session to reuse.

    Returns:
        list[tuple[ChannelRole, Role]]: Ordered by join time.
    """
    statement = (
        select(ChannelRole, Role)
        .join(Role, Role.id == ChannelRole.role_id)  # type: ignore[arg-type]
        .where(ChannelRole.user_id == user_id)
        .order_by(ChannelRole.joined_at)  # type: ignore[arg-type]
    )
    if active_only:
        statement = statement.where(ChannelRole.status == MembershipStatus.ACTIVE.value)
    async with session_scope(session) as s:
        result = await s.exec(statement)
        return [(membership, role) for membership, role in result.all()]


async def list_channel_roles(
    channel_id: str,
    *,
    active_only: bool = True,
    session: AsyncSession | None = None,
) -> list[ChannelMember]:
    """Everyone in a channel who holds a role.

    Args:
        channel_id: The channel.
        active_only: Ignore inactive roles.
        session: Optional session to reuse.

    Returns:
        list[ChannelMember]: Ordered by join time.
    """
    statement = (
        select(ChannelRole, User, Role)
        .join(User, User.id == ChannelRole.user_id)  # type: ignore[arg-type]
        .join(Role, Role.id == ChannelRole.role_id)  # type: ignore[arg-type]
        .where(ChannelRole.channel_id == channel_id)
        .order_by(ChannelRole.joined_at)  # type: ignore[arg-type]
    )
    if active_only:
        statement = statement.where(ChannelRole.status == MembershipStatus.ACTIVE.value)
    async with session_scope(session) as s:
        result = await s.exec(statement)
        return [ChannelMember(membership, user, role) for membership, user, role in result.all()]


async def upsert_channel_role(
    *,
    user_id: int,
    team_id: str,
    channel_id: str,
    role_id: int,
    assigned_by_id: int | None,
    session: AsyncSession | None = None,
) -> RoleChange:
    """Give a person a role in a channel, idempotently.

    A second identical call changes nothing and reports ``created=False`` with
    ``previous_role_id == role_id``. A call with a different role replaces the
    role (one role per person per channel) and reports the previous one.

    Two callers racing to create the same membership (a double-sent message,
    two transports) both converge on the one row: the insert runs inside a
    savepoint, and the loser of the unique-constraint race falls through to
    the update path instead of surfacing an ``IntegrityError``.

    Args:
        user_id: The person.
        team_id: The team.
        channel_id: The channel.
        role_id: The role to hold.
        assigned_by_id: Who is assigning it.
        session: Optional session to reuse.

    Returns:
        RoleChange: The row plus what changed.
    """
    async with session_scope(session) as s:
        existing = await get_channel_role(user_id, channel_id, session=s)
        if existing is None:
            membership = ChannelRole(
                user_id=user_id,
                team_id=team_id,
                channel_id=channel_id,
                role_id=role_id,
                assigned_by_id=assigned_by_id,
            )
            try:
                async with s.begin_nested():
                    s.add(membership)
                    await s.flush()
            except IntegrityError:
                # Someone else inserted the row between our SELECT and INSERT.
                # Rolling back the savepoint discarded only the failed insert
                # (and the pending instance); re-read — the competing
                # transaction has committed, or PostgreSQL would not have
                # reported the conflict — and continue as an update.
                existing = await get_channel_role(user_id, channel_id, session=s)
            else:
                await s.refresh(membership)
                return RoleChange(membership, True, None)
        if existing is None:
            raise RuntimeError(f"role for user {user_id} in channel {channel_id} vanished mid-upsert")

        previous_role_id = existing.role_id
        if existing.role_id == role_id and existing.status == MembershipStatus.ACTIVE.value:
            return RoleChange(existing, False, previous_role_id)

        reactivated = existing.status != MembershipStatus.ACTIVE.value
        existing.role_id = role_id
        existing.status = MembershipStatus.ACTIVE.value
        existing.assigned_by_id = assigned_by_id
        existing.updated_at = utcnow()
        s.add(existing)
        await s.flush()
        await s.refresh(existing)
        return RoleChange(existing, False, previous_role_id, reactivated)


async def set_channel_role_status(
    user_id: int,
    channel_id: str,
    status: MembershipStatus,
    session: AsyncSession | None = None,
) -> ChannelRole | None:
    """Activate or deactivate a role without deleting its history.

    Args:
        user_id: The person.
        channel_id: The channel.
        status: New status.
        session: Optional session to reuse.

    Returns:
        ChannelRole | None: The updated row, or None when absent.
    """
    async with session_scope(session) as s:
        membership = await get_channel_role(user_id, channel_id, session=s)
        if membership is None:
            return None
        membership.status = status.value
        membership.updated_at = utcnow()
        s.add(membership)
        await s.flush()
        await s.refresh(membership)
        return membership
