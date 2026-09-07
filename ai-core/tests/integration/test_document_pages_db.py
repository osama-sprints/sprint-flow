"""Per-page cache and read coverage against the real database.

Runs only with ``SPRINTFLOW_INTEGRATION_DB=1``. Pins the cache key, the read
ledger, and that retention's cascade removes a document's pages with it.
"""

import os
import uuid
from datetime import timedelta

import pytest

from app.models import (
    Attachment,
    DocumentPage,
    utcnow,
)
from app.services.domain import attachments as attachments_store
from app.services.domain import document_pages as store

pytestmark = pytest.mark.skipif(
    os.getenv("SPRINTFLOW_INTEGRATION_DB") != "1", reason="needs SPRINTFLOW_INTEGRATION_DB=1 and a database"
)


async def _document(expires_in_minutes: int = 60) -> Attachment:
    row = Attachment(
        id=f"doc-{uuid.uuid4().hex[:20]}",
        post_id="post-int",
        channel_id="chan-int",
        session_id=f"s-{uuid.uuid4()}",
        name="h.pdf",
        status="accepted",
        kind="pdf",
        sha256="a" * 64,
        page_count=3,
        expires_at=utcnow() + timedelta(minutes=expires_in_minutes),
    )
    await attachments_store.save_attachments([row])
    return row


def _page(doc: Attachment, page_no: int, method: str = "native", model: str = "", text: str = "hello") -> DocumentPage:
    return DocumentPage(
        attachment_id=doc.id,
        sha256=doc.sha256,
        page_no=page_no,
        method=method,
        model=model,
        render_key="edge1600-q85" if method == "vision" else "",
        text=text,
        chars=len(text),
        usable=True,
    )


async def test_pages_are_keyed_by_revision_page_method_model_and_settings():
    doc = await _document()
    await store.upsert_pages([_page(doc, 1), _page(doc, 2), _page(doc, 1, "vision", "m1", "seen")])
    await store.upsert_pages([_page(doc, 1, text="hello again")])  # replaces, does not duplicate

    rows = await store.pages_for(doc.id, doc.sha256)
    assert [(r.page_no, r.method, r.text) for r in rows] == [
        (1, "native", "hello again"),
        (1, "vision", "seen"),
        (2, "native", "hello"),
    ]
    assert [r.page_no for r in await store.pages_for(doc.id, doc.sha256, [1], method="vision")] == [1]
    assert await store.pages_for(doc.id, "b" * 64) == []  # another revision: nothing


async def test_reads_are_recorded_per_conversation():
    doc = await _document()
    await store.record_reads(doc.id, "conv-a", {1: "native", 2: "vision"})
    await store.record_reads(doc.id, "conv-a", {2: "native"})
    assert await store.pages_read(doc.id, "conv-a") == {1, 2}
    assert await store.pages_read(doc.id, "conv-b") == set()


async def test_retention_removes_a_documents_pages_with_it():
    doc = await _document(expires_in_minutes=-1)
    await store.upsert_pages([_page(doc, 1)])
    await store.record_reads(doc.id, "conv", {1: "native"})
    deleted = await attachments_store.purge_expired()
    assert deleted >= 1
    assert await store.pages_for(doc.id, doc.sha256) == [] and await store.pages_read(doc.id, "conv") == set()
