"""Data access for attachment records.

Reads are always scoped to a conversation: the tools that expose stored text
pass the session and channel of the turn they run in, taken from the trusted
turn context, and a file from any other conversation is simply not found.
"""

from datetime import datetime
from typing import (
    List,
    Optional,
    Sequence,
)

from sqlalchemy import delete
from sqlmodel import (
    col,
    select,
)
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models import (
    Attachment,
    utcnow,
)
from app.services.database import session_scope


async def save_attachments(rows: Sequence[Attachment], *, session: AsyncSession | None = None) -> int:
    """Insert or replace attachment records.

    A retried turn ingests the same file ids again, so this merges on the
    primary key instead of failing on the second attempt.

    Args:
        rows: The records to store.
        session: Optional session to reuse.

    Returns:
        int: Number of rows written.
    """
    if not rows:
        return 0

    async def _run(db: AsyncSession) -> int:
        for row in rows:
            await db.merge(row)
        await db.flush()
        return len(rows)

    if session is not None:
        return await _run(session)
    async with session_scope() as db:
        return await _run(db)


async def list_for_conversation(
    session_id: str,
    channel_id: str,
    *,
    limit: int = 50,
    session: AsyncSession | None = None,
) -> List[Attachment]:
    """List the attachments a conversation has received, newest last.

    Args:
        session_id: LangGraph thread id of the conversation.
        channel_id: Channel the conversation lives in.
        limit: Maximum rows.
        session: Optional session to reuse.

    Returns:
        list[Attachment]: Matching rows in arrival order.
    """
    statement = (
        select(Attachment)
        .where(col(Attachment.session_id) == session_id, col(Attachment.channel_id) == channel_id)
        .order_by(col(Attachment.created_at).desc())
        .limit(limit)
    )

    async def _run(db: AsyncSession) -> List[Attachment]:
        rows = list((await db.exec(statement)).all())
        rows.reverse()
        return rows

    if session is not None:
        return await _run(session)
    async with session_scope() as db:
        return await _run(db)


async def get_for_conversation(
    attachment_id: str,
    session_id: str,
    channel_id: str,
    *,
    session: AsyncSession | None = None,
) -> Optional[Attachment]:
    """Read one attachment, only if it belongs to the given conversation.

    Args:
        attachment_id: The Mattermost file id.
        session_id: LangGraph thread id of the conversation asking.
        channel_id: Channel of that conversation.
        session: Optional session to reuse.

    Returns:
        Attachment | None: The row, or None when it does not exist or belongs
        to another conversation — the two cases are deliberately identical.
    """

    async def _run(db: AsyncSession) -> Optional[Attachment]:
        row = await db.get(Attachment, attachment_id)
        if row is None or row.session_id != session_id or row.channel_id != channel_id:
            return None
        return row

    if session is not None:
        return await _run(session)
    async with session_scope() as db:
        return await _run(db)


async def purge_expired(now: datetime | None = None, *, session: AsyncSession | None = None) -> int:
    """Delete every record whose retention has lapsed.

    Args:
        now: The reference time; defaults to the current UTC time.
        session: Optional session to reuse.

    Returns:
        int: Rows deleted.
    """
    moment = now or utcnow()
    statement = delete(Attachment).where(col(Attachment.expires_at).is_not(None), col(Attachment.expires_at) < moment)

    async def _run(db: AsyncSession) -> int:
        result = await db.exec(statement)
        return int(getattr(result, "rowcount", 0) or 0)

    if session is not None:
        return await _run(session)
    async with session_scope() as db:
        return await _run(db)
