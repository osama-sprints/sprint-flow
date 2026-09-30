from typing import List
from openai import AsyncOpenAI
from app.core.config import settings

# Gemini (via LiteLLM) hard limit
MAX_BATCH_SIZE = 100


class EmbeddingService:
    def __init__(self):
        self.client = AsyncOpenAI(
            base_url=settings.OPENAI_BASE_URL,
            api_key=settings.OPENAI_API_KEY,
        )

    async def get_embeddings(self, texts: List[str]) -> List[List[float]]:
        """Return embeddings for a list of texts, automatically batching to respect the 100-item limit."""
        if not texts:
            return []

        all_embeddings: List[List[float]] = []

        for i in range(0, len(texts), MAX_BATCH_SIZE):
            batch = texts[i : i + MAX_BATCH_SIZE]
            response = await self.client.embeddings.create(
                model=settings.POLICY_EMBEDDING_MODEL,
                input=batch,
                dimensions=settings.POLICY_EMBEDDING_DIM,
            )
            all_embeddings.extend([item.embedding for item in response.data])

        return all_embeddings


async def generate_embeddings(query: str) -> List[float]:
    embeddings = await EmbeddingService().get_embeddings([query])
    return embeddings[0]