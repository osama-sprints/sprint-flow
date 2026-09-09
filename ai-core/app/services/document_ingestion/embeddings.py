from typing import List
from openai import AsyncOpenAI
from app.core.config import settings


class EmbeddingService:
    def __init__(self):
        self.client = AsyncOpenAI(base_url=settings.OPENAI_BASE_URL, api_key=settings.OPENAI_API_KEY)

    async def get_embeddings(self, texts: List[str]) -> List[List[float]]:
        response = await self.client.embeddings.create(
            model=settings.POLICY_EMBEDDING_MODEL,
            input=texts,
            dimensions=settings.POLICY_EMBEDDING_DIM,
        )
        return [item.embedding for item in response.data]


async def generate_embeddings(query: str) -> List[float]:
    embeddings = await EmbeddingService().get_embeddings([query])
    return embeddings[0]
