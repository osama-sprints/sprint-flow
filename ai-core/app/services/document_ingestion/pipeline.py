import asyncio
from pathlib import Path
from typing import Any, Dict

from app.core.logging import logger
from app.services.document_ingestion.chunker import PolicyChunker
from app.services.document_ingestion.embeddings import EmbeddingService
from app.services.document_ingestion.loaders import DocumentLoader
from app.services.document_ingestion.vector_store import PolicyVectorStore

SUPPORTED_EXTENSIONS = {".pdf", ".docx", ".md", ".txt"}


class IngestionPipeline:
    def __init__(self):
        self.chunker = PolicyChunker()
        self.embedder = EmbeddingService()
        self.vector_store = PolicyVectorStore()

    async def ingest_document(self, file_path: str, document_id: str, audience: str) -> int:
        doc_data = DocumentLoader.load(file_path)
        chunks = self.chunker.chunk_document(doc_data, document_id, audience)
        if not chunks:
            return 0

        texts = [c["content"] for c in chunks]
        embeddings = await self.embedder.get_embeddings(texts)

        await self.remove_document(document_id)
        await self.vector_store.upsert_chunks(chunks, embeddings)
        return len(chunks)

    async def remove_document(self, document_id: str):
        await self.vector_store.delete_document(document_id)

    async def ingest_folder(
        self, folder_path: str, default_audience: str = "learner"
    ) -> Dict[str, Any]:
        """Ingest all supported policy and knowledge documents inside a target directory recursively."""
        dir_path = Path(folder_path)
        if not dir_path.is_dir():
            raise ValueError(f"Directory not found or invalid: {folder_path}")

        files = [
            f
            for f in dir_path.rglob("*")
            if f.is_file() and f.suffix.lower() in SUPPORTED_EXTENSIONS
        ]

        if not files:
            return {
                "status": "success",
                "processed_files": 0,
                "total_chunks": 0,
                "details": [],
            }

        processed_files = 0
        total_chunks = 0
        details = []

        for file_path in files:
            doc_id = file_path.stem.lower().replace(" ", "_").replace("-", "_")
            audience = (
                "internal_operator"
                if any(k in doc_id for k in ("operator", "ops", "internal"))
                else default_audience
            )
            try:
                chunk_count = await self.ingest_document(str(file_path), doc_id, audience)
                processed_files += 1
                total_chunks += chunk_count
                details.append(
                    {
                        "file": file_path.name,
                        "document_id": doc_id,
                        "audience": audience,
                        "status": "success",
                        "chunks": chunk_count,
                    }
                )
            except Exception as e:
                logger.error(f"Failed to ingest file '{file_path.name}': {e}")
                details.append(
                    {
                        "file": file_path.name,
                        "document_id": doc_id,
                        "status": "failed",
                        "error": str(e),
                    }
                )

        return {
            "status": "success",
            "processed_files": processed_files,
            "total_chunks": total_chunks,
            "details": details,
        }


async def ingest_all(data_dir: str = "/app/data/sample_policies") -> None:
    """Ingest all policy documents inside the target directory."""
    pipeline = IngestionPipeline()
    try:
        result = await pipeline.ingest_folder(data_dir)
        print(
            f"Folder ingestion summary: Processed {result['processed_files']} file(s), "
            f"{result['total_chunks']} chunk(s)."
        )
    except Exception as e:
        logger.error(f"Failed folder ingestion on {data_dir}: {e}")


if __name__ == "__main__":
    asyncio.run(ingest_all())