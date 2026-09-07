import hashlib
from typing import List, Dict, Any
from app.core.config import settings

class PolicyChunker:
    def __init__(self, chunk_size: int = None, chunk_overlap: int = None):
        self.chunk_size = chunk_size or settings.POLICY_CHUNK_SIZE
        self.chunk_overlap = chunk_overlap or settings.POLICY_CHUNK_OVERLAP

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

                chunks.append({
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
                    },
                })
                chunk_idx += 1
                start += (self.chunk_size - self.chunk_overlap)

        return chunks