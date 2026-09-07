import asyncio
from pathlib import Path

from app.services.document_ingestion.loaders import DocumentLoader
from app.services.document_ingestion.chunker import PolicyChunker
from app.services.document_ingestion.embeddings import EmbeddingService
from app.services.document_ingestion.vector_store import PolicyVectorStore


class IngestionPipeline:
    def __init__(self):
        self.chunker = PolicyChunker()
        self.embedder = EmbeddingService()
        self.vector_store = PolicyVectorStore()

    async def ingest_document(self, file_path: str, document_id: str, audience: str) -> int:
        doc_data = DocumentLoader.load(file_path)
        chunks = self.chunker.chunk_document(doc_data, document_id, audience)
        texts = [c["content"] for c in chunks]
        embeddings = await self.embedder.get_embeddings(texts)
        await self.vector_store.upsert_chunks(chunks, embeddings)
        return len(chunks)

    async def remove_document(self, document_id: str):
        await self.vector_store.delete_document(document_id)


async def ingest_all(data_dir: str = "/app/data/sample_policies") -> None:
    """Ingest the bundled learner and operator policy documents."""
    pipeline = IngestionPipeline()
    documents = (
        ("_ACC FAQs Presentation (editable).pdf", "acc_faqs", "learner"),
        ("Ops Circle Chatbot Scripts.docx", "ops_circle_chatbot_scripts", "internal_operator"),
    )

    for filename, document_id, audience in documents:
        file_path = Path(data_dir) / filename
        if not file_path.is_file():
            raise FileNotFoundError(f"Policy document not found: {file_path}")
        count = await pipeline.ingest_document(str(file_path), document_id, audience)
        print(f"Ingested {count} chunks from {filename} ({audience})")


if __name__ == "__main__":
    asyncio.run(ingest_all())