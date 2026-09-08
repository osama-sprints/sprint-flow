"""Ceremony types, scheduled ceremonies and their amendment trail."""

from datetime import (
    datetime,
    timedelta,
)
from typing import Any

from sqlalchemy import func
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models import (
    Ceremony,
    CeremonyAmendment,
    CeremonyType,
    require_aware,
    utcnow,
)
from app.models.enums import (
    CeremonyStatus,
    CeremonyTypeKey,
)
from app.services.database import session_scope

# Columns an amendment may change. Everything else on a ceremony is identity.
AMENDABLE_FIELDS: frozenset[str] = frozenset(
    {
        "scheduled_at",
        "duration_minutes",
        "agenda",
        "notes",
        "status",
        "time_expression",
        "time_zone",
        "channel_id",
        "meet_link",
    }
)


async def get_ceremony_type_by_key(
    key: str | CeremonyTypeKey, session: AsyncSession | None = None
) -> CeremonyType | None:
    """Fetch a ceremony type by machine key.

    Args:
        key: ``daily_standup``, ``sprint_planning``, ...
        session: Optional session to reuse.

    Returns:
        CeremonyType | None: The row, or None.
    """
    async with session_scope(session) as s:
        result = await s.exec(select(CeremonyType).where(CeremonyType.key == str(key)))
        return result.first()


async def get_ceremony_type(ceremony_type_id: int, session: AsyncSession | None = None) -> CeremonyType | None:
    """Fetch a ceremony type by id.

    Args:
        ceremony_type_id: ``ceremony_types.id``.
        session: Optional session to reuse.

    Returns:
        CeremonyType | None: The row, or None.
    """
    async with session_scope(session) as s:
        return await s.get(CeremonyType, ceremony_type_id)


async def list_ceremony_types(session: AsyncSession | None = None) -> list[CeremonyType]:
    """List every ceremony type.

    Args:
        session: Optional session to reuse.

    Returns:
        list[CeremonyType]: All rows, by id.
    """
    async with session_scope(session) as s:
        result = await s.exec(select(CeremonyType).order_by(CeremonyType.id))  # type: ignore[arg-type]
        return list(result.all())


async def create_ceremony(
    *,
    team_id: str,
    channel_id: str,
    ceremony_type_id: int,
    organizer_id: int,
    scheduled_at: datetime,
    duration_minutes: int,
    agenda: str | None = None,
    sprint_id: int | None = None,
    time_expression: str | None = None,
    time_zone: str | None = None,
    meet_link: str | None = None,
    session: AsyncSession | None = None,
) -> Ceremony:
    """Persist a ceremony. Conflict and authorisation checks belong to the caller.

    Args:
        team_id: The team.
        channel_id: The channel.
        ceremony_type_id: The kind of ceremony.
        organizer_id: The person scheduling it (from stored identity).
        scheduled_at: Timezone-aware start instant.
        duration_minutes: Length.
        agenda: Free text.
        sprint_id: The sprint, if named.
        time_expression: What the organiser typed.
        time_zone: IANA zone used for interpretation.
        meet_link: Google Meet join URL, if attached.
        session: Optional session to reuse.

    Returns:
        Ceremony: The stored row.

    Raises:
        ValueError: If ``scheduled_at`` is naive.
    """
    require_aware(scheduled_at, "scheduled_at")
    ceremony = Ceremony(
        team_id=team_id,
        channel_id=channel_id,
        ceremony_type_id=ceremony_type_id,
        organizer_id=organizer_id,
        scheduled_at=scheduled_at,
        duration_minutes=duration_minutes,
        agenda=agenda,
        sprint_id=sprint_id,
        time_expression=time_expression,
        time_zone=time_zone,
        meet_link=meet_link,
    )
    async with session_scope(session) as s:
        s.add(ceremony)
        await s.flush()
        await s.refresh(ceremony)
        return ceremony


async def get_ceremony(ceremony_id: int, session: AsyncSession | None = None) -> Ceremony | None:
    """Fetch a ceremony by id.

    Args:
        ceremony_id: ``ceremonies.id``.
        session: Optional session to reuse.

    Returns:
        Ceremony | None: The row, or None.
    """
    async with session_scope(session) as s:
        return await s.get(Ceremony, ceremony_id)


async def list_ceremonies(
    channel_id: str,
    *,
    include_past: bool = False,
    include_cancelled: bool = False,
    now: datetime | None = None,
    session: AsyncSession | None = None,
) -> list[Ceremony]:
    """A channel's calendar, soonest first.

    Args:
        channel_id: The channel.
        include_past: Also return ceremonies that already started.
        include_cancelled: Also return cancelled ceremonies.
        now: The reference instant (defaults to now, UTC).
        session: Optional session to reuse.

    Returns:
        list[Ceremony]: Matching rows.
    """
    reference = require_aware(now, "now") if now is not None else utcnow()
    statement = select(Ceremony).where(Ceremony.channel_id == channel_id).order_by(Ceremony.scheduled_at)  # type: ignore[arg-type]
    if not include_past:
        statement = statement.where(Ceremony.scheduled_at >= reference)
    if not include_cancelled:
        statement = statement.where(Ceremony.status != CeremonyStatus.CANCELLED.value)
    async with session_scope(session) as s:
        result = await s.exec(statement)
        return list(result.all())


async def list_upcoming_ceremonies(
    *,
    within: timedelta,
    now: datetime | None = None,
    session: AsyncSession | None = None,
) -> list[Ceremony]:
    """Every scheduled ceremony, across cohorts, starting within a window — the reminder task's query.

    Args:
        within: Look-ahead window.
        now: The reference instant (defaults to now, UTC).
        session: Optional session to reuse.

    Returns:
        list[Ceremony]: Soonest first.
    """
    reference = require_aware(now, "now") if now is not None else utcnow()
    statement = (
        select(Ceremony)
        .where(
            Ceremony.status == CeremonyStatus.SCHEDULED.value,
            Ceremony.scheduled_at >= reference,
            Ceremony.scheduled_at <= reference + within,
        )
        .order_by(Ceremony.scheduled_at)  # type: ignore[arg-type]
    )
    async with session_scope(session) as s:
        result = await s.exec(statement)
        return list(result.all())


async def find_overlapping_ceremonies(
    channel_id: str,
    scheduled_at: datetime,
    duration_minutes: int,
    *,
    exclude_id: int | None = None,
    session: AsyncSession | None = None,
) -> list[Ceremony]:
    """Scheduled ceremonies of the channel whose time range overlaps the candidate's.

    Two ranges overlap when each starts before the other ends. Cancelled and
    completed ceremonies never conflict.

    Args:
        channel_id: The channel.
        scheduled_at: Candidate start (timezone-aware).
        duration_minutes: Candidate length.
        exclude_id: A ceremony to ignore (the one being amended).
        session: Optional session to reuse.

    Returns:
        list[Ceremony]: Conflicting ceremonies, soonest first.

    Raises:
        ValueError: If ``scheduled_at`` is naive.
    """
    require_aware(scheduled_at, "scheduled_at")
    candidate_end = scheduled_at + timedelta(minutes=duration_minutes)
    existing_end = Ceremony.scheduled_at + func.make_interval(0, 0, 0, 0, 0, Ceremony.duration_minutes)  # type: ignore[operator]
    statement = (
        select(Ceremony)
        .where(
            Ceremony.channel_id == channel_id,
            Ceremony.status == CeremonyStatus.SCHEDULED.value,
            Ceremony.scheduled_at < candidate_end,
            existing_end > scheduled_at,
        )
        .order_by(Ceremony.scheduled_at)  # type: ignore[arg-type]
    )
    if exclude_id is not None:
        statement = statement.where(Ceremony.id != exclude_id)
    async with session_scope(session) as s:
        result = await s.exec(statement)
        return list(result.all())


def _render(value: Any) -> str | None:
    """Render a column value for the amendment trail."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


async def update_ceremony(
    ceremony_id: int,
    *,
    amended_by_id: int,
    changes: dict[str, Any],
    reason: str | None = None,
    session: AsyncSession | None = None,
) -> Ceremony | None:
    """Apply changes to a ceremony and record one amendment row per changed field, atomically.

    Args:
        ceremony_id: The ceremony.
        amended_by_id: Who is changing it (from stored identity).
        changes: ``{column: new_value}``; only ``AMENDABLE_FIELDS`` are accepted.
        reason: Free text stored with each amendment.
        session: Optional session to reuse.

    Returns:
        Ceremony | None: The updated row, or None when the ceremony does not exist.

    Raises:
        ValueError: On an unknown column or a naive ``scheduled_at``.
    """
    unknown = set(changes) - AMENDABLE_FIELDS
    if unknown:
        raise ValueError(f"cannot amend fields: {sorted(unknown)}")
    new_instant = changes.get("scheduled_at")
    if isinstance(new_instant, datetime):
        require_aware(new_instant, "scheduled_at")

    async with session_scope(session) as s:
        ceremony = await s.get(Ceremony, ceremony_id)
        if ceremony is None:
            return None
        changed = False
        for field, new_value in changes.items():
            old_value = getattr(ceremony, field)
            if old_value == new_value:
                continue
            setattr(ceremony, field, new_value)
            s.add(
                CeremonyAmendment(
                    ceremony_id=ceremony_id,
                    amended_by_id=amended_by_id,
                    field=field,
                    old_value=_render(old_value),
                    new_value=_render(new_value),
                    reason=reason,
                )
            )
            changed = True
        if changed:
            ceremony.updated_at = utcnow()
            s.add(ceremony)
            await s.flush()
            await s.refresh(ceremony)
        return ceremony


async def list_amendments(ceremony_id: int, session: AsyncSession | None = None) -> list[CeremonyAmendment]:
    """The amendment trail of a ceremony, oldest first.

    Args:
        ceremony_id: The ceremony.
        session: Optional session to reuse.

    Returns:
        list[CeremonyAmendment]: Matching rows.
    """
    async with session_scope(session) as s:
        result = await s.exec(
            select(CeremonyAmendment)
            .where(CeremonyAmendment.ceremony_id == ceremony_id)
            .order_by(CeremonyAmendment.created_at, CeremonyAmendment.id)  # type: ignore[arg-type]
        )
        return list(result.all())
