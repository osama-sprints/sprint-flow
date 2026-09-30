import hashlib
from typing import List, Dict, Any
from app.core.config import settings


class PolicyChunker:
    def __init__(self, chunk_size: int | None = None, chunk_overlap: int | None = None):
        # Explicit ``0`` is a valid configuration and must not fall back to the
        # settings default (a plain ``or`` would silently convert 0 -> default
        # and, with overlap >= chunk_size, make the stride zero or negative).
        self.chunk_size = chunk_size if chunk_size is not None else settings.POLICY_CHUNK_SIZE
        self.chunk_overlap = chunk_overlap if chunk_overlap is not None else settings.POLICY_CHUNK_OVERLAP
        if self.chunk_size <= 0:
            raise ValueError(f"chunk_size must be positive, got {self.chunk_size}")
        if not 0 <= self.chunk_overlap < self.chunk_size:
            raise ValueError(
                f"chunk_overlap must satisfy 0 <= overlap < chunk_size "
                f"(overlap={self.chunk_overlap}, chunk_size={self.chunk_size}); "
                "otherwise chunking would not advance"
            )

    def chunk_document(self, doc_data: Dict[str, Any], doc_id: str, audience: str) -> List[Dict[str, Any]]:
        chunks = []
        chunk_idx = 0

        for sec in doc_data.get("sections", []):
            text = sec["text"]
            start = 0
            while start < len(text):
                end = start + self.chunk_size
                chunk_text = text[start:end]

                content_hash = hashlib.sha256(chunk_text.encode("utf-8")).hexdigest()
                deterministic_id = hashlib.sha256(f"{doc_id}_{chunk_idx}_{content_hash}".encode("utf-8")).hexdigest()

                chunks.append(
                    {
                        "id": deterministic_id,
                        "document_id": doc_id,
                        "chunk_index": chunk_idx,
                        "audience": audience,
                        "content": chunk_text,
                        "content_hash": content_hash,
                        "metadata": {
                            "document_id": doc_id,
                            "section_title": sec.get("title", "Root"),
                            "page_number": sec.get("page", 1),
                            "audience": audience,
                            "source_file_path": sec.get("file_path", doc_data.get("file_path")),
                        },
                    }
                )
                chunk_idx += 1
                start += self.chunk_size - self.chunk_overlap

        return chunks
