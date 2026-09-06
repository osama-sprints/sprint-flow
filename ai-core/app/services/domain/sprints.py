"""Time-boxed sprints per cohort."""

from datetime import date

from sqlalchemy import func
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models import (
    Sprint,
    utcnow,
)
from app.models.enums import SprintStatus
from app.services.database import session_scope


async def create_sprint(
    *,
    cohort_id: int,
    name: str,
    start_date: date,
    end_date: date,
    status: SprintStatus = SprintStatus.ACTIVE,
    opened_by_id: int | None = None,
    session: AsyncSession | None = None,
) -> Sprint:
    """Insert a sprint. Callers check ``get_sprint_by_name`` first for idempotency.

    Args:
        cohort_id: The cohort.
        name: Unique within the cohort.
        start_date: First day.
        end_date: Last day (not before ``start_date``).
        status: Initial status.
        opened_by_id: Who opened it.
        session: Optional session to reuse.

    Returns:
        Sprint: The stored row.
    """
    sprint = Sprint(
        cohort_id=cohort_id,
        name=name.strip(),
        start_date=start_date,
        end_date=end_date,
        status=status.value,
        opened_by_id=opened_by_id,
    )
    async with session_scope(session) as s:
        s.add(sprint)
        await s.flush()
        await s.refresh(sprint)
        return sprint


async def get_sprint(sprint_id: int, session: AsyncSession | None = None) -> Sprint | None:
    """Fetch a sprint by id.

    Args:
        sprint_id: ``sprints.id``.
        session: Optional session to reuse.

    Returns:
        Sprint | None: The row, or None.
    """
    async with session_scope(session) as s:
        return await s.get(Sprint, sprint_id)


async def get_sprint_by_name(cohort_id: int, name: str, session: AsyncSession | None = None) -> Sprint | None:
    """Fetch a sprint by cohort and name, case-insensitively.

    Args:
        cohort_id: The cohort.
        name: The name as typed.
        session: Optional session to reuse.

    Returns:
        Sprint | None: The row, or None.
    """
    wanted = name.strip().lower()
    if not wanted:
        return None
    async with session_scope(session) as s:
        result = await s.exec(select(Sprint).where(Sprint.cohort_id == cohort_id, func.lower(Sprint.name) == wanted))
        return result.first()


async def list_sprints(cohort_id: int, session: AsyncSession | None = None) -> list[Sprint]:
    """List a cohort's sprints by start date.

    Args:
        cohort_id: The cohort.
        session: Optional session to reuse.

    Returns:
        list[Sprint]: Matching rows.
    """
    async with session_scope(session) as s:
        result = await s.exec(
            select(Sprint).where(Sprint.cohort_id == cohort_id).order_by(Sprint.start_date, Sprint.id)  # type: ignore[arg-type]
        )
        return list(result.all())


async def get_active_sprint(cohort_id: int, session: AsyncSession | None = None) -> Sprint | None:
    """The cohort's currently active sprint, if exactly one is open.

    Args:
        cohort_id: The cohort.
        session: Optional session to reuse.

    Returns:
        Sprint | None: The most recently started active sprint, or None.
    """
    async with session_scope(session) as s:
        result = await s.exec(
            select(Sprint)
            .where(Sprint.cohort_id == cohort_id, Sprint.status == SprintStatus.ACTIVE.value)
            .order_by(Sprint.start_date.desc())  # type: ignore[union-attr]
        )
        return result.first()


async def find_overlapping_sprints(
    cohort_id: int,
    start_date: date,
    end_date: date,
    *,
    exclude_id: int | None = None,
    session: AsyncSession | None = None,
) -> list[Sprint]:
    """Sprints of the cohort that are not completed and share at least one day with the range.

    Args:
        cohort_id: The cohort.
        start_date: Candidate first day.
        end_date: Candidate last day.
        exclude_id: A sprint to ignore (the one being amended).
        session: Optional session to reuse.

    Returns:
        list[Sprint]: Overlapping sprints, by start date.
    """
    statement = (
        select(Sprint)
        .where(
            Sprint.cohort_id == cohort_id,
            Sprint.status != SprintStatus.COMPLETED.value,
            Sprint.start_date <= end_date,
            Sprint.end_date >= start_date,
        )
        .order_by(Sprint.start_date)  # type: ignore[arg-type]
    )
    if exclude_id is not None:
        statement = statement.where(Sprint.id != exclude_id)
    async with session_scope(session) as s:
        result = await s.exec(statement)
        return list(result.all())


async def set_sprint_status(
    sprint_id: int, status: SprintStatus, session: AsyncSession | None = None
) -> Sprint | None:
    """Move a sprint through its lifecycle.

    Args:
        sprint_id: The sprint.
        status: New status.
        session: Optional session to reuse.

    Returns:
        Sprint | None: The updated row, or None when absent.
    """
    async with session_scope(session) as s:
        sprint = await s.get(Sprint, sprint_id)
        if sprint is None:
            return None
        sprint.status = status.value
        sprint.updated_at = utcnow()
        s.add(sprint)
        await s.flush()
        await s.refresh(sprint)
        return sprint
