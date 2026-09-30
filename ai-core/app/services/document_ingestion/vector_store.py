from typing import List, Dict, Any
from sqlalchemy.future import select
from sqlalchemy import delete
from app.services.database import session_scope
from app.models.policy import PolicyDocumentChunk
import hashlib


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
            allowed_audiences = (
                ("admin", "learner", "public", "internal_operator")
                if audience in (None, "admin", "superadmin")
                else (audience,)
            )

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

    async def index_knowledge_candidate(
        self,
        *,
        escalation_id: int,
        candidate_id: int,
        reviewer_id: int,
        audience: str,
        statement: str,
        embedding: List[float],
    ) -> None:
        """Index one approved knowledge candidate, the same way any other
        chunk is stored -- only ever called after a candidate has been
        approved (see app.services.knowledge_review.approve_candidate).

        The chunk id is deterministic (a hash of the escalation id and the
        statement's own content, mirroring PolicyChunker's own id scheme),
        so calling this twice for the same escalation and the same
        statement text updates the existing row instead of creating a
        second one -- the same idempotency guarantee every other row in
        this table already has.

        Args:
            escalation_id: The source EscalationTicket.id -- carried in the
                chunk's metadata so provenance survives into search results.
            candidate_id: The KnowledgeCandidate.id, for the same reason.
            reviewer_id: Who approved it, for audit.
            audience: "learner" or "internal_operator" -- this is the field
                similarity_search() filters on, so an approved candidate is
                only ever reachable by the audience it was actually approved for.
            statement: The narrowed knowledge text to embed and store.
            embedding: The statement's own embedding vector.
        """
        content_hash = hashlib.sha256(statement.encode("utf-8")).hexdigest()
        chunk_id = hashlib.sha256(f"escalation:{escalation_id}_0_{content_hash}".encode("utf-8")).hexdigest()

        chunk = {
            "id": chunk_id,
            "document_id": f"escalation:{escalation_id}",
            "chunk_index": 0,
            "audience": audience,
            "content": statement,
            "content_hash": content_hash,
            "metadata": {
                "source": "knowledge_candidate",
                "escalation_id": escalation_id,
                "candidate_id": candidate_id,
                "reviewer_id": reviewer_id,
            },
        }
        await self.upsert_chunks([chunk], [embedding])
