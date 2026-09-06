"""Data access for durable rich artifacts.

Two behaviours here are load-bearing beyond ordinary CRUD:

* ``find_turn_publication`` is the reconciliation read. Creating a Mattermost
  post and recording that we created it are two steps; a crash between them
  would republish the reply on the next attempt. The turn id travels in the
  post's props, so the published post can be found again and adopted.
* ``claim_pending`` uses ``FOR UPDATE SKIP LOCKED`` with a lease, the same
  pattern as the onboarding outbox, so two workers partition generation work
  instead of paying for the same image twice.
"""

from datetime import (
    datetime,
    timedelta,
)
from typing import (
    Any,
    Dict,
    List,
    Optional,
    Sequence,
)

from sqlalchemy import or_
from sqlmodel import (
    col,
    select,
)
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models import (
    RichArtifact,
    require_aware,
    utcnow,
)
from app.services.database import session_scope


async def create_artifact(
    artifact: RichArtifact,
    *,
    session: AsyncSession | None = None,
) -> RichArtifact:
    """Persist one staged artifact.

    Args:
        artifact: The row to insert.
        session: Optional session to reuse.

    Returns:
        RichArtifact: The stored row.
    """

    async def _run(db: AsyncSession) -> RichArtifact:
        db.add(artifact)
        await db.flush()
        await db.refresh(artifact)
        return artifact

    if session is not None:
        return await _run(session)
    async with session_scope() as db:
        return await _run(db)


async def get_artifact(artifact_id: str, *, session: AsyncSession | None = None) -> Optional[RichArtifact]:
    """Read one artifact by id.

    Args:
        artifact_id: The artifact's id.
        session: Optional session to reuse.

    Returns:
        RichArtifact | None: The row, or None when it does not exist.
    """

    async def _run(db: AsyncSession) -> Optional[RichArtifact]:
        return await db.get(RichArtifact, artifact_id)

    if session is not None:
        return await _run(session)
    async with session_scope() as db:
        return await _run(db)


async def list_for_turn(turn_id: str, *, session: AsyncSession | None = None) -> List[RichArtifact]:
    """Return every artifact staged by one turn, in creation order.

    Args:
        turn_id: The turn.
        session: Optional session to reuse.

    Returns:
        list[RichArtifact]: The artifacts, oldest first.
    """

    async def _run(db: AsyncSession) -> List[RichArtifact]:
        statement = (
            select(RichArtifact)
            .where(col(RichArtifact.turn_id) == turn_id)
            .order_by(col(RichArtifact.created_at), col(RichArtifact.id))
        )
        return list((await db.exec(statement)).all())

    if session is not None:
        return await _run(session)
    async with session_scope() as db:
        return await _run(db)


async def find_turn_publication(turn_id: str, *, session: AsyncSession | None = None) -> Optional[str]:
    """Return the post a turn already published, if any.

    Args:
        turn_id: The turn.
        session: Optional session to reuse.

    Returns:
        str | None: The published post id, or None when nothing was recorded.
    """

    async def _run(db: AsyncSession) -> Optional[str]:
        statement = (
            select(RichArtifact.post_id)
            .where(col(RichArtifact.turn_id) == turn_id)
            .where(col(RichArtifact.post_id).is_not(None))
            .limit(1)
        )
        return (await db.exec(statement)).first()

    if session is not None:
        return await _run(session)
    async with session_scope() as db:
        return await _run(db)


async def mark_published(
    turn_id: str,
    post_id: str,
    *,
    file_ids: Optional[Sequence[str]] = None,
    session: AsyncSession | None = None,
) -> int:
    """Record the post that published a turn's artifacts.

    Args:
        turn_id: The turn.
        post_id: The created post.
        file_ids: Attachments carried by the post.
        session: Optional session to reuse.

    Returns:
        int: How many rows were updated.
    """

    async def _run(db: AsyncSession) -> int:
        rows = await list_for_turn(turn_id, session=db)
        now = utcnow()
        for row in rows:
            row.post_id = post_id
            row.published_at = now
            if file_ids is not None:
                row.file_ids = list(file_ids)
            db.add(row)
        return len(rows)

    if session is not None:
        return await _run(session)
    async with session_scope() as db:
        return await _run(db)


async def update_content(
    artifact_id: str,
    *,
    status: Optional[str] = None,
    content: Optional[Dict[str, Any]] = None,
    error: Optional[str] = None,
    file_ids: Optional[Sequence[str]] = None,
    bump_revision: bool = True,
    session: AsyncSession | None = None,
) -> Optional[RichArtifact]:
    """Change an artifact after publication and bump its revision.

    Args:
        artifact_id: The artifact.
        status: New status, when it changes.
        content: New content payload.
        error: Failure reason, when it failed.
        file_ids: Attachment ids produced for it.
        bump_revision: Whether this counts as a new revision.
        session: Optional session to reuse.

    Returns:
        RichArtifact | None: The updated row, or None when it is unknown.
    """

    async def _run(db: AsyncSession) -> Optional[RichArtifact]:
        row = await db.get(RichArtifact, artifact_id)
        if row is None:
            return None
        if status is not None:
            row.status = status
        if content is not None:
            row.content = content
        if error is not None:
            row.error = error
        if file_ids is not None:
            row.file_ids = list(file_ids)
        if bump_revision:
            row.revision += 1
        db.add(row)
        return row

    if session is not None:
        return await _run(session)
    async with session_scope() as db:
        return await _run(db)


async def claim_pending(
    *,
    worker_id: str,
    lease_seconds: int,
    kind: str,
    limit: int = 5,
    now: Optional[datetime] = None,
    session: AsyncSession | None = None,
) -> List[RichArtifact]:
    """Atomically claim artifacts waiting for their side effect.

    Args:
        worker_id: Stamped on the rows this worker takes.
        lease_seconds: How long a claim stays exclusive.
        kind: Artifact kind to claim (``image``).
        limit: Maximum rows per call.
        now: Reference instant, defaulting to now.
        session: Optional session to reuse.

    Returns:
        list[RichArtifact]: The claimed rows.
    """
    reference = require_aware(now, "now") if now is not None else utcnow()
    cutoff = reference - timedelta(seconds=lease_seconds)

    async def _run(db: AsyncSession) -> List[RichArtifact]:
        statement = (
            select(RichArtifact)
            .where(col(RichArtifact.kind) == kind)
            .where(col(RichArtifact.status) == "pending")
            .where(or_(col(RichArtifact.next_attempt_at).is_(None), col(RichArtifact.next_attempt_at) <= reference))
            .where(or_(col(RichArtifact.claimed_at).is_(None), col(RichArtifact.claimed_at) <= cutoff))
            .order_by(col(RichArtifact.created_at))
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        rows = list((await db.exec(statement)).all())
        for row in rows:
            row.claimed_at = reference
            row.claimed_by = worker_id
            row.status = "running"
            row.attempt_count += 1
            db.add(row)
        return rows

    if session is not None:
        return await _run(session)
    async with session_scope() as db:
        return await _run(db)


async def record_failure(
    artifact_id: str,
    *,
    error: str,
    retry_at: Optional[datetime] = None,
    session: AsyncSession | None = None,
) -> None:
    """Mark an artifact failed, or schedule another attempt.

    Args:
        artifact_id: The artifact.
        error: What went wrong.
        retry_at: When to try again; None marks it permanently failed.
        session: Optional session to reuse.
    """

    async def _run(db: AsyncSession) -> None:
        row = await db.get(RichArtifact, artifact_id)
        if row is None:
            return
        row.last_error = error[:2000]
        row.claimed_at = None
        row.claimed_by = None
        if retry_at is None:
            row.status = "failed"
            row.error = error[:500]
        else:
            row.status = "pending"
            row.next_attempt_at = require_aware(retry_at, "retry_at")
        db.add(row)

    if session is not None:
        await _run(session)
        return
    async with session_scope() as db:
        await _run(db)
