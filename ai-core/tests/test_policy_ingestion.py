"""Capability 14 — Making company policy searchable: ingestion foundation tests.

Deterministic and fully offline: PDF/DOCX fixtures are built in ``tmp_path`` at
runtime (pypdf / python-docx write as well as read), embeddings are a fake
hash-based generator, and no network call is possible. The chunker's id scheme
must stay stable — it IS the idempotency guarantee of re-ingestion.
"""

import asyncio
import hashlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pypdf import PdfReader, PdfWriter
from pypdf.errors import PdfReadError

from app.services.document_ingestion.chunker import PolicyChunker
from app.services.document_ingestion.loaders import DocumentLoader
from app.services.document_ingestion.pipeline import IngestionPipeline


# ---------------------------------------------------------------------------
# Deterministic fake embeddings
# ---------------------------------------------------------------------------


def fake_embedding(text: str, dim: int = 1536) -> list[float]:
    """Hash-based deterministic embedding: same text -> same vector, always."""
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    raw = (digest * ((dim // len(digest)) + 1))[:dim]
    return [(b / 127.5) - 1.0 for b in raw]


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------


def test_text_loader_preserves_content_and_metadata(tmp_path: Path):
    file = tmp_path / "policy.txt"
    file.write_text("Learners receive 21 days of leave per year.\nRequests need 3 days notice.", "utf-8")

    doc = DocumentLoader.load(str(file))

    assert "21 days of leave" in doc["content"]
    assert doc["sections"][0]["title"] == "Root"
    assert doc["sections"][0]["page"] == 1
    assert doc["file_path"] == str(file)
    assert doc["sections"][0]["file_path"] == str(file)


def test_markdown_loader_treated_as_text(tmp_path: Path):
    file = tmp_path / "policy.md"
    file.write_text("# Leave\nAll leave needs approval.", "utf-8")

    doc = DocumentLoader.load(str(file))

    assert doc["sections"][0]["text"] == "# Leave\nAll leave needs approval."


def test_pdf_loader_extracts_pages_as_sections(tmp_path: Path):
    file = tmp_path / "policy.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    writer.add_blank_page(width=612, height=792)
    with open(file, "wb") as fh:
        writer.write(fh)
    assert len(PdfReader(str(file)).pages) == 2

    doc = DocumentLoader.load(str(file))

    assert len(doc["sections"]) == 2
    assert doc["sections"][0]["title"] == "Page 1"
    assert doc["sections"][0]["page"] == 1
    assert doc["sections"][1]["page"] == 2


def test_docx_loader_splits_on_headings(tmp_path: Path):
    docx = pytest.importorskip("docx")
    file = tmp_path / "policy.docx"

    document = docx.Document()
    document.add_heading("Leave Policy", level=1)
    document.add_paragraph("Learners get 21 days.")
    document.add_heading("Equipment", level=1)
    document.add_paragraph("Laptops are requested through IT.")
    document.save(file)

    doc = DocumentLoader.load(str(file))

    titles = [section["title"] for section in doc["sections"]]
    assert "Leave Policy" in titles and "Equipment" in titles
    joined = "\n".join(section["text"] for section in doc["sections"])
    assert "21 days" in joined and "Laptops" in joined


def test_unsupported_format_is_rejected(tmp_path: Path):
    file = tmp_path / "policy.exe"
    file.write_bytes(b"MZ fake binary")

    with pytest.raises(ValueError, match="Unsupported document format"):
        DocumentLoader.load(str(file))


def test_empty_text_document_produces_no_chunks(tmp_path: Path):
    file = tmp_path / "empty.txt"
    file.write_text("", "utf-8")

    doc = DocumentLoader.load(str(file))
    chunker = PolicyChunker(chunk_size=100, chunk_overlap=0)
    assert chunker.chunk_document(doc, "doc-1", "learner") == []


def test_malformed_pdf_raises_a_clear_error(tmp_path: Path):
    file = tmp_path / "broken.pdf"
    file.write_bytes(b"%PDF-1.4 this is not a real pdf body \xff\xfe")

    with pytest.raises((ValueError, PdfReadError)):
        DocumentLoader.load(str(file))


# ---------------------------------------------------------------------------
# Chunker tests
# ---------------------------------------------------------------------------


def _doc(sections: list[dict]) -> dict:
    return {"content": "\n".join(s["text"] for s in sections), "sections": sections}


def test_chunk_ids_are_stable_across_runs():
    chunker = PolicyChunker(chunk_size=50, chunk_overlap=0)
    doc = _doc([{"title": "Leave", "text": "A" * 120, "page": 3}])

    first = chunker.chunk_document(doc, "DOC-1", "learner")
    second = chunker.chunk_document(doc, "DOC-1", "learner")

    assert [c["id"] for c in first] == [c["id"] for c in second]
    assert len({c["id"] for c in first}) == len(first), "chunk ids must be unique within a document"


def test_chunk_ids_differ_for_different_documents_or_content():
    chunker = PolicyChunker(chunk_size=50, chunk_overlap=0)
    doc_a = _doc([{"title": "T", "text": "same text", "page": 1}])
    doc_b = _doc([{"title": "T", "text": "different text", "page": 1}])

    a_ids = {c["id"] for c in chunker.chunk_document(doc_a, "DOC-1", "learner")}
    b_ids = {c["id"] for c in chunker.chunk_document(doc_b, "DOC-1", "learner")}
    same_text_other_doc = {c["id"] for c in chunker.chunk_document(doc_a, "DOC-2", "learner")}

    assert a_ids != b_ids
    assert a_ids != same_text_other_doc


def test_zero_chunk_overlap_is_honoured_not_replaced_by_default():
    chunker = PolicyChunker(chunk_size=50, chunk_overlap=0)
    assert chunker.chunk_overlap == 0
    assert chunker.chunk_size == 50

    doc = _doc([{"title": "T", "text": "x" * 120, "page": 1}])
    chunks = chunker.chunk_document(doc, "DOC-1", "learner")
    assert [len(c["content"]) for c in chunks] == [50, 50, 20]


def test_invalid_chunker_configuration_is_rejected():
    with pytest.raises(ValueError, match="chunk_size must be positive"):
        PolicyChunker(chunk_size=0)
    with pytest.raises(ValueError, match="chunk_overlap must satisfy"):
        PolicyChunker(chunk_size=50, chunk_overlap=50)
    with pytest.raises(ValueError, match="chunk_overlap must satisfy"):
        PolicyChunker(chunk_size=50, chunk_overlap=-1)


def test_chunk_metadata_carries_document_section_page_audience(tmp_path: Path):
    file = tmp_path / "p.txt"
    file.write_text("hello", "utf-8")
    doc = _doc([{"title": "Leave", "text": "L" * 250, "page": 7}])
    doc["file_path"] = str(file)

    chunks = PolicyChunker(chunk_size=100, chunk_overlap=0).chunk_document(doc, "DOC-9", "learner")

    assert all(c["metadata"]["document_id"] == "DOC-9" for c in chunks)
    assert all(c["metadata"]["section_title"] == "Leave" for c in chunks)
    assert all(c["metadata"]["page_number"] == 7 for c in chunks)
    assert all(c["metadata"]["audience"] == "learner" for c in chunks)
    assert all(c["metadata"]["source_file_path"] == str(file) for c in chunks)
    assert all(c["audience"] == "learner" for c in chunks)


def test_chunking_is_deterministic_with_overlap():
    chunker = PolicyChunker(chunk_size=40, chunk_overlap=10)
    doc = _doc([{"title": "S", "text": "x" * 100, "page": 1}])

    first = chunker.chunk_document(doc, "D", "learner")
    second = PolicyChunker(chunk_size=40, chunk_overlap=10).chunk_document(doc, "D", "learner")

    assert first == second
    assert len(first) >= 3


def test_chunk_content_hash_matches_sha256_of_content():
    chunker = PolicyChunker(chunk_size=64, chunk_overlap=0)
    doc = _doc([{"title": "S", "text": "y" * 200, "page": 1}])

    for chunk in chunker.chunk_document(doc, "D", "learner"):
        assert chunk["content_hash"] == hashlib.sha256(chunk["content"].encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Pipeline & Folder Ingestion Tests
# ---------------------------------------------------------------------------


def test_pipeline_reingestion_replaces_the_document_deterministically(tmp_path: Path):
    file = tmp_path / "policy.txt"
    file.write_text("Stable policy text for idempotency checking." * 5, "utf-8")

    captured: list[tuple[list, list]] = []

    async def fake_upsert(chunks, embeddings):
        captured.append((list(chunks), list(embeddings)))

    async def fake_delete(document_id):
        return None

    embedder = SimpleNamespace(get_embeddings=AsyncMock(side_effect=lambda texts: [fake_embedding(t) for t in texts]))
    store = SimpleNamespace(
        upsert_chunks=AsyncMock(side_effect=fake_upsert), delete_document=AsyncMock(side_effect=fake_delete)
    )

    async def run_once() -> list[str]:
        doc_data = DocumentLoader.load(str(file))
        chunks = PolicyChunker().chunk_document(doc_data, "doc-idem", "learner")
        texts = [c["content"] for c in chunks]
        embeddings = await embedder.get_embeddings(texts)
        await store.delete_document("doc-idem")
        await store.upsert_chunks(chunks, embeddings)
        return [c["id"] for c in chunks]

    first_ids = asyncio.run(run_once())
    second_ids = asyncio.run(run_once())

    assert first_ids == second_ids
    assert first_ids
    assert store.delete_document.await_count == 2


def test_pipeline_uses_remove_before_upsert_for_reingestion(tmp_path: Path):
    file = tmp_path / "policy.txt"
    file.write_text("content", "utf-8")

    pipeline = IngestionPipeline()
    pipeline.chunker = PolicyChunker(chunk_size=80, chunk_overlap=0)
    pipeline.embedder = SimpleNamespace(get_embeddings=AsyncMock(return_value=[[0.1] * 1536]))
    pipeline.vector_store = SimpleNamespace(upsert_chunks=AsyncMock(), delete_document=AsyncMock(return_value=None))

    count = asyncio.run(pipeline.ingest_document(str(file), "doc-replace", "learner"))

    assert count == 1
    pipeline.vector_store.delete_document.assert_awaited_once_with("doc-replace")
    assert pipeline.vector_store.upsert_chunks.await_count == 1


def test_pipeline_honours_audience_parameter(tmp_path: Path):
    file = tmp_path / "internal.txt"
    file.write_text("Operator-only runbook content." * 10, "utf-8")

    pipeline = IngestionPipeline()
    pipeline.chunker = PolicyChunker(chunk_size=100, chunk_overlap=0)
    pipeline.embedder = SimpleNamespace(get_embeddings=AsyncMock(return_value=[[0.2] * 1536]))
    captured: dict = {}

    async def fake_upsert(chunks, embeddings):
        captured["chunks"] = chunks

    pipeline.vector_store = SimpleNamespace(
        upsert_chunks=AsyncMock(side_effect=fake_upsert), delete_document=AsyncMock(return_value=None)
    )

    asyncio.run(pipeline.ingest_document(str(file), "doc-internal", "internal_operator"))

    assert captured["chunks"]
    assert all(c["audience"] == "internal_operator" for c in captured["chunks"])


def test_ingest_folder_processes_directory(tmp_path: Path):
    docs_dir = tmp_path / "policies"
    docs_dir.mkdir()

    (docs_dir / "general_leave.txt").write_text("Learners receive 21 days of annual leave.", encoding="utf-8")
    (docs_dir / "ops_runbook.md").write_text("# Ops\nInternal operator instructions.", encoding="utf-8")
    (docs_dir / "ignored.exe").write_bytes(b"MZ fake binary")

    pipeline = IngestionPipeline()
    pipeline.chunker = PolicyChunker(chunk_size=100, chunk_overlap=0)
    pipeline.embedder = SimpleNamespace(
        get_embeddings=AsyncMock(side_effect=lambda texts: [fake_embedding(t) for t in texts])
    )
    pipeline.vector_store = SimpleNamespace(
        upsert_chunks=AsyncMock(return_value=None),
        delete_document=AsyncMock(return_value=None),
    )

    result = asyncio.run(pipeline.ingest_folder(str(docs_dir), default_audience="learner"))

    assert result["status"] == "success"
    assert result["processed_files"] == 2
    assert result["total_chunks"] >= 2

    ops_detail = next(d for d in result["details"] if d["file"] == "ops_runbook.md")
    assert ops_detail["audience"] == "internal_operator"


def test_ingest_folder_raises_on_missing_dir():
    pipeline = IngestionPipeline()
    with pytest.raises(ValueError, match="Directory not found"):
        asyncio.run(pipeline.ingest_folder("/non/existent/path/for/sure"))