from typing import List, Dict, Any
from sqlalchemy.future import select
from sqlalchemy import delete
from app.services.database import session_scope
from app.models.policy import PolicyDocumentChunk

class PolicyVectorStore:
    async def upsert_chunks(self, chunks: List[Dict[str, Any]], embeddings: List[List[float]]):
        async with session_scope() as session:
            for chunk_data, emb in zip(chunks, embeddings):
                existing = await session.get(PolicyDocumentChunk, chunk_data["id"])
                if not existing:
                    chunk = PolicyDocumentChunk(
                        id=chunk_data["id"],
                        document_id=chunk_data["document_id"],
                        chunk_index=chunk_data["chunk_index"],
                        audience=chunk_data["audience"],
                        content=chunk_data["content"],
                        embedding=emb,
                        doc_metadata=chunk_data["metadata"],
                        content_hash=chunk_data["content_hash"],
                    )
                    session.add(chunk)
            await session.commit()

    async def delete_document(self, document_id: str):
        async with session_scope() as session:
            stmt = delete(PolicyDocumentChunk).where(PolicyDocumentChunk.document_id == document_id)
            await session.execute(stmt)
            await session.commit()

    async def similarity_search(self, query_embedding: List[float], audience: str, top_k: int = 5) -> List[Dict[str, Any]]:
        async with session_scope() as session:
            stmt = (
                select(PolicyDocumentChunk)
                .where(PolicyDocumentChunk.audience == audience)
                .order_by(PolicyDocumentChunk.embedding.l2_distance(query_embedding))
                .limit(top_k)
            )
            result = await session.execute(stmt)
            records = result.scalars().all()
            return [{"content": r.content, "metadata": r.doc_metadata} for r in records]