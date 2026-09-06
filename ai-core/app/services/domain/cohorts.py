"""Cohorts, roles and cohort memberships.

This module answers the two questions every privileged action asks first:
"which cohort is meant?" and "what role does this person hold there?".
"""

from typing import NamedTuple

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models import (
    Cohort,
    CohortMembership,
    Role,
    User,
    utcnow,
)
from app.models.enums import (
    MembershipStatus,
    RoleKey,
)
from app.services.database import session_scope


class CohortMember(NamedTuple):
    """One row of a cohort's member list."""

    membership: CohortMembership
    user: User
    role: Role


class MembershipChange(NamedTuple):
    """Outcome of an idempotent role assignment."""

    membership: CohortMembership
    created: bool
    previous_role_id: int | None
    reactivated: bool = False


# ---------------------------------------------------------------------------
# Cohorts
# ---------------------------------------------------------------------------


async def create_cohort(
    name: str,
    *,
    mattermost_team_id: str | None = None,
    mattermost_channel_id: str | None = None,
    created_by_id: int | None = None,
    session: AsyncSession | None = None,
) -> Cohort:
    """Insert a cohort. Callers check for an existing name first (see ``get_cohort_by_name``).

    Args:
        name: Human name; unique case-insensitively at the database.
        mattermost_team_id: Mattermost team id, if known.
        mattermost_channel_id: Cohort channel id, if known.
        created_by_id: The superadmin creating it.
        session: Optional session to reuse.

    Returns:
        Cohort: The stored row.
    """
    cohort = Cohort(
        name=name.strip(),
        mattermost_team_id=mattermost_team_id,
        mattermost_channel_id=mattermost_channel_id,
        created_by_id=created_by_id,
    )
    async with session_scope(session) as s:
        s.add(cohort)
        await s.flush()
        await s.refresh(cohort)
        return cohort


async def get_cohort(cohort_id: int, session: AsyncSession | None = None) -> Cohort | None:
    """Fetch a cohort by id.

    Args:
        cohort_id: ``cohorts.id``.
        session: Optional session to reuse.

    Returns:
        Cohort | None: The row, or None.
    """
    async with session_scope(session) as s:
        return await s.get(Cohort, cohort_id)


async def get_cohort_by_name(name: str, session: AsyncSession | None = None) -> Cohort | None:
    """Fetch a cohort by name, case-insensitively.

    Args:
        name: The name as typed.
        session: Optional session to reuse.

    Returns:
        Cohort | None: The row, or None.
    """
    wanted = name.strip().lower()
    if not wanted:
        return None
    async with session_scope(session) as s:
        result = await s.exec(select(Cohort).where(func.lower(Cohort.name) == wanted))
        return result.first()


async def resolve_cohort(reference: str, session: AsyncSession | None = None) -> Cohort | None:
    """Resolve what a person typed — a numeric id or a name — to a cohort.

    Args:
        reference: ``"7"``, ``"#7"``, ``"Backend-01"`` and so on.
        session: Optional session to reuse.

    Returns:
        Cohort | None: The row, or None when nothing matches.
    """
    text = reference.strip().lstrip("#")
    if not text:
        return None
    if text.isdigit():
        return await get_cohort(int(text), session=session)
    return await get_cohort_by_name(text, session=session)


async def list_cohorts(active_only: bool = False, session: AsyncSession | None = None) -> list[Cohort]:
    """List cohorts, oldest first.

    Args:
        active_only: Only cohorts whose kill switch is not thrown.
        session: Optional session to reuse.

    Returns:
        list[Cohort]: Matching rows.
    """
    statement = select(Cohort).order_by(Cohort.id)  # type: ignore[arg-type]
    if active_only:
        statement = statement.where(Cohort.is_active.is_(True))  # type: ignore[union-attr]
    async with session_scope(session) as s:
        result = await s.exec(statement)
        return list(result.all())


async def set_cohort_active(cohort_id: int, is_active: bool, session: AsyncSession | None = None) -> Cohort | None:
    """Throw or reset the kill switch.

    Args:
        cohort_id: The cohort.
        is_active: New state.
        session: Optional session to reuse.

    Returns:
        Cohort | None: The updated row, or None when the cohort does not exist.
    """
    async with session_scope(session) as s:
        cohort = await s.get(Cohort, cohort_id)
        if cohort is None:
            return None
        cohort.is_active = is_active
        cohort.deactivated_at = None if is_active else utcnow()
        cohort.updated_at = utcnow()
        s.add(cohort)
        await s.flush()
        await s.refresh(cohort)
        return cohort


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
# Memberships
# ---------------------------------------------------------------------------


async def get_membership(user_id: int, cohort_id: int, session: AsyncSession | None = None) -> CohortMembership | None:
    """Fetch a person's membership row in a cohort, whatever its status.

    Args:
        user_id: The person.
        cohort_id: The cohort.
        session: Optional session to reuse.

    Returns:
        CohortMembership | None: The row, or None.
    """
    async with session_scope(session) as s:
        result = await s.exec(
            select(CohortMembership).where(
                CohortMembership.user_id == user_id,
                CohortMembership.cohort_id == cohort_id,
            )
        )
        return result.first()


async def get_role_for_user_in_cohort(
    user_id: int,
    cohort_id: int,
    *,
    active_only: bool = True,
    session: AsyncSession | None = None,
) -> Role | None:
    """Answer "what role does this person hold in this cohort?".

    Args:
        user_id: The person.
        cohort_id: The cohort.
        active_only: Ignore inactive memberships (the default for authorisation).
        session: Optional session to reuse.

    Returns:
        Role | None: The role, or None when the person is not a member.
    """
    statement = (
        select(Role)
        .join(CohortMembership, CohortMembership.role_id == Role.id)  # type: ignore[arg-type]
        .where(CohortMembership.user_id == user_id, CohortMembership.cohort_id == cohort_id)
    )
    if active_only:
        statement = statement.where(CohortMembership.status == MembershipStatus.ACTIVE.value)
    async with session_scope(session) as s:
        result = await s.exec(statement)
        return result.first()


async def list_memberships_for_user(
    user_id: int,
    *,
    active_only: bool = True,
    session: AsyncSession | None = None,
) -> list[tuple[CohortMembership, Cohort, Role]]:
    """Every cohort a person belongs to, with the role held in each.

    Args:
        user_id: The person.
        active_only: Ignore inactive memberships.
        session: Optional session to reuse.

    Returns:
        list[tuple[CohortMembership, Cohort, Role]]: Ordered by join time.
    """
    statement = (
        select(CohortMembership, Cohort, Role)
        .join(Cohort, Cohort.id == CohortMembership.cohort_id)  # type: ignore[arg-type]
        .join(Role, Role.id == CohortMembership.role_id)  # type: ignore[arg-type]
        .where(CohortMembership.user_id == user_id)
        .order_by(CohortMembership.joined_at)  # type: ignore[arg-type]
    )
    if active_only:
        statement = statement.where(CohortMembership.status == MembershipStatus.ACTIVE.value)
    async with session_scope(session) as s:
        result = await s.exec(statement)
        return [(membership, cohort, role) for membership, cohort, role in result.all()]


async def list_cohort_members(
    cohort_id: int,
    *,
    active_only: bool = True,
    session: AsyncSession | None = None,
) -> list[CohortMember]:
    """Everyone in a cohort with their role.

    Args:
        cohort_id: The cohort.
        active_only: Ignore inactive memberships.
        session: Optional session to reuse.

    Returns:
        list[CohortMember]: Ordered by join time.
    """
    statement = (
        select(CohortMembership, User, Role)
        .join(User, User.id == CohortMembership.user_id)  # type: ignore[arg-type]
        .join(Role, Role.id == CohortMembership.role_id)  # type: ignore[arg-type]
        .where(CohortMembership.cohort_id == cohort_id)
        .order_by(CohortMembership.joined_at)  # type: ignore[arg-type]
    )
    if active_only:
        statement = statement.where(CohortMembership.status == MembershipStatus.ACTIVE.value)
    async with session_scope(session) as s:
        result = await s.exec(statement)
        return [CohortMember(membership, user, role) for membership, user, role in result.all()]


async def upsert_membership(
    *,
    user_id: int,
    cohort_id: int,
    role_id: int,
    assigned_by_id: int | None,
    session: AsyncSession | None = None,
) -> MembershipChange:
    """Give a person a role in a cohort, idempotently.

    A second identical call changes nothing and reports ``created=False`` with
    ``previous_role_id == role_id``. A call with a different role replaces the
    role (one role per person per cohort) and reports the previous one.

    Two callers racing to create the same membership (a double-sent message,
    two transports) both converge on the one row: the insert runs inside a
    savepoint, and the loser of the unique-constraint race falls through to
    the update path instead of surfacing an ``IntegrityError``.

    Args:
        user_id: The person.
        cohort_id: The cohort.
        role_id: The role to hold.
        assigned_by_id: Who is assigning it.
        session: Optional session to reuse.

    Returns:
        MembershipChange: The row plus what changed.
    """
    async with session_scope(session) as s:
        existing = await get_membership(user_id, cohort_id, session=s)
        if existing is None:
            membership = CohortMembership(
                user_id=user_id,
                cohort_id=cohort_id,
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
                existing = await get_membership(user_id, cohort_id, session=s)
            else:
                await s.refresh(membership)
                return MembershipChange(membership, True, None)
        if existing is None:
            raise RuntimeError(f"membership for user {user_id} in cohort {cohort_id} vanished mid-upsert")

        previous_role_id = existing.role_id
        if existing.role_id == role_id and existing.status == MembershipStatus.ACTIVE.value:
            return MembershipChange(existing, False, previous_role_id)

        reactivated = existing.status != MembershipStatus.ACTIVE.value
        existing.role_id = role_id
        existing.status = MembershipStatus.ACTIVE.value
        existing.assigned_by_id = assigned_by_id
        existing.updated_at = utcnow()
        s.add(existing)
        await s.flush()
        await s.refresh(existing)
        return MembershipChange(existing, False, previous_role_id, reactivated)


async def set_membership_status(
    user_id: int,
    cohort_id: int,
    status: MembershipStatus,
    session: AsyncSession | None = None,
) -> CohortMembership | None:
    """Activate or deactivate a membership without deleting its history.

    Args:
        user_id: The person.
        cohort_id: The cohort.
        status: New status.
        session: Optional session to reuse.

    Returns:
        CohortMembership | None: The updated row, or None when absent.
    """
    async with session_scope(session) as s:
        membership = await get_membership(user_id, cohort_id, session=s)
        if membership is None:
            return None
        membership.status = status.value
        membership.updated_at = utcnow()
        s.add(membership)
        await s.flush()
        await s.refresh(membership)
        return membership
