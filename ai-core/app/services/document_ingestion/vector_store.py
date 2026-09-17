from typing import List, Dict, Any
from sqlalchemy.future import select
from sqlalchemy import delete
from app.services.database import session_scope
from app.models.policy import PolicyDocumentChunk


class PolicyVectorStore:
    async def upsert_chunks(self, chunks: List[Dict[str, Any]], embeddings: List[List[float]]):
        async with session_scope() as session:
            for chunk_data, emb in zip(chunks, embeddings, strict=False):
                existing = await session.get(PolicyDocumentChunk, chunk_data["id"])
                if existing:
                    existing.doc_metadata = chunk_data["metadata"]
                    continue
                else:
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
            stmt = delete(PolicyDocumentChunk).where(PolicyDocumentChunk.document_id == document_id)  # type: ignore
            await session.execute(stmt)
            await session.commit()

    async def similarity_search(
        self, query_embedding: List[float], audience: str | None = None, top_k: int = 5
    ) -> List[Dict[str, Any]]:
        async with session_scope() as session:
            distance_expr = PolicyDocumentChunk.embedding.l2_distance(query_embedding)
            allowed_audiences = ("admin", "learner", "public") if audience in (None, "admin", "superadmin") else (audience,)

            stmt = (
                select(PolicyDocumentChunk, distance_expr.label("distance"))
                .where(PolicyDocumentChunk.audience.in_(allowed_audiences))  # type: ignore
                .order_by(distance_expr)
                .limit(top_k)
            )
            result = await session.execute(stmt)
            records = result.all()

            return [
                {
                    "content": row[0].content,
                    "metadata": row[0].doc_metadata,
                    "similarity_score": 1.0 / (1.0 + float(row[1])),
                }
                for row in records
            ]
