"""Data access for execution records.

Every write here is a small, targeted update: the running turn touches its
own row often, and a cancel request from another process must land without
waiting for it.
"""

from datetime import (
    datetime,
    timedelta,
)
from typing import (
    Any,
    List,
    Optional,
    Sequence,
)

from sqlmodel import (
    col,
    select,
)
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models import (
    Execution,
    utcnow,
)
from app.services.database import session_scope

RUNNING = "running"
SUCCEEDED = "succeeded"
FAILED = "failed"
CANCELLED = "cancelled"
FINISHED = (SUCCEEDED, FAILED, CANCELLED)


async def create_execution(row: Execution, *, session: AsyncSession | None = None) -> Execution:
    """Persist a new execution.

    Args:
        row: The row to insert.
        session: Optional session to reuse.

    Returns:
        Execution: The stored row.
    """

    async def _run(db: AsyncSession) -> Execution:
        db.add(row)
        await db.flush()
        await db.refresh(row)
        return row

    if session is not None:
        return await _run(session)
    async with session_scope() as db:
        return await _run(db)


async def get_execution(execution_id: str, *, session: AsyncSession | None = None) -> Optional[Execution]:
    """Read one execution.

    Args:
        execution_id: The turn id.
        session: Optional session to reuse.

    Returns:
        Execution | None: The row, or None.
    """

    async def _run(db: AsyncSession) -> Optional[Execution]:
        return await db.get(Execution, execution_id)

    if session is not None:
        return await _run(session)
    async with session_scope() as db:
        return await _run(db)


async def update_execution(
    execution_id: str, *, session: AsyncSession | None = None, **fields: Any
) -> Optional[Execution]:
    """Apply a partial update.

    Args:
        execution_id: The turn id.
        session: Optional session to reuse.
        **fields: Column values to set.

    Returns:
        Execution | None: The updated row, or None when it does not exist.
    """

    async def _run(db: AsyncSession) -> Optional[Execution]:
        row = await db.get(Execution, execution_id)
        if row is None:
            return None
        for name, value in fields.items():
            setattr(row, name, value)
        row.updated_at = utcnow()
        db.add(row)
        await db.flush()
        await db.refresh(row)
        return row

    if session is not None:
        return await _run(session)
    async with session_scope() as db:
        return await _run(db)


async def request_cancel(
    execution_id: str, by_mattermost_user_id: str, *, session: AsyncSession | None = None
) -> Optional[Execution]:
    """Record that a person asked for the turn to stop.

    Args:
        execution_id: The turn id.
        by_mattermost_user_id: Who asked.
        session: Optional session to reuse.

    Returns:
        Execution | None: The row, or None when it does not exist.
    """
    return await update_execution(
        execution_id,
        session=session,
        cancel_requested_at=utcnow(),
        cancel_requested_by=by_mattermost_user_id,
    )


async def find_running_for_session(session_id: str, *, session: AsyncSession | None = None) -> List[Execution]:
    """Executions still running in a conversation, oldest first.

    Args:
        session_id: LangGraph thread id.
        session: Optional session to reuse.

    Returns:
        list[Execution]: Running rows.
    """
    statement = (
        select(Execution)
        .where(col(Execution.session_id) == session_id, col(Execution.status) == RUNNING)
        .order_by(col(Execution.created_at))
    )

    async def _run(db: AsyncSession) -> List[Execution]:
        return list((await db.exec(statement)).all())

    if session is not None:
        return await _run(session)
    async with session_scope() as db:
        return await _run(db)


async def latest_for_session(
    session_id: str,
    statuses: Sequence[str] = FINISHED,
    *,
    session: AsyncSession | None = None,
) -> Optional[Execution]:
    """The most recent execution of a conversation in one of the given states.

    Args:
        session_id: LangGraph thread id.
        statuses: States to consider.
        session: Optional session to reuse.

    Returns:
        Execution | None: The newest matching row.
    """
    statement = (
        select(Execution)
        .where(col(Execution.session_id) == session_id, col(Execution.status).in_(list(statuses)))
        .order_by(col(Execution.created_at).desc())
        .limit(1)
    )

    async def _run(db: AsyncSession) -> Optional[Execution]:
        return (await db.exec(statement)).first()

    if session is not None:
        return await _run(session)
    async with session_scope() as db:
        return await _run(db)


async def find_running_for_host(hostname: str, *, session: AsyncSession | None = None) -> List[Execution]:
    """Running rows written by any process on a host.

    Args:
        hostname: The host part of ``worker`` (``host:pid``).
        session: Optional session to reuse.

    Returns:
        list[Execution]: Running rows from that host.
    """
    statement = select(Execution).where(col(Execution.status) == RUNNING, col(Execution.worker).like(f"{hostname}:%"))

    async def _run(db: AsyncSession) -> List[Execution]:
        return list((await db.exec(statement)).all())

    if session is not None:
        return await _run(session)
    async with session_scope() as db:
        return await _run(db)


async def mark_stale_running_failed(
    *,
    stale_after_seconds: int,
    now: datetime | None = None,
    session: AsyncSession | None = None,
) -> List[Execution]:
    """Fail every running row whose heartbeat stopped long enough ago.

    Args:
        stale_after_seconds: Age of the heartbeat past which a row is stale.
        now: Reference time; defaults to now.
        session: Optional session to reuse.

    Returns:
        list[Execution]: The rows that were failed.
    """
    moment = now or utcnow()
    cutoff = moment - timedelta(seconds=stale_after_seconds)
    statement = select(Execution).where(col(Execution.status) == RUNNING)

    async def _run(db: AsyncSession) -> List[Execution]:
        failed: List[Execution] = []
        for row in (await db.exec(statement)).all():
            last = row.heartbeat_at or row.started_at or row.created_at
            if last is not None and last > cutoff:
                continue
            row.status = FAILED
            row.error = "The assistant restarted while working on this."
            row.finished_at = moment
            row.updated_at = moment
            db.add(row)
            failed.append(row)
        await db.flush()
        return failed

    if session is not None:
        return await _run(session)
    async with session_scope() as db:
        return await _run(db)
