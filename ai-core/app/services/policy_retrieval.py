from typing import Any, Dict, List, Tuple
from app.core.logging import logger
from app.services.document_ingestion.embeddings import generate_embeddings

try:
    # Import the PolicyVectorStore class from your vector_store.py
    from app.services.document_ingestion.vector_store import PolicyVectorStore
except ImportError:
    PolicyVectorStore = None


async def get_grounded_answer_or_refusal(
    query: str, audience: str | None = None, top_k: int = 5, threshold: float = 0.6
) -> Tuple[str, List[Dict[str, Any]]]:
    if not PolicyVectorStore:
        logger.warning("Vector store is not available. Returning empty results.")
        raw_results = []
    else:
        try:
            query_embedding = await generate_embeddings(query)

            store = PolicyVectorStore()
            raw_results = await store.similarity_search(
                query_embedding=query_embedding, audience=audience, top_k=top_k
            )
        except Exception as e:
            logger.error(f"Vector store search failed: {e}")
            return "error", []

    if not raw_results:
        return "no_match", []

    normalized_docs: List[Dict[str, Any]] = []
    for item in raw_results:
        if isinstance(item, tuple) and len(item) == 2:
            doc, score = item
            doc_dict = dict(doc) if isinstance(doc, dict) else doc.__dict__
            doc_dict["similarity_score"] = float(score)
            normalized_docs.append(doc_dict)
        elif isinstance(item, dict):
            normalized_docs.append(item)

    grounded_docs = [doc for doc in normalized_docs if doc.get("similarity_score", 0.0) >= threshold]

    if not grounded_docs:
        return "no_match", []

    return "grounded", grounded_docs
