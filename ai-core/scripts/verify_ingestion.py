import asyncio
import logging
import sys
from pathlib import Path

from sqlalchemy import text

from app.core.config import settings
from app.services.document_ingestion.chunker import PolicyChunker
from app.services.document_ingestion.embeddings import EmbeddingService
from app.services.document_ingestion.pipeline import IngestionPipeline, ingest_all
from app.services.document_ingestion.vector_store import PolicyVectorStore
from app.services.database import database_service

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("verify_ingestion")


DOCUMENTS = {
    "acc_faqs": ("_ACC FAQs Presentation (editable).pdf", "learner"),
    "ops_circle_chatbot_scripts": ("Ops Circle Chatbot Scripts.docx", "internal_operator"),
}


async def chunk_count() -> int:
    async with database_service.engine.connect() as conn:
        result = await conn.execute(text("SELECT count(*) FROM policy_document_chunks"))
        return int(result.scalar_one())


async def verify_audience_filter() -> None:
    embedder = EmbeddingService()
    query_embedding = (await embedder.get_embeddings(["What is the program?"]))[0]
    results = await PolicyVectorStore().similarity_search(query_embedding, audience="learner", top_k=20)

    assert results, "Learner semantic search returned no results"
    assert all(result["metadata"].get("audience") == "learner" for result in results), (
        "Learner semantic search returned a non-learner chunk"
    )
    assert not any(result["metadata"].get("audience") == "internal_operator" for result in results), (
        "Learner semantic search returned an internal_operator chunk"
    )
    logger.info("✓ learner semantic search excludes internal_operator chunks.")


async def verify_source_file_provenance() -> None:
    async with database_service.engine.connect() as conn:
        result = await conn.execute(
            text(
                "SELECT document_id, metadata FROM policy_document_chunks "
                "WHERE document_id IN ('acc_faqs', 'ops_circle_chatbot_scripts')"
            )
        )
        rows = result.fetchall()

    expected_paths = {
        "acc_faqs": "/app/data/sample_policies/_ACC FAQs Presentation (editable).pdf",
        "ops_circle_chatbot_scripts": "/app/data/sample_policies/Ops Circle Chatbot Scripts.docx",
    }
    assert rows, "No policy chunks found for provenance verification"
    for document_id, metadata in rows:
        assert metadata.get("source_file_path") == expected_paths[document_id], (
            f"Missing source_file_path metadata for {document_id}"
        )
        assert metadata.get("page_number") is not None, f"Missing page_number metadata for {document_id}"
        assert metadata.get("section_title"), f"Missing section_title metadata for {document_id}"
    logger.info("✓ source file paths are stored with page and section provenance.")


def verify_chunker_configuration() -> None:
    chunker = PolicyChunker()
    assert chunker.chunk_size == settings.POLICY_CHUNK_SIZE, "Chunker does not use POLICY_CHUNK_SIZE"
    assert chunker.chunk_overlap == settings.POLICY_CHUNK_OVERLAP, "Chunker does not use POLICY_CHUNK_OVERLAP"
    logger.info("✓ chunker uses configured POLICY_CHUNK_SIZE and POLICY_CHUNK_OVERLAP.")


async def verify_idempotency() -> None:
    before = await chunk_count()
    await ingest_all()
    after = await chunk_count()
    assert after == before, f"Consecutive ingestion grew chunk count: {before} -> {after}"
    logger.info("✓ consecutive ingestion is idempotent (%d chunks).", after)


async def verify_pruning() -> None:
    document_id = "ops_circle_chatbot_scripts"
    store = PolicyVectorStore()
    await store.delete_document(document_id)
    async with database_service.engine.connect() as conn:
        result = await conn.execute(
            text("SELECT count(*) FROM policy_document_chunks WHERE document_id = :document_id"),
            {"document_id": document_id},
        )
        remaining = int(result.scalar_one())
    assert remaining == 0, f"Document pruning left {remaining} chunks"

    filename, audience = DOCUMENTS[document_id]
    await IngestionPipeline().ingest_document(f"/app/data/sample_policies/{filename}", document_id, audience)
    logger.info("✓ document pruning removed stale chunks and restored the document.")


async def verify_pipeline():
    logger.info("Verifying pgvector extension and policy_document_chunks table...")
    async with database_service.engine.connect() as conn:
        # Check pgvector extension
        ext_check = await conn.execute(text("SELECT extname FROM pg_extension WHERE extname = 'vector';"))
        assert ext_check.fetchone() is not None, "pgvector extension is NOT installed!"
        logger.info("✓ pgvector extension is active.")

        # Check table creation
        table_check = await conn.execute(
            text("SELECT table_name FROM information_schema.tables WHERE table_name = 'policy_document_chunks';")
        )
        assert table_check.fetchone() is not None, "policy_document_chunks table does NOT exist!"
        logger.info("✓ policy_document_chunks table exists.")

    data_dir = Path("/app/data/sample_policies")
    assert all((data_dir / filename).is_file() for filename, _ in DOCUMENTS.values()), (
        "Required sample policy documents are not mounted"
    )
    verify_chunker_configuration()
    await verify_idempotency()
    await verify_source_file_provenance()
    await verify_audience_filter()
    await verify_pruning()
    logger.info("All ingestion verification assertions passed!")


if __name__ == "__main__":
    try:
        asyncio.run(verify_pipeline())
    except Exception as e:
        logger.error(f"Verification failed: {e}")
        sys.exit(1)
