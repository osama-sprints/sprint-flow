from typing import Any , Dict , List , Tuple
from app.core.logging import logger

try :
    from app.services.document_ingestion.vector_store import similarity_search
except ImportError :
    async def similarity_search ( query : str , audience : str , top_k : int = 5 ) -> List [ Tuple [ Dict [ str , Any ] , float ] ] :
        logger.warning ( "Vector store is not available. Returning empty results." )
        return []
async def get_grounded_answer_or_refusal(
    query : str , 
    audience : str,
    top_k : int = 5,
    threshold : float = 0.75
)-> Tuple[str, List[Dict[str,Any]]]:
    try:
        results = await similarity_search(query=query, audience=audience, top_k=top_k)
    except Exception as e:
        logger.error(f"Vector store search failed: {e}")
        return "error", []

    if not results:
        return "no_match", []

    top_score = max((doc.get("similarity_score", 0.0) for doc in results), default=0.0)

    if top_score < threshold:
        return "weak_match", []

    return "grounded", results