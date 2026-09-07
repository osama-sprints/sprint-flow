"""Cache behaviour proven against the real database.

Runs only with ``SPRINTFLOW_INTEGRATION_DB=1``. The transcriber is a counter:
a repeated request must not call it again, while a different model, different
rendering settings or a different document revision must.
"""

import hashlib
import os
import uuid
from dataclasses import replace
from types import SimpleNamespace

import pytest

from app.core.config import settings
from app.models import Attachment
from app.services import rich_media
from app.services.documents import (
    ocr,
    policy,
)
from app.services.documents import pdf as engine
from app.services.documents import service as documents
from app.services.domain import attachments as attachments_store
from app.services.domain import document_pages as pages_store
from tests.test_documents import (
    make_pdf,
    scanned_pdf,
    text_pages,
)

pytestmark = pytest.mark.skipif(
    os.getenv("SPRINTFLOW_INTEGRATION_DB") != "1", reason="needs SPRINTFLOW_INTEGRATION_DB=1 and a database"
)


async def _world(monkeypatch):
    """Set up counters, stand-ins and a conversation. Returns a namespace with ``close()``.

    Not a fixture: the suite runs coroutine tests through its own hook and has
    no async-fixture support, so each test awaits this and tears it down.
    """
    calls = {"transcribe": 0, "extract_pages": 0, "download": 0}
    files: dict[str, bytes] = {}

    async def transcribe(image, *, model, page_no):
        calls["transcribe"] += 1
        return ocr.Transcription(
            text=f"page {page_no} via {model}",
            model=model,
            prompt_tokens=1,
            completion_tokens=1,
            cost_usd=0.0,
            latency_ms=1,
        )

    async def download_file(file_id, *, max_bytes):
        calls["download"] += 1
        return files.get(file_id)

    original_extract = engine.extract_text

    def counting_extract(pdf, numbers):
        numbers = list(numbers)
        calls["extract_pages"] += len(numbers)
        return original_extract(pdf, numbers)

    monkeypatch.setattr(documents.ocr, "transcribe", transcribe)
    monkeypatch.setattr(documents.mattermost_client, "download_file", download_file)
    monkeypatch.setattr(documents, "extract_text", counting_extract)
    monkeypatch.setattr(settings, "PDF_OCR_MODEL", "model-a")
    monkeypatch.setattr(settings, "PDF_PAGE_BUDGET_PER_TURN", 100)
    documents.clear_cache()
    session = f"s-{uuid.uuid4()}"
    rich_media.begin_turn(channel_id="chan-int", session_id=session)
    documents.begin_turn()

    async def add(data: bytes, *, document_id: str | None = None):
        document_id = document_id or f"doc-{uuid.uuid4().hex[:20]}"
        files[document_id] = data
        pdf = engine.open_pdf(data)
        try:
            count = len(pdf)
        finally:
            engine.close_pdf(pdf)
        row = Attachment(
            id=document_id,
            post_id="post-int",
            channel_id="chan-int",
            session_id=session,
            name="cache.pdf",
            status="accepted",
            kind="pdf",
            sha256=hashlib.sha256(data).hexdigest(),
            page_count=count,
            metadata_={"page_count": count},
        )
        await attachments_store.save_attachments([row])
        return document_id

    def close():
        rich_media.end_turn()
        documents.end_turn()

    return SimpleNamespace(calls=calls, add=add, files=files, session=session, close=close)


async def test_repeated_page_requests_do_not_call_the_provider_again(monkeypatch):
    world = await _world(monkeypatch)
    try:
        doc = await world.add(scanned_pdf(3))
        first = await documents.read_pages(doc, 1, 2)
        assert world.calls["transcribe"] == 2 and [p.cached for p in first.pages] == [False, False]

        documents.begin_turn()  # a later turn
        again = await documents.read_pages(doc, 1, 2)
        assert world.calls["transcribe"] == 2 and [p.cached for p in again.pages] == [True, True]
        assert [p.text for p in again.pages] == ["page 1 via model-a", "page 2 via model-a"]

        # The search over those pages is also free.
        await documents.search(doc, "page 2")
        assert world.calls["transcribe"] == 2
    finally:
        world.close()


async def test_native_text_is_extracted_once_per_page(monkeypatch):
    world = await _world(monkeypatch)
    try:
        doc = await world.add(make_pdf(text_pages(6)))
        await documents.read_pages(doc, 1, 3)
        extracted = world.calls["extract_pages"]
        assert extracted == 3
        await documents.read_pages(doc, 1, 3)
        await documents.search(doc, "Section 2")
        assert world.calls["extract_pages"] == 6  # the search assessed the remaining three pages once
        await documents.search(doc, "Section 2")
        assert world.calls["extract_pages"] == 6
    finally:
        world.close()


async def test_a_different_model_or_rendering_setting_transcribes_afresh(monkeypatch):
    world = await _world(monkeypatch)
    try:
        doc = await world.add(scanned_pdf(1))
        await documents.read_pages(doc, 1, 1)
        assert world.calls["transcribe"] == 1

        monkeypatch.setattr(settings, "PDF_OCR_MODEL", "model-b")
        result = await documents.read_pages(doc, 1, 1)
        assert world.calls["transcribe"] == 2 and result.pages[0].text == "page 1 via model-b"

        monkeypatch.setattr(settings, "PDF_OCR_MODEL", "model-a")
        monkeypatch.setattr(policy, "PDF", replace(policy.PDF, render_max_edge=900))
        await documents.read_pages(doc, 1, 1)
        assert world.calls["transcribe"] == 3

        # Back to the original settings: the first transcription is still there.
        monkeypatch.setattr(policy, "PDF", replace(policy.PDF, render_max_edge=1600))
        await documents.read_pages(doc, 1, 1)
        assert world.calls["transcribe"] == 3

        rows = await pages_store.pages_for(doc, hashlib.sha256(world.files[doc]).hexdigest(), method="vision")
        assert {(r.model, r.render_key) for r in rows} == {
            ("model-a", "edge1600-q85"),
            ("model-b", "edge1600-q85"),
            ("model-a", "edge900-q85"),
        }
    finally:
        world.close()


async def test_a_new_document_revision_does_not_reuse_the_old_pages(monkeypatch):
    world = await _world(monkeypatch)
    try:
        doc = await world.add(scanned_pdf(2))
        await documents.read_pages(doc, 1, 2)
        assert world.calls["transcribe"] == 2

        # Same id, different bytes: the record's revision changes, and so must the cache key.
        revised = scanned_pdf(2) + b"\n%revised\n"
        world.files[doc] = revised
        await attachments_store.save_attachments(
            [
                Attachment(
                    id=doc,
                    post_id="post-int",
                    channel_id="chan-int",
                    session_id=world.session,
                    name="cache.pdf",
                    status="accepted",
                    kind="pdf",
                    sha256=hashlib.sha256(revised).hexdigest(),
                    page_count=2,
                    metadata_={"page_count": 2},
                )
            ]
        )
        documents.clear_cache()
        await documents.read_pages(doc, 1, 2)
        assert world.calls["transcribe"] == 4
    finally:
        world.close()
