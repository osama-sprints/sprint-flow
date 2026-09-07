"""Question-driven visual inspection and progressive search over scans.

Builders are shared with ``test_documents``; the stores, the transcriber and
the visual model are stood in for at their module boundaries.
"""

from dataclasses import replace
from types import SimpleNamespace

import pytest

from app.core.config import settings
from app.core.langgraph.tools import pdf as pdf_tools
from app.models import Attachment
from app.services import rich_media
from app.services.documents import (
    ocr,
    policy,
    vision,
)
from app.services.documents import service as documents
from app.services.documents.pdf import open_pdf
from tests.test_documents import (
    FakePages,
    make_pdf,
    scanned_pdf,
    text_pages,
)

# ---------------------------------------------------------------------------
# Stand-ins (the visual model added)
# ---------------------------------------------------------------------------


class FakeVision:
    def __init__(self) -> None:
        self.calls: list = []

    async def answer_about_pages(self, images, *, question, model, document_name):
        self.calls.append(([p for p, _ in images], question, model, document_name))
        return vision.VisualAnswer(
            text=f"On (p. {images[0][0]}) the diagram shows three boxes.",
            model=model,
            pages=[p for p, _ in images],
            prompt_tokens=1500,
            completion_tokens=60,
            cost_usd=0.001,
            latency_ms=1200,
        )


class FakeOcr:
    def __init__(self) -> None:
        self.calls: list = []
        self.text_for: dict = {}

    async def transcribe(self, image, *, model, page_no):
        self.calls.append(page_no)
        text = self.text_for.get(page_no, f"Transcribed page {page_no}: nothing special here.")
        return ocr.Transcription(
            text=text, model=model, prompt_tokens=900, completion_tokens=40, cost_usd=0.0003, latency_ms=800
        )


@pytest.fixture
def world(monkeypatch):
    pages = FakePages()
    fake_ocr = FakeOcr()
    fake_vision = FakeVision()
    docs: dict = {}

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
    monkeypatch.setattr(documents.vision, "answer_about_pages", fake_vision.answer_about_pages)
    monkeypatch.setattr(settings, "PDF_OCR_MODEL", "gemini/test-lite")
    monkeypatch.setattr(settings, "FILE_INPUT_VISION_MODEL", "gemini/test-vision")
    monkeypatch.setattr(settings, "PDF_PAGE_BUDGET_PER_TURN", 60)
    documents.clear_cache()
    documents.begin_turn()

    def add(document_id: str, data: bytes, *, session_id="s1", channel_id="c1", name="doc.pdf"):
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
            kind="pdf",
            sha256=hashlib.sha256(data).hexdigest(),
            page_count=count,
            metadata_={"page_count": count, "toc": [], "labels": False},
        )
        docs[document_id] = (row, data)
        return row

    rich_media.begin_turn(channel_id="c1", session_id="s1")
    yield SimpleNamespace(pages=pages, ocr=fake_ocr, vision=fake_vision, add=add)
    rich_media.end_turn()
    documents.end_turn()


# ---------------------------------------------------------------------------
# 1. Question-driven visual inspection, distinct from transcription
# ---------------------------------------------------------------------------


async def test_ask_pages_shows_the_pages_to_the_vision_model_with_provenance(world):
    world.add("d1", make_pdf(text_pages(6)), name="topology.pdf")
    result = await documents.ask_pages("d1", "Which component talks to the database?", 3, 4)

    assert result.pages == [3, 4] and result.model == "gemini/test-vision"
    assert world.vision.calls == [
        ([3, 4], "Which component talks to the database?", "gemini/test-vision", "topology.pdf")
    ]
    assert world.ocr.calls == []  # a question is not a transcription
    assert result.budget_remaining == 58 and result.coverage == "3–4"
    text = pdf_tools._ask_text(result)
    assert "visual reading of page(s) 3, 4 of 6 by gemini/test-vision" in text
    assert "not a transcription" in text and "(p. 3)" in text


async def test_ask_pages_refuses_wide_ranges_bad_ranges_and_empty_questions(world):
    world.add("d1", make_pdf(text_pages(8)))
    with pytest.raises(documents.InvalidRange, match="at most 4 pages"):
        await documents.ask_pages("d1", "what is this?", 1, 5)
    with pytest.raises(documents.InvalidRange, match="pages run 1–8"):
        await documents.ask_pages("d1", "what is this?", 9)
    with pytest.raises(documents.InvalidRange, match="give a question"):
        await documents.ask_pages("d1", "   ", 1)
    assert world.vision.calls == []


async def test_ask_pages_counts_against_the_turn_budget(world, monkeypatch):
    monkeypatch.setattr(settings, "PDF_PAGE_BUDGET_PER_TURN", 3)
    world.add("d1", make_pdf(text_pages(8)))
    await documents.ask_pages("d1", "q", 1, 2)
    with pytest.raises(documents.BudgetExhausted) as exhausted:
        await documents.ask_pages("d1", "q", 3, 4)
    assert exhausted.value.pages == [3, 4]  # all or nothing: no page was looked at
    assert len(world.vision.calls) == 1


async def test_ask_pages_is_scoped_to_the_conversation(world):
    world.add("theirs", make_pdf(text_pages(2)), session_id="other")
    with pytest.raises(documents.DocumentUnavailable):
        await documents.ask_pages("theirs", "q", 1)


# ---------------------------------------------------------------------------
# 2. Progressive search over a scanned document
# ---------------------------------------------------------------------------


async def test_progressive_search_transcribes_missing_pages_in_order_within_budget(world, monkeypatch):
    monkeypatch.setattr(policy, "PDF", replace(policy.PDF, search_transcribe_pages_per_call=2))
    world.add("scan", scanned_pdf(5), name="scan.pdf")
    world.ocr.text_for = {3: "Page three: the cancellation terms apply after thirty days.", 4: "Page four: unrelated."}

    plain = await documents.search("scan", "cancellation")
    assert plain.hits == [] and plain.unsearched == "1–5" and plain.transcribed_now == "" and world.ocr.calls == []
    assert "NOT searched: pages 1–5 have no readable text yet" in pdf_tools._search_text(plain)

    first = await documents.search("scan", "cancellation", transcribe_missing=True)
    assert first.transcribed_now == "1–2" and first.unsearched == "3–5" and first.hits == []
    assert world.ocr.calls == [1, 2] or sorted(world.ocr.calls) == [1, 2]
    text = pdf_tools._search_text(first)
    assert "Transcribed in this call so they could be searched: pages 1–2." in text
    assert "NOT searched: pages 3–5" in text and "transcribe_missing=true" in text

    second = await documents.search("scan", "cancellation", transcribe_missing=True)
    assert second.transcribed_now == "3–4" and [h.page_no for h in second.hits] == [3]
    assert second.unsearched == "5" and "thirty days" in second.hits[0].snippet
    assert sorted(world.ocr.calls) == [1, 2, 3, 4]

    # Everything already transcribed is free; only page 5 is new.
    third = await documents.search("scan", "cancellation", transcribe_missing=True)
    assert third.transcribed_now == "5" and third.unsearched == "" and sorted(world.ocr.calls) == [1, 2, 3, 4, 5]
    assert third.next_cursor is None and "Every page has been searched." in pdf_tools._search_text(third)


async def test_progressive_search_reports_pages_the_budget_could_not_reach(world, monkeypatch):
    monkeypatch.setattr(settings, "PDF_PAGE_BUDGET_PER_TURN", 2)
    world.add("scan", scanned_pdf(4))
    result = await documents.search("scan", "anything", transcribe_missing=True)
    assert result.transcribed_now == "1–2" and result.budget_skipped == "3–4" and result.unsearched == "3–4"
    assert result.budget_remaining == 0
    text = pdf_tools._search_text(result)
    assert "left untranscribed because this turn's transcription budget is used up" in text
    assert "3–4" in text


async def test_failed_transcriptions_are_shown_but_never_cached(world, monkeypatch):
    attempts = {"n": 0}

    async def flaky(image, *, model, page_no):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise ocr.OcrFailed("proxy 503")
        return ocr.Transcription(
            text=f"page {page_no} recovered",
            model=model,
            prompt_tokens=1,
            completion_tokens=1,
            cost_usd=0.0,
            latency_ms=5,
        )

    monkeypatch.setattr(documents.ocr, "transcribe", flaky)
    world.add("scan", scanned_pdf(1))
    first = await documents.read_pages("scan", 1, 1)
    assert first.pages[0].text == "" and "could not be transcribed" in first.pages[0].warnings[0]
    assert [r for r in world.pages.rows if r.method == "vision"] == []  # the outage was not written down

    documents.begin_turn()
    second = await documents.read_pages("scan", 1, 1)
    assert second.pages[0].text == "page 1 recovered" and attempts["n"] == 2


async def test_progressive_search_does_not_stall_on_pages_already_found_empty(world, monkeypatch):
    monkeypatch.setattr(policy, "PDF", replace(policy.PDF, search_transcribe_pages_per_call=2))
    world.add("scan", scanned_pdf(4))
    world.ocr.text_for = {1: "[empty page]", 2: "[empty page]", 3: "the cancellation clause", 4: "[empty page]"}
    first = await documents.search("scan", "cancellation", transcribe_missing=True)
    assert first.transcribed_now == "1–2" and first.hits == [] and first.empty_pages == "1–2"
    second = await documents.search("scan", "cancellation", transcribe_missing=True)
    assert second.transcribed_now == "3–4" and [h.page_no for h in second.hits] == [3]
    assert sorted(world.ocr.calls) == [1, 2, 3, 4]  # the blanks were not transcribed again
    third = await documents.search("scan", "cancellation", transcribe_missing=True)
    assert third.transcribed_now == "" and third.empty_pages == "1–2, 4" and third.unsearched == ""


async def test_budget_skipped_pages_are_not_offered_as_a_continuation(world, monkeypatch):
    monkeypatch.setattr(settings, "PDF_PAGE_BUDGET_PER_TURN", 0)
    world.add("scan", scanned_pdf(2))
    result = await documents.read_pages("scan", 1, 2)
    assert result.next_page is None and result.not_processed == [{"reason": "budget", "pages": "1–2"}]
    text = pdf_tools._read_text(result)
    assert "Continue with read_pdf_pages" not in text and "Do not call again for the skipped pages" in text


async def test_a_dense_page_can_be_read_on_from_the_cut(world, monkeypatch):
    monkeypatch.setattr(policy, "PDF", replace(policy.PDF, result_chars_per_page=200, result_chars_total=1000))
    world.add("d1", make_pdf([[f"Line {i} of a very dense page with plenty of words to read." for i in range(40)]]))
    first = await documents.read_pages("d1", 1, 1)
    assert first.pages[0].text.endswith("…") and any("char_offset=" in w for w in first.pages[0].warnings)
    hint = next(w for w in first.pages[0].warnings if "char_offset=" in w)
    offset = int(hint.split("char_offset=")[1].split(")")[0])
    second = await documents.read_pages("d1", 1, 1, char_offset=offset)
    assert second.pages[0].text and not second.pages[0].text.startswith(first.pages[0].text[:20])
    assert any("continuing from character" in w for w in second.pages[0].warnings)


async def test_arabic_hits_are_quoted_as_printed(world):
    from app.models import DocumentPage

    row = world.add("d1", make_pdf(text_pages(2)))
    world.pages.rows.append(
        DocumentPage(
            id="ar",
            attachment_id="d1",
            sha256=row.sha256,
            page_no=2,
            method="native",
            text="يجب على الموظف تقديم طلب الإجازة قبل أسبوع من الموعد",
            chars=40,
            usable=True,
        )
    )
    result = await documents.search("d1", "الاجازة")
    assert [h.page_no for h in result.hits] == [2]
    assert "الإجازة" in result.hits[0].snippet and "الاجازه" not in result.hits[0].snippet
