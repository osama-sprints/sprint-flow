"""What the PDF tools do: authorise, open, read on demand, search, account.

Every entry point re-checks that the document belongs to the conversation
the turn runs in — cache hits included — because a document id is an
identifier, not a capability. Pages are read lazily: native text first when
it is usable, a render-and-transcribe only for pages that need it or when the
agent asks for a visual reading, and never more than the turn's page budget.
Results are bounded and carry continuation, coverage and provenance, so the
agent can keep going without ever holding a whole document in context.
"""

import asyncio
import hashlib
import re
import time
import unicodedata
from contextvars import ContextVar
from dataclasses import (
    dataclass,
    field,
)
from typing import (
    Any,
    Dict,
    List,
    Optional,
)

from app.core.config import settings
from app.core.logging import logger
from app.models import (
    Attachment,
    DocumentPage,
)
from app.services import (
    executions,
    rich_media,
)
from app.services.documents import (
    ocr,
    policy,
)
from app.services.documents.pdf import (
    DocumentCache,
    PageText,
    PdfError,
    compress_ranges,
    extract_text,
    has_labels,
    metadata,
    open_pdf,
    outline,
    page_count,
    render_page,
)
from app.services.domain import attachments as attachments_store
from app.services.domain import document_pages as pages_store
from app.services.mattermost import (
    FileTooLarge,
    mattermost_client,
)

MODES = ("auto", "text", "vision")

_cache = DocumentCache()


class DocumentUnavailable(Exception):
    """No readable PDF with that id exists in this conversation."""


class InvalidRange(ValueError):
    """The page selection is not valid for this document."""


@dataclass
class TurnBudget:
    """Pages rendered and transcribed so far in this turn.

    Attributes:
        used: Pages transcribed (cache hits do not count).
    """

    used: int = 0

    @property
    def remaining(self) -> int:
        """Pages this turn may still transcribe.

        Returns:
            int: Never negative.
        """
        return max(0, settings.PDF_PAGE_BUDGET_PER_TURN - self.used)


_turn_budget: ContextVar[Optional[TurnBudget]] = ContextVar("pdf_turn_budget", default=None)


def begin_turn() -> None:
    """Start the turn's transcription budget."""
    _turn_budget.set(TurnBudget())


def end_turn() -> None:
    """Drop the turn's budget."""
    _turn_budget.set(None)


def _budget() -> TurnBudget:
    budget = _turn_budget.get()
    if budget is None:
        budget = TurnBudget()
        _turn_budget.set(budget)
    return budget


@dataclass
class PageResult:
    """One page as returned to the agent.

    Attributes:
        page_no: Physical page number, 1-based.
        label: Printed label, when defined.
        method: native, vision, or none when nothing could be read.
        text: The text (bounded by the caller).
        warnings: Notes about this page.
        cached: Whether it came from the cache.
        model: Model used for a transcription.
    """

    page_no: int
    label: Optional[str]
    method: str
    text: str
    warnings: List[str] = field(default_factory=list)
    cached: bool = False
    model: str = ""


@dataclass
class ReadResult:
    """A range read.

    Attributes:
        document: The attachment.
        pages: Pages in order.
        requested: ``(start, end)`` as asked.
        served_through: Last page actually included.
        next_page: Where to continue, when the range was not finished.
        not_processed: Pages skipped and why, e.g. ``{"reason": "budget", "pages": "13–20"}``.
        coverage: Pages this conversation has been shown so far.
        total_pages: Document length.
        budget_remaining: Transcriptions left this turn.
    """

    document: Attachment
    pages: List[PageResult]
    requested: tuple[int, int]
    served_through: int
    next_page: Optional[int]
    not_processed: List[Dict[str, str]]
    coverage: str
    total_pages: int
    budget_remaining: int


@dataclass
class Hit:
    """A search hit.

    Attributes:
        page_no: Physical page.
        label: Printed label, when defined.
        method: Where the text came from.
        snippet: Context around the match.
    """

    page_no: int
    label: Optional[str]
    method: str
    snippet: str


@dataclass
class SearchResult:
    """A search over a window of pages.

    Attributes:
        document: The attachment.
        query: As given.
        hits: Matches, in page order.
        searched: ``(first, last)`` page scanned.
        unsearched: Pages in the window with no readable text, as ranges.
        next_cursor: Page to continue from, when pages remain.
        total_pages: Document length.
        capped: Whether the hit limit stopped the scan early.
    """

    document: Attachment
    query: str
    hits: List[Hit]
    searched: tuple[int, int]
    unsearched: str
    next_cursor: Optional[int]
    total_pages: int
    capped: bool


async def _document(document_id: str) -> Attachment:
    context = rich_media.current_rich_media.get()
    if context is None:
        raise DocumentUnavailable("documents can only be read while answering a message")
    row = await attachments_store.get_for_conversation(document_id.strip(), context.session_id, context.channel_id)
    if row is None or row.status != "accepted" or row.kind != "pdf":
        raise DocumentUnavailable("no readable PDF with that id exists in this conversation")
    return row


async def _bytes(row: Attachment) -> bytes:
    key = f"{row.id}:{row.sha256}"
    data = _cache.get(key)
    if data is not None:
        return data
    try:
        data = await mattermost_client.download_file(row.id, max_bytes=settings.FILE_INPUT_MAX_FILE_BYTES)
    except FileTooLarge:
        data = None
    if data is None:
        raise DocumentUnavailable("the file could not be fetched from Mattermost")
    digest = hashlib.sha256(data).hexdigest()
    if row.sha256 and digest != row.sha256:
        logger.warning("pdf_revision_changed", attachment_id=row.id, expected=row.sha256[:12], actual=digest[:12])
        raise DocumentUnavailable("the file changed since it was attached; please send it again")
    _cache.put(key, data)
    return data


def _native_row(row: Attachment, page: PageText) -> DocumentPage:
    return DocumentPage(
        attachment_id=row.id,
        sha256=row.sha256,
        page_no=page.page_no,
        label=page.label,
        method="native",
        model="",
        render_key="",
        text=page.text,
        chars=page.chars,
        usable=page.usable,
        warnings=list(page.warnings),
    )


async def _native_pages(row: Attachment, data: bytes, page_numbers: List[int]) -> Dict[int, DocumentPage]:
    """Native text for the pages, from the cache or extracted now and cached."""
    cached = {
        page.page_no: page for page in await pages_store.pages_for(row.id, row.sha256, page_numbers, method="native")
    }
    missing = [n for n in page_numbers if n not in cached]
    if missing:

        def _extract() -> List[PageText]:
            pdf = open_pdf(data)
            try:
                return extract_text(pdf, missing)
            finally:
                pdf.close()

        rows = [_native_row(row, page) for page in await asyncio.to_thread(_extract)]
        await pages_store.upsert_pages(rows)
        cached.update({r.page_no: r for r in rows})
    return cached


async def _transcribe_page(row: Attachment, data: bytes, page_no: int, label: Optional[str]) -> DocumentPage:
    model = settings.PDF_OCR_MODEL
    started = time.monotonic()

    def _render() -> bytes:
        pdf = open_pdf(data)
        try:
            return render_page(pdf, page_no)
        finally:
            pdf.close()

    image = await asyncio.to_thread(_render)
    warnings: List[str] = []
    latency_ms: Optional[int] = None
    try:
        result = await ocr.transcribe(image, model=model, page_no=page_no)
        text = result.text
        usage = result.usage
        latency_ms = result.latency_ms
        if ocr.UNREADABLE_MARKER in text:
            warnings.append("some text on this page was unreadable")
        usable = text.strip() != ocr.EMPTY_PAGE_MARKER
    except Exception as e:
        logger.warning("pdf_page_transcription_failed", attachment_id=row.id, page=page_no, error=str(e))
        text, usage, usable = "", {"model": model, "error": str(e)[:200]}, False
        warnings.append("the page could not be transcribed")
    return DocumentPage(
        attachment_id=row.id,
        sha256=row.sha256,
        page_no=page_no,
        label=label,
        method="vision",
        model=model,
        render_key=policy.PDF.render_key,
        text=text,
        chars=len(re.sub(r"\s+", "", text)),
        usable=usable,
        warnings=warnings,
        usage=usage,
        # The model call's own latency; rendering is accounted separately.
        latency_ms=latency_ms if latency_ms is not None else int((time.monotonic() - started) * 1000),
    )


def _bound(text: str, limit: int) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    return text[:limit].rstrip() + " …", True


async def inspect(document_id: str) -> Dict[str, Any]:
    """Describe a document: size, metadata, contents, text coverage, reads.

    Args:
        document_id: The attachment id.

    Returns:
        dict: What ``inspect_pdf`` reports.

    Raises:
        DocumentUnavailable: Not this conversation's PDF, or not fetchable.
    """
    row = await _document(document_id)
    facts = dict(row.metadata_ or {})
    total = int(row.page_count or facts.get("page_count") or 0)
    if not facts or not total:
        data = await _bytes(row)

        def _facts() -> Dict[str, Any]:
            pdf = open_pdf(data)
            try:
                return {
                    "page_count": page_count(pdf),
                    **metadata(pdf),
                    "toc": outline(pdf, policy.PDF.toc_entries),
                    "labels": has_labels(pdf),
                }
            finally:
                pdf.close()

        facts = await asyncio.to_thread(_facts)
        total = int(facts["page_count"])

    native = await pages_store.pages_for(row.id, row.sha256, method="native")
    assessed = {p.page_no for p in native}
    # Pages intake did not assess (older records, or a document longer than
    # the intake cap) are assessed now, within the same cap.
    missing = [n for n in range(1, min(total, policy.PDF.intake_native_pages) + 1) if n not in assessed]
    if missing:
        data = await _bytes(row)
        native = list((await _native_pages(row, data, missing)).values()) + native
        assessed = {p.page_no for p in native}
    vision = await pages_store.pages_for(row.id, row.sha256, method="vision")
    with_text = {p.page_no for p in native if p.usable}
    transcribed = {p.page_no for p in vision if p.usable}
    without_text = sorted(assessed - with_text - transcribed)
    context = rich_media.current_rich_media.get()
    read = await pages_store.pages_read(row.id, context.session_id) if context else set()
    unread = [n for n in range(1, total + 1) if n not in read]

    return {
        "id": row.id,
        "name": row.name,
        "pages": total,
        "title": facts.get("title"),
        "author": facts.get("author"),
        "created": facts.get("created"),
        "toc": facts.get("toc") or [],
        "labels": bool(facts.get("labels")),
        "assessed_pages": len(assessed),
        "text_pages": compress_ranges(with_text),
        "text_page_count": len(with_text),
        "no_text_pages": compress_ranges(without_text),
        "no_text_page_count": len(without_text),
        "transcribed_pages": compress_ranges(transcribed),
        "unassessed": total - len(assessed) if total > len(assessed) else 0,
        "read_pages": compress_ranges(read),
        "read_count": len(read),
        "next_unread_page": unread[0] if unread else None,
        "budget_remaining": _budget().remaining,
        "ocr_model": settings.PDF_OCR_MODEL,
    }


async def read_pages(document_id: str, start: int, end: Optional[int], mode: str = "auto") -> ReadResult:
    """Read a page or an inclusive range.

    Args:
        document_id: The attachment id.
        start: First page, 1-based.
        end: Last page, inclusive; None for a single page.
        mode: auto (native text, vision where there is none), text, or vision.

    Returns:
        ReadResult: Pages with provenance, continuation and coverage.

    Raises:
        DocumentUnavailable: Not this conversation's PDF, or not fetchable.
        InvalidRange: Bad page numbers or mode.
    """
    row = await _document(document_id)
    if mode not in MODES:
        raise InvalidRange(f"mode must be one of {', '.join(MODES)}")
    data = await _bytes(row)
    total = int(row.page_count or 0)
    if not total:

        def _count() -> int:
            pdf = open_pdf(data)
            try:
                return page_count(pdf)
            finally:
                pdf.close()

        total = await asyncio.to_thread(_count)

    last = start if end is None else end
    if start < 1 or last < start or start > total:
        raise InvalidRange(f"pages run 1–{total}; {start}–{last} is not a valid range")
    last = min(last, total)
    served_through = min(last, start + policy.PDF.max_pages_per_read - 1)
    numbers = list(range(start, served_through + 1))
    not_processed: List[Dict[str, str]] = []
    if served_through < last:
        not_processed.append({"reason": "range", "pages": compress_ranges(range(served_through + 1, last + 1))})

    native = await _native_pages(row, data, numbers)
    need_vision = [
        n for n in numbers if mode == "vision" or (mode == "auto" and not (n in native and native[n].usable))
    ]
    vision: Dict[int, DocumentPage] = {}
    fresh_pages: set[int] = set()
    if need_vision:
        vision = {
            p.page_no: p
            for p in await pages_store.pages_for(row.id, row.sha256, need_vision, method="vision")
            if p.model == settings.PDF_OCR_MODEL and p.render_key == policy.PDF.render_key
        }
        fresh = [n for n in need_vision if n not in vision]
        budget = _budget()
        allowed = fresh[: budget.remaining]
        skipped = fresh[len(allowed) :]
        if skipped:
            not_processed.append({"reason": "budget", "pages": compress_ranges(skipped)})
        if allowed:
            budget.used += len(allowed)
            semaphore = asyncio.Semaphore(policy.PDF.vision_concurrency)
            done: List[DocumentPage] = []

            async def _one(page_no: int) -> None:
                async with semaphore:
                    executions.check_cancel()
                    await executions.progress(f"Reading page {page_no} of {row.name}…")
                    label = native[page_no].label if page_no in native else None
                    result = await executions.run_cancellable(_transcribe_page(row, data, page_no, label))
                    done.append(result)
                    # Cache as we go: a cancel or a crash keeps what was paid for.
                    await pages_store.upsert_pages([result])
                    fresh_pages.add(page_no)

            await asyncio.gather(*(_one(n) for n in allowed))
            vision.update({p.page_no: p for p in done})
    freshly_transcribed = fresh_pages

    budget_skipped = {
        int(x) for entry in not_processed if entry["reason"] == "budget" for x in _expand(entry["pages"])
    }
    pages: List[PageResult] = []
    shown: Dict[int, str] = {}
    remaining_chars = policy.PDF.result_chars_total
    for n in numbers:
        if n in budget_skipped:
            continue
        if remaining_chars <= 0:
            not_processed.append({"reason": "size", "pages": compress_ranges(range(n, served_through + 1))})
            served_through = n - 1
            break
        chosen: Optional[DocumentPage] = None
        warnings: List[str] = []
        if mode == "text":
            chosen = native.get(n)
            if chosen is not None and not chosen.usable:
                warnings.append('no usable text layer; read again with mode="vision" to transcribe the page')
        elif mode == "vision":
            chosen = vision.get(n)
        else:
            chosen = native.get(n) if n in native and native[n].usable else vision.get(n)
        if chosen is None:
            pages.append(PageResult(page_no=n, label=None, method="none", text="", warnings=["nothing could be read"]))
            continue
        text, cut = _bound(chosen.text, min(policy.PDF.result_chars_per_page, remaining_chars))
        remaining_chars -= len(text)
        page_warnings = list(chosen.warnings) + warnings
        if cut:
            page_warnings.append("text shortened to fit; the rest is on the page itself")
        pages.append(
            PageResult(
                page_no=n,
                label=chosen.label,
                method=chosen.method,
                text=text,
                warnings=page_warnings,
                cached=chosen.method == "vision" and n not in freshly_transcribed,
                model=chosen.model,
            )
        )
        shown[n] = chosen.method

    context = rich_media.current_rich_media.get()
    if context and shown:
        await pages_store.record_reads(row.id, context.session_id, shown)
    read = await pages_store.pages_read(row.id, context.session_id) if context else set(shown)

    next_page: Optional[int] = None
    pending = [int(x) for entry in not_processed for x in _expand(entry["pages"])]
    if pending:
        next_page = min(pending)
    logger.info(
        "pdf_pages_read",
        attachment_id=row.id,
        requested=f"{start}-{last}",
        served=len(pages),
        mode=mode,
        transcribed=len([p for p in pages if p.method == "vision"]),
        next_page=next_page,
        budget_remaining=_budget().remaining,
    )
    return ReadResult(
        document=row,
        pages=pages,
        requested=(start, last),
        served_through=served_through,
        next_page=next_page,
        not_processed=not_processed,
        coverage=compress_ranges(read),
        total_pages=total,
        budget_remaining=_budget().remaining,
    )


def _expand(ranges: str) -> List[int]:
    numbers: List[int] = []
    for part in ranges.split(","):
        part = part.strip()
        if not part:
            continue
        if "–" in part:
            a, b = part.split("–")
            numbers.extend(range(int(a), int(b) + 1))
        else:
            numbers.append(int(part))
    return numbers


_ARABIC_MARKS = re.compile(r"[ً-ْـٰ]")


def normalise(text: str) -> str:
    """Fold case and Arabic orthographic variants for matching.

    Args:
        text: Any text.

    Returns:
        str: Comparable form; lengths are not preserved.
    """
    text = unicodedata.normalize("NFKC", text)
    text = _ARABIC_MARKS.sub("", text)
    text = text.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا").replace("ى", "ي").replace("ة", "ه")
    return text.casefold()


def _snippet(text: str, position: int, length: int) -> str:
    half = policy.PDF.search_snippet_chars // 2
    start = max(0, position - half)
    end = min(len(text), position + length + half)
    piece = re.sub(r"\s+", " ", text[start:end]).strip()
    return ("…" if start > 0 else "") + piece + ("…" if end < len(text) else "")


async def search(document_id: str, query: str, cursor: Optional[int] = None) -> SearchResult:
    """Find pages whose available text contains the query.

    Args:
        document_id: The attachment id.
        query: Text to look for; matched case- and diacritic-insensitively.
        cursor: Page to start from, from a previous result's ``next_cursor``.

    Returns:
        SearchResult: Hits, what was searched, what could not be.

    Raises:
        DocumentUnavailable: Not this conversation's PDF, or not fetchable.
        InvalidRange: Empty query or bad cursor.
    """
    row = await _document(document_id)
    needle = normalise(query.strip())
    if not needle:
        raise InvalidRange("give a word or phrase to search for")
    data = await _bytes(row)
    total = int(row.page_count or 0)
    first = cursor or 1
    if first < 1 or (total and first > total):
        raise InvalidRange(f"cursor must be a page between 1 and {total}")
    last = (
        min(total, first + policy.PDF.search_pages_per_call - 1)
        if total
        else first + policy.PDF.search_pages_per_call - 1
    )
    numbers = list(range(first, last + 1))

    native = await _native_pages(row, data, numbers)
    vision = {
        p.page_no: p for p in await pages_store.pages_for(row.id, row.sha256, numbers, method="vision") if p.usable
    }

    hits: List[Hit] = []
    unsearched: List[int] = []
    capped = False
    stopped_at = last
    for n in numbers:
        page = vision.get(n) or (native.get(n) if n in native and native[n].usable else None)
        if page is None:
            unsearched.append(n)
            continue
        haystack = normalise(page.text)
        position = haystack.find(needle)
        if position < 0:
            continue
        # Map back approximately: search the original text case-insensitively
        # for a snippet; fall back to the normalised text when folding moved it.
        original = page.text
        lowered = original.casefold()
        raw_position = lowered.find(query.strip().casefold())
        snippet = (
            _snippet(original, raw_position, len(query))
            if raw_position >= 0
            else _snippet(haystack, position, len(needle))
        )
        hits.append(Hit(page_no=n, label=page.label, method=page.method, snippet=snippet))
        if len(hits) >= policy.PDF.search_max_hits:
            capped = True
            stopped_at = n
            break

    next_cursor = stopped_at + 1 if stopped_at < total else None
    logger.info(
        "pdf_searched", attachment_id=row.id, pages=f"{first}-{stopped_at}", hits=len(hits), unsearched=len(unsearched)
    )
    return SearchResult(
        document=row,
        query=query,
        hits=hits,
        searched=(first, stopped_at),
        unsearched=compress_ranges(n for n in unsearched if n <= stopped_at),
        next_cursor=next_cursor,
        total_pages=total,
        capped=capped,
    )


async def facts_for_intake(name: str, data: bytes) -> tuple[Dict[str, Any], List[PageText]]:
    """What intake needs from a PDF: metadata, contents, and native text per page.

    Args:
        name: File name, for errors.
        data: The file.

    Returns:
        tuple: facts dict (page_count, title…, toc, labels) and the native
        text of the first ``intake_native_pages`` pages.

    Raises:
        PdfError: encrypted or damaged.
    """

    def _work() -> tuple[Dict[str, Any], List[PageText]]:
        pdf = open_pdf(data)
        try:
            total = page_count(pdf)
            facts = {
                "page_count": total,
                **metadata(pdf),
                "toc": outline(pdf, policy.PDF.toc_entries),
                "labels": has_labels(pdf),
            }
            pages = extract_text(pdf, range(1, min(total, policy.PDF.intake_native_pages) + 1))
            return facts, pages
        finally:
            pdf.close()

    return await asyncio.to_thread(_work)


def clear_cache() -> None:
    """Drop cached document bytes (tests)."""
    _cache.clear()


__all__ = [
    "DocumentUnavailable",
    "InvalidRange",
    "PdfError",
    "ReadResult",
    "SearchResult",
    "begin_turn",
    "end_turn",
    "facts_for_intake",
    "inspect",
    "read_pages",
    "search",
]
