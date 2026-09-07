"""Data access for the per-page document cache and read coverage."""

from typing import (
    Dict,
    Iterable,
    List,
    Optional,
    Sequence,
    Set,
)

from sqlmodel import (
    col,
    select,
)
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models import (
    DocumentPage,
    DocumentPageRead,
    utcnow,
)
from app.services.database import session_scope


async def upsert_pages(rows: Sequence[DocumentPage], *, session: AsyncSession | None = None) -> int:
    """Store page texts, replacing any row with the same cache key.

    Args:
        rows: Pages to store; ``id`` may be empty and is then generated.
        session: Optional session to reuse.

    Returns:
        int: Rows written.
    """
    if not rows:
        return 0

    async def _run(db: AsyncSession) -> int:
        for row in rows:
            existing = (
                await db.exec(
                    select(DocumentPage).where(
                        col(DocumentPage.attachment_id) == row.attachment_id,
                        col(DocumentPage.sha256) == row.sha256,
                        col(DocumentPage.page_no) == row.page_no,
                        col(DocumentPage.method) == row.method,
                        col(DocumentPage.model) == row.model,
                        col(DocumentPage.render_key) == row.render_key,
                    )
                )
            ).first()
            if existing is not None:
                for name in ("label", "text", "chars", "usable", "warnings", "usage", "latency_ms"):
                    setattr(existing, name, getattr(row, name))
                existing.updated_at = utcnow()
                db.add(existing)
                continue
            db.add(row)
        await db.flush()
        return len(rows)

    if session is not None:
        return await _run(session)
    async with session_scope() as db:
        return await _run(db)


async def pages_for(
    attachment_id: str,
    sha256: str,
    page_numbers: Iterable[int] | None = None,
    *,
    method: str | None = None,
    session: AsyncSession | None = None,
) -> List[DocumentPage]:
    """Read cached page rows for a document revision.

    Args:
        attachment_id: The document.
        sha256: Its revision.
        page_numbers: Restrict to these pages; None for all.
        method: Restrict to native or vision; None for both.
        session: Optional session to reuse.

    Returns:
        list[DocumentPage]: Rows ordered by page then method.
    """
    statement = select(DocumentPage).where(
        col(DocumentPage.attachment_id) == attachment_id, col(DocumentPage.sha256) == sha256
    )
    if page_numbers is not None:
        statement = statement.where(col(DocumentPage.page_no).in_(list(page_numbers)))
    if method is not None:
        statement = statement.where(col(DocumentPage.method) == method)
    statement = statement.order_by(col(DocumentPage.page_no), col(DocumentPage.method))

    async def _run(db: AsyncSession) -> List[DocumentPage]:
        return list((await db.exec(statement)).all())

    if session is not None:
        return await _run(session)
    async with session_scope() as db:
        return await _run(db)


async def record_reads(
    attachment_id: str,
    session_id: str,
    pages: Dict[int, str],
    *,
    session: AsyncSession | None = None,
) -> None:
    """Note that a conversation was shown these pages.

    Args:
        attachment_id: The document.
        session_id: The conversation.
        pages: ``{page_no: method}`` for every page shown.
        session: Optional session to reuse.
    """
    if not pages:
        return
    now = utcnow()

    async def _run(db: AsyncSession) -> None:
        existing = {
            row.page_no: row
            for row in (
                await db.exec(
                    select(DocumentPageRead).where(
                        col(DocumentPageRead.attachment_id) == attachment_id,
                        col(DocumentPageRead.session_id) == session_id,
                        col(DocumentPageRead.page_no).in_(list(pages)),
                    )
                )
            ).all()
        }
        for page_no, method in pages.items():
            row = existing.get(page_no)
            if row is None:
                row = DocumentPageRead(attachment_id=attachment_id, session_id=session_id, page_no=page_no)
            row.method = method
            row.read_at = now
            row.updated_at = now
            db.add(row)
        await db.flush()

    if session is not None:
        return await _run(session)
    async with session_scope() as db:
        return await _run(db)


async def pages_read(attachment_id: str, session_id: str, *, session: AsyncSession | None = None) -> Set[int]:
    """Pages a conversation has been shown.

    Args:
        attachment_id: The document.
        session_id: The conversation.
        session: Optional session to reuse.

    Returns:
        set[int]: Physical page numbers.
    """
    statement = select(DocumentPageRead.page_no).where(
        col(DocumentPageRead.attachment_id) == attachment_id, col(DocumentPageRead.session_id) == session_id
    )

    async def _run(db: AsyncSession) -> Set[int]:
        return set((await db.exec(statement)).all())

    if session is not None:
        return await _run(session)
    async with session_scope() as db:
        return await _run(db)


async def first_page_without_text(
    attachment_id: str, sha256: str, *, session: AsyncSession | None = None
) -> Optional[int]:
    """Lowest page number with no usable text from any method, if any is cached.

    Args:
        attachment_id: The document.
        sha256: Its revision.
        session: Optional session to reuse.

    Returns:
        int | None: A page number, or None when every cached page is usable.
    """
    rows = await pages_for(attachment_id, sha256, session=session)
    usable = {row.page_no for row in rows if row.usable}
    seen = {row.page_no for row in rows}
    missing = sorted(seen - usable)
    return missing[0] if missing else None
