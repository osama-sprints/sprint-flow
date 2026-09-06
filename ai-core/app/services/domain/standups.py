"""Daily progress entries."""

from datetime import date

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models import (
    DailyStandup,
    utcnow,
)
from app.services.database import session_scope


async def upsert_daily_standup(
    *,
    sprint_id: int,
    learner_id: int,
    log_date: date,
    what_i_did: str,
    what_i_will_do: str,
    blockers: str | None = None,
    session: AsyncSession | None = None,
) -> DailyStandup:
    """Record (or overwrite) one learner's entry for one day of a sprint.

    Args:
        sprint_id: The sprint.
        learner_id: The person.
        log_date: The day.
        what_i_did: Progress.
        what_i_will_do: Plan.
        blockers: Blockers, or None.
        session: Optional session to reuse.

    Returns:
        DailyStandup: The stored row.
    """
    async with session_scope(session) as s:
        result = await s.exec(
            select(DailyStandup).where(
                DailyStandup.sprint_id == sprint_id,
                DailyStandup.learner_id == learner_id,
                DailyStandup.log_date == log_date,
            )
        )
        entry = result.first()
        if entry is None:
            entry = DailyStandup(
                sprint_id=sprint_id,
                learner_id=learner_id,
                log_date=log_date,
                what_i_did=what_i_did,
                what_i_will_do=what_i_will_do,
                blockers=blockers,
            )
        else:
            entry.what_i_did = what_i_did
            entry.what_i_will_do = what_i_will_do
            entry.blockers = blockers
            entry.updated_at = utcnow()
        s.add(entry)
        await s.flush()
        await s.refresh(entry)
        return entry


async def get_daily_standup(standup_id: int, session: AsyncSession | None = None) -> DailyStandup | None:
    """Fetch one entry by id.

    Args:
        standup_id: ``daily_standups.id``.
        session: Optional session to reuse.

    Returns:
        DailyStandup | None: The row, or None.
    """
    async with session_scope(session) as s:
        return await s.get(DailyStandup, standup_id)


async def list_daily_standups(
    sprint_id: int,
    *,
    learner_id: int | None = None,
    log_date: date | None = None,
    session: AsyncSession | None = None,
) -> list[DailyStandup]:
    """Entries for a sprint, optionally narrowed to one person or one day.

    Args:
        sprint_id: The sprint.
        learner_id: Only this person's entries.
        log_date: Only this day.
        session: Optional session to reuse.

    Returns:
        list[DailyStandup]: Ordered by day then person.
    """
    statement = select(DailyStandup).where(DailyStandup.sprint_id == sprint_id)
    if learner_id is not None:
        statement = statement.where(DailyStandup.learner_id == learner_id)
    if log_date is not None:
        statement = statement.where(DailyStandup.log_date == log_date)
    statement = statement.order_by(DailyStandup.log_date, DailyStandup.learner_id)  # type: ignore[arg-type]
    async with session_scope(session) as s:
        result = await s.exec(statement)
        return list(result.all())
