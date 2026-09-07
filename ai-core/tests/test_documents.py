"""On-demand PDF reading: engine, service and the text the tools return.

PDFs are built in-process (a real text layer through hand-written content
streams, page labels and an outline in the catalog; a scan through Pillow).
The stores and the transcriber are stood in for at their module boundaries,
so what is pinned is the reading logic: authorisation on every call, page
addressing, lazy transcription, caching, budgets, coverage and honest search.
"""

import io
import uuid
from dataclasses import (
    dataclass,
    replace,
)
from types import SimpleNamespace
from typing import Optional

import pypdfium2 as pdfium
import pytest
from PIL import Image

from app.core.config import settings
from app.core.langgraph.tools import pdf as pdf_tools
from app.models import (
    Attachment,
    DocumentPage,
)
from app.services import rich_media
from app.services.documents import (
    ocr,
    policy,
)
from app.services.documents import service as documents
from app.services.documents.pdf import (
    PdfError,
    assess,
    compress_ranges,
    extract_text,
    has_labels,
    open_pdf,
    outline,
    render_page,
)

# ---------------------------------------------------------------------------
# PDF builders
# ---------------------------------------------------------------------------


def make_pdf(pages: list[list[str]], *, labels: bool = False, bookmarks: list[tuple[str, int]] | None = None) -> bytes:
    """A well-formed PDF with a real text layer, optional page labels and outline."""
    objects: list[str] = ["", "", "<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>"]
    page_refs: list[str] = []
    for lines in pages:
        content = "BT /F1 12 Tf 40 760 Td 14 TL " + " ".join(f"({line}) Tj T*" for line in lines) + " ET"
        number = len(objects) + 1
        objects.append(
            f"<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]/Contents {number + 1} 0 R/Resources<</Font<</F1 3 0 R>>>>>>"
        )
        objects.append(f"<</Length {len(content)}>>stream\n{content}\nendstream")
        page_refs.append(f"{number} 0 R")
    catalog = "/Type/Catalog/Pages 2 0 R"
    if labels:
        # Two roman front-matter pages, then decimal numbering restarting at 1.
        catalog += "/PageLabels<</Nums[0<</S/r>>2<</S/D/St 1>>]>>"
    if bookmarks:
        first = len(objects) + 1
        outlines_number = first + len(bookmarks)
        for index, (title, page) in enumerate(bookmarks):
            number = first + index
            prev = f"/Prev {number - 1} 0 R" if index else ""
            nxt = f"/Next {number + 1} 0 R" if index < len(bookmarks) - 1 else ""
            objects.append(
                f"<</Title({title})/Parent {outlines_number} 0 R{prev}{nxt}/Dest[{page_refs[page - 1]} /Fit]>>"
            )
        objects.append(
            f"<</Type/Outlines/First {first} 0 R/Last {first + len(bookmarks) - 1} 0 R/Count {len(bookmarks)}>>"
        )
        catalog += f"/Outlines {outlines_number} 0 R"
    objects[0] = f"<<{catalog}>>"
    objects[1] = f"<</Type/Pages/Kids[{' '.join(page_refs)}]/Count {len(page_refs)}>>"
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(out.tell())
        out.write(f"{number} 0 obj\n{body}\nendobj\n".encode("latin-1"))
    xref = out.tell()
    out.write(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode())
    for offset in offsets:
        out.write(f"{offset:010d} 00000 n \n".encode())
    out.write(f"trailer\n<</Size {len(objects) + 1}/Root 1 0 R>>\nstartxref\n{xref}\n%%EOF\n".encode())
    return out.getvalue()


def text_pages(count: int) -> list[list[str]]:
    pages = []
    for n in range(1, count + 1):
        lines = [f"Section {n}. This page carries ordinary handbook text for page {n} of the document."]
        if n == 7:
            lines.append("Cancellation terms: notice of 30 days in writing; the current month is not refunded.")
        if n == 23:
            lines.append("The escalation code word for October is SAFFRON-7.")
        pages.append(lines)
    return pages


def scanned_pdf(pages: int = 3) -> bytes:
    images = [Image.new("RGB", (400, 560), (250, 250, 250)) for _ in range(pages)]
    buffer = io.BytesIO()
    images[0].save(buffer, format="PDF", save_all=True, append_images=images[1:])
    return buffer.getvalue()


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


def test_text_layer_is_read_per_page_with_labels_and_outline():
    data = make_pdf(text_pages(4), labels=True, bookmarks=[("Introduction", 1), ("Terms", 3)])
    pdf = open_pdf(data)
    try:
        assert has_labels(pdf)
        pages = extract_text(pdf, [1, 2, 3, 7])
        assert [p.page_no for p in pages] == [1, 2, 3]  # page 7 does not exist: skipped, not invented
        # Front matter prints "i", "ii"; numbering then restarts, so physical page 3 prints "1".
        assert [p.label for p in pages] == ["i", "ii", "1"]
        assert all(p.usable for p in pages) and "Section 2." in pages[1].text
        toc = outline(pdf, 10)
        assert toc == [{"title": "Introduction", "page": 1, "level": 0}, {"title": "Terms", "page": 3, "level": 0}]
        jpeg = render_page(pdf, 1)
        image = Image.open(io.BytesIO(jpeg))
        assert image.format == "JPEG" and max(image.size) <= policy.PDF.render_max_edge
        with pytest.raises(IndexError):
            render_page(pdf, 9)
    finally:
        pdf.close()


def test_unusable_text_layers_are_recognised():
    assert assess("short")[1] is False
    assert assess("x" * 100)[1] is True
    # PDFium hands back some producers' Arabic in visual order: the words read
    # backwards. Such a layer is unusable and must be transcribed instead.
    forward = "تم الاتفاق بين المؤجر والمستأجر على تأجير الشقة في حي النرجس من أول أكتوبر إلى نهاية سبتمبر"
    reversed_layer = " ".join(word[::-1] for word in forward.split())
    assert assess(forward + " " + forward)[1] is True
    chars, usable, warnings = assess(reversed_layer + " " + reversed_layer)
    assert usable is False and "visual order" in warnings[0]
    broken = "(cid:12)(cid:13)(cid:14) " * 20
    chars, usable, warnings = assess(broken)
    assert usable is False and "broken font encoding" in warnings[0]
    scan = scanned_pdf(1)
    pdf = open_pdf(scan)
    try:
        page = extract_text(pdf, [1])[0]
        assert page.usable is False and page.chars == 0
    finally:
        pdf.close()


def test_damaged_and_encrypted_files_are_told_apart(monkeypatch):
    with pytest.raises(PdfError) as damaged:
        open_pdf(b"%PDF-1.4 not really a pdf")
    assert damaged.value.kind == "damaged"

    def needs_password(*args, **kwargs):
        raise pdfium.PdfiumError("Failed to load document (PDFium: Incorrect password error).")

    monkeypatch.setattr(pdfium, "PdfDocument", needs_password)
    with pytest.raises(PdfError) as encrypted:
        open_pdf(b"%PDF-1.4")
    assert encrypted.value.kind == "encrypted"


def test_page_ranges_are_described_compactly():
    assert compress_ranges([]) == ""
    assert compress_ranges([3, 1, 2, 7, 12, 13, 14]) == "1–3, 7, 12–14"


# ---------------------------------------------------------------------------
# Service stand-ins
# ---------------------------------------------------------------------------


class FakePages:
    def __init__(self) -> None:
        self.rows: list[DocumentPage] = []
        self.reads: dict[tuple[str, str], dict[int, str]] = {}

    def _key(self, r):
        return (r.attachment_id, r.sha256, r.page_no, r.method, r.model, r.render_key)

    async def upsert_pages(self, rows, *, session=None):
        for row in rows:
            existing = next((r for r in self.rows if self._key(r) == self._key(row)), None)
            if existing is not None:
                self.rows.remove(existing)
            if not row.id:
                row.id = str(uuid.uuid4())
            self.rows.append(row)
        return len(rows)

    async def pages_for(self, attachment_id, sha256, page_numbers=None, *, method=None, session=None):
        wanted = set(page_numbers) if page_numbers is not None else None
        rows = [
            r
            for r in self.rows
            if r.attachment_id == attachment_id
            and r.sha256 == sha256
            and (wanted is None or r.page_no in wanted)
            and (method is None or r.method == method)
        ]
        return sorted(rows, key=lambda r: (r.page_no, r.method))

    async def record_reads(self, attachment_id, session_id, pages, *, session=None):
        self.reads.setdefault((attachment_id, session_id), {}).update(pages)

    async def pages_read(self, attachment_id, session_id, *, session=None):
        return set(self.reads.get((attachment_id, session_id), {}))


@dataclass
class FakeOcr:
    calls: list = None  # type: ignore[assignment]
    text_for: Optional[dict] = None

    def __post_init__(self):
        self.calls = []

    async def transcribe(self, image, *, model, page_no):
        self.calls.append((page_no, model))
        text = (self.text_for or {}).get(page_no, f"Transcribed page {page_no}: amount 1,234 SAR")
        return ocr.Transcription(
            text=text, model=model, prompt_tokens=900, completion_tokens=40, cost_usd=0.0003, latency_ms=800
        )


@pytest.fixture
def world(monkeypatch):
    pages = FakePages()
    fake_ocr = FakeOcr()
    docs: dict[str, tuple[Attachment, bytes]] = {}

    async def get_for_conversation(attachment_id, session_id, channel_id, *, session=None):
        entry = docs.get(attachment_id)
        if entry is None:
            return None
        row = entry[0]
        return row if (row.session_id, row.channel_id) == (session_id, channel_id) else None

    async def download_file(file_id, *, max_bytes):
        entry = docs.get(file_id)
        return entry[1] if entry else None

    monkeypatch.setattr(documents, "pages_store", pages)
    monkeypatch.setattr(documents.attachments_store, "get_for_conversation", get_for_conversation)
    monkeypatch.setattr(documents.mattermost_client, "download_file", download_file)
    monkeypatch.setattr(documents.ocr, "transcribe", fake_ocr.transcribe)
    monkeypatch.setattr(settings, "PDF_OCR_MODEL", "gemini/test-lite")
    monkeypatch.setattr(settings, "PDF_PAGE_BUDGET_PER_TURN", 60)
    documents.clear_cache()
    documents.begin_turn()

    def add(document_id: str, data: bytes, *, session_id="s1", channel_id="c1", kind="pdf", name="doc.pdf"):
        import hashlib

        pdf = open_pdf(data)
        try:
            count = len(pdf)
        finally:
            pdf.close()
        row = Attachment(
            id=document_id,
            channel_id=channel_id,
            session_id=session_id,
            name=name,
            status="accepted",
            kind=kind,
            sha256=hashlib.sha256(data).hexdigest(),
            page_count=count,
            metadata_={"page_count": count, "toc": [], "labels": False},
        )
        docs[document_id] = (row, data)
        return row

    def bind(session_id="s1", channel_id="c1"):
        rich_media.begin_turn(channel_id=channel_id, session_id=session_id)

    bind()
    yield SimpleNamespace(pages=pages, ocr=fake_ocr, add=add, bind=bind)
    rich_media.end_turn()
    documents.end_turn()


# ---------------------------------------------------------------------------
# Authorisation and addressing
# ---------------------------------------------------------------------------


async def test_documents_are_only_readable_from_their_own_conversation(world):
    world.add("d1", make_pdf(text_pages(3)))
    assert (await documents.read_pages("d1", 1, 1)).pages[0].page_no == 1

    world.bind(session_id="someone-else")
    with pytest.raises(documents.DocumentUnavailable):
        await documents.read_pages("d1", 1, 1)
    with pytest.raises(documents.DocumentUnavailable):
        await documents.inspect("d1")
    with pytest.raises(documents.DocumentUnavailable):
        await documents.search("d1", "cancellation")

    world.bind()
    world.add("img", b"", kind="image") if False else None
    rich_media.end_turn()
    with pytest.raises(documents.DocumentUnavailable):
        await documents.inspect("d1")


async def test_invalid_ranges_and_modes_are_refused_with_the_document_length(world):
    world.add("d1", make_pdf(text_pages(5)))
    for start, end in ((0, 1), (6, 6), (4, 2)):
        with pytest.raises(documents.InvalidRange, match="pages run 1–5"):
            await documents.read_pages("d1", start, end)
    with pytest.raises(documents.InvalidRange, match="mode must be one of"):
        await documents.read_pages("d1", 1, 1, "ocr")
    # An end past the document is clamped, not refused.
    result = await documents.read_pages("d1", 4, 99)
    assert [p.page_no for p in result.pages] == [4, 5] and result.next_page is None


# ---------------------------------------------------------------------------
# Lazy reading, caching, budgets, continuation
# ---------------------------------------------------------------------------


async def test_auto_mode_uses_native_text_and_transcribes_only_pages_without_it(world):
    world.add("d1", make_pdf(text_pages(2)))
    world.add("scan", scanned_pdf(2), name="scan.pdf")

    text = await documents.read_pages("d1", 1, 2)
    assert [p.method for p in text.pages] == ["native", "native"] and world.ocr.calls == []
    assert "Section 2." in text.pages[1].text

    scan = await documents.read_pages("scan", 1, 2)
    assert [p.method for p in scan.pages] == ["vision", "vision"]
    # Pages transcribe concurrently, so only the set of calls is stable.
    assert sorted(c[0] for c in world.ocr.calls) == [1, 2] and scan.pages[0].model == "gemini/test-lite"
    assert scan.pages[0].cached is False and "1,234 SAR" in scan.pages[0].text
    assert scan.budget_remaining == 58

    # The same pages again: served from the cache, nothing transcribed twice.
    again = await documents.read_pages("scan", 1, 2)
    assert len(world.ocr.calls) == 2 and all(p.cached for p in again.pages)

    # A different model means a fresh transcription, keyed separately.
    stored = [r for r in world.pages.rows if r.method == "vision"]
    assert {(r.page_no, r.model, r.render_key) for r in stored} == {
        (1, "gemini/test-lite", policy.PDF.render_key),
        (2, "gemini/test-lite", policy.PDF.render_key),
    }
    assert stored[0].usage["cost_usd"] == 0.0003 and stored[0].latency_ms == 800


async def test_text_mode_never_transcribes_and_vision_mode_always_does(world):
    world.add("scan", scanned_pdf(1))
    world.add("d1", make_pdf(text_pages(1)))

    weak = await documents.read_pages("scan", 1, 1, "text")
    assert weak.pages[0].method == "native" and weak.pages[0].text == "" and world.ocr.calls == []
    assert any('mode="vision"' in w for w in weak.pages[0].warnings)

    forced = await documents.read_pages("d1", 1, 1, "vision")
    assert forced.pages[0].method == "vision" and world.ocr.calls == [(1, "gemini/test-lite")]


async def test_large_ranges_come_in_batches_with_a_continuation(world, monkeypatch):
    monkeypatch.setattr(policy, "PDF", replace(policy.PDF, max_pages_per_read=4))
    world.add("d1", make_pdf(text_pages(10)))
    first = await documents.read_pages("d1", 1, 10)
    assert [p.page_no for p in first.pages] == [1, 2, 3, 4] and first.next_page == 5
    assert first.not_processed == [{"reason": "range", "pages": "5–10"}]
    assert first.coverage == "1–4"
    second = await documents.read_pages("d1", first.next_page, 10)
    assert [p.page_no for p in second.pages] == [5, 6, 7, 8] and second.coverage == "1–8"


async def test_transcription_budget_is_enforced_per_turn_and_reported(world, monkeypatch):
    monkeypatch.setattr(settings, "PDF_PAGE_BUDGET_PER_TURN", 2)
    world.add("scan", scanned_pdf(3))
    result = await documents.read_pages("scan", 1, 3)
    # A budget-skipped page is reported, not offered as a continuation: the budget
    # will not have changed within this turn.
    assert [p.page_no for p in result.pages] == [1, 2] and result.next_page is None
    assert result.not_processed == [{"reason": "budget", "pages": "3"}] and result.budget_remaining == 0
    assert len(world.ocr.calls) == 2

    # Cached pages stay free after the budget is spent; the new page is not.
    documents.begin_turn()
    monkeypatch.setattr(settings, "PDF_PAGE_BUDGET_PER_TURN", 0)
    result = await documents.read_pages("scan", 1, 3)
    assert [p.page_no for p in result.pages] == [1, 2] and all(p.cached for p in result.pages)
    assert result.not_processed == [{"reason": "budget", "pages": "3"}]


async def test_results_are_size_bounded_and_say_where_to_continue(world, monkeypatch):
    monkeypatch.setattr(policy, "PDF", replace(policy.PDF, result_chars_total=150, result_chars_per_page=120))
    world.add("d1", make_pdf(text_pages(4)))
    result = await documents.read_pages("d1", 1, 4)
    assert len(result.pages) < 4 and result.next_page is not None
    assert any(e["reason"] == "size" for e in result.not_processed)
    assert all(len(p.text) <= 122 for p in result.pages)


async def test_transcription_failure_is_a_page_warning_not_a_crash(world, monkeypatch):
    async def broken(image, *, model, page_no):
        raise ocr.OcrFailed("model returned nothing")

    monkeypatch.setattr(documents.ocr, "transcribe", broken)
    world.add("scan", scanned_pdf(1))
    result = await documents.read_pages("scan", 1, 1)
    page = result.pages[0]
    assert page.method == "none" or (page.method == "vision" and page.text == "")
    assert any("could not be transcribed" in w or "nothing could be read" in w for w in page.warnings)


# ---------------------------------------------------------------------------
# Search: honest coverage
# ---------------------------------------------------------------------------


async def test_search_reports_hits_by_page_and_never_claims_unsearched_pages_are_empty(world):
    world.add("d1", make_pdf(text_pages(30)))
    result = await documents.search("d1", "cancellation TERMS")
    assert [h.page_no for h in result.hits] == [7] and "Cancellation terms" in result.hits[0].snippet
    assert result.searched == (1, 30) and result.unsearched == "" and result.next_cursor is None

    world.add("mixed", scanned_pdf(2))
    result = await documents.search("mixed", "anything")
    assert result.hits == [] and result.unsearched == "1–2"
    text = pdf_tools._search_text(result)
    assert "NOT searched: pages 1–2 have no readable text yet" in text and 'mode="vision"' in text

    # Once a page is transcribed, the search covers it.
    world.ocr.text_for = {1: "Page one mentions the cancellation clause explicitly."}
    await documents.read_pages("mixed", 1, 1)
    result = await documents.search("mixed", "cancellation")
    assert [(h.page_no, h.method) for h in result.hits] == [(1, "vision")] and result.unsearched == "2"


async def test_search_normalises_arabic_and_scans_in_windows(world, monkeypatch):
    monkeypatch.setattr(policy, "PDF", replace(policy.PDF, search_pages_per_call=10))
    row = world.add("d1", make_pdf(text_pages(25)))
    # Arabic lives in the stored text layer; the fixture font cannot draw it.
    world.pages.rows.append(
        DocumentPage(
            id="x",
            attachment_id="d1",
            sha256=row.sha256,
            page_no=23,
            method="native",
            text="شُروط الإلغاء تُطبَّق بعد ثلاثين يوماً",
            chars=30,
            usable=True,
        )
    )
    first = await documents.search("d1", "شروط الالغاء")
    assert first.hits == [] and first.searched == (1, 10) and first.next_cursor == 11
    third = await documents.search("d1", "شروط الالغاء", cursor=21)
    assert [h.page_no for h in third.hits] == [23] and third.searched == (21, 25) and third.next_cursor is None

    with pytest.raises(documents.InvalidRange):
        await documents.search("d1", "   ")
    with pytest.raises(documents.InvalidRange):
        await documents.search("d1", "x", cursor=99)


async def test_hit_cap_stops_early_and_hands_back_the_page_it_stopped_on(world, monkeypatch):
    monkeypatch.setattr(policy, "PDF", replace(policy.PDF, search_max_hits=3))
    world.add("d1", make_pdf(text_pages(8)))
    result = await documents.search("d1", "handbook text")
    assert [h.page_no for h in result.hits] == [1, 2, 3] and result.capped and result.next_cursor == 4


# ---------------------------------------------------------------------------
# Inspect and coverage across turns
# ---------------------------------------------------------------------------


async def test_inspect_describes_structure_coverage_and_reads(world):
    world.add("d1", make_pdf(text_pages(6), bookmarks=[("Terms", 3)]))
    docs_row = await documents.inspect("d1")
    assert (docs_row["pages"], docs_row["read_count"], docs_row["next_unread_page"]) == (6, 0, 1)
    await documents.read_pages("d1", 1, 2)
    facts = await documents.inspect("d1")
    assert facts["read_pages"] == "1–2" and facts["next_unread_page"] == 3
    assert facts["text_page_count"] == 6 and facts["no_text_page_count"] == 0
    text = pdf_tools._inspect_text(facts)
    assert "Read in this conversation so far: pages 1–2 (2 of 6)" in text and "First unread page: 3" in text


async def test_tool_text_carries_provenance_and_continuation(world, monkeypatch):
    monkeypatch.setattr(policy, "PDF", replace(policy.PDF, max_pages_per_read=2))
    world.add("d1", make_pdf(text_pages(5), labels=True))
    result = await documents.read_pages("d1", 1, 5)
    text = pdf_tools._read_text(result)
    assert text.startswith("doc.pdf (id d1) — pages 1–5 of 5; served 2 page(s): 2 native text.")
    assert 'Continue with read_pdf_pages("d1", 3, 5).' in text
    assert '--- page 1 (label "i") · native text ---' in text and "Section 1." in text
    assert "Read in this conversation so far: 1–2 of 5 pages." in text
