"""Long-term memory service: mem0 over an external Qdrant instance.

Qdrant is deliberately outside docker-compose. Keeping the vector store off the
Postgres cluster means a vector-side fault can never reach Mattermost's
database, and it removes the need for any table-creation workarounds — mem0
manages its own Qdrant collection.

Memory is scoped per PERSON (``user_id``), not per conversation. That is what
makes per-thread context isolation safe: durable facts about someone follow
them between threads, while the transcript of one thread never leaks into
another.
"""

from mem0 import AsyncMemory

from app.core.cache import (
    cache_key,
    cache_service,
)
from app.core.config import settings
from app.core.logging import logger


class MemoryService:
    """Service for managing long-term memory using mem0 over Qdrant."""

    def __init__(self):
        """Initialize the memory service."""
        self._memory: AsyncMemory | None = None

    async def _get_memory(self) -> AsyncMemory:
        if self._memory is None:
            if not settings.QDRANT_URL:
                # Refuse rather than degrade: with no url and no host, mem0's
                # Qdrant config silently falls back to a local on-disk store at
                # /tmp/qdrant, so memory would appear to work and then vanish
                # with the container.
                raise RuntimeError("QDRANT_URL is not set — long-term memory has no vector store")

            self._memory = await AsyncMemory.from_config(
                config_dict={
                    "vector_store": {
                        "provider": "qdrant",
                        "config": {
                            "collection_name": settings.LONG_TERM_MEMORY_COLLECTION_NAME,
                            "embedding_model_dims": settings.QDRANT_EMBEDDING_DIMS,
                            "url": settings.QDRANT_URL,
                            "api_key": settings.QDRANT_API_KEY,
                            # Persist vectors to disk on the Qdrant side rather
                            # than keeping the collection memory-only.
                            "on_disk": True,
                        },
                    },
                    "llm": {
                        "provider": "openai",
                        "config": {"model": settings.LONG_TERM_MEMORY_MODEL},
                    },
                    "embedder": {
                        "provider": "openai",
                        "config": {"model": settings.LONG_TERM_MEMORY_EMBEDDER_MODEL},
                    },
                }
            )
        return self._memory

    async def initialize(self) -> None:
        """Pre-warm the mem0 AsyncMemory instance and its Qdrant client.

        Call once at startup so the first search() or add() does not pay the
        cold-init cost of building the client and checking the collection.
        """
        await self._get_memory()
        logger.info("memory_service_initialized")

    async def search(self, user_id: str | None, query: str) -> str:
        """Search relevant memories for a user.

        Checks cache first; on miss, queries mem0 and caches the result.

        Returns formatted memory string, or empty string on failure or when
        no user_id is supplied (anonymous sessions skip long-term memory
        rather than pooling under a shared partition).
        """
        if user_id is None:
            return ""
        try:
            # Check cache first
            key = cache_key("memory", str(user_id), query)
            cached = await cache_service.get(key)
            if cached is not None:
                logger.debug("memory_search_cache_hit", user_id=user_id)
                return cached

            memory = await self._get_memory()
            results = await memory.search(user_id=str(user_id), query=query)
            result = "\n".join([f"* {r['memory']}" for r in results["results"]])

            # Cache successful results
            if result:
                await cache_service.set(key, result)

            return result
        except Exception as e:
            logger.error("failed_to_get_relevant_memory", error=str(e), user_id=user_id, query=query)
            return ""

    async def add(self, user_id: str | None, messages: list[dict], metadata: dict | None = None) -> None:
        """Add messages to long-term memory for a user.

        No-op when ``user_id`` is ``None`` (see ``search`` for rationale).
        """
        if user_id is None:
            return
        try:
            memory = await self._get_memory()
            await memory.add(messages, user_id=str(user_id), metadata=metadata)
            logger.info("long_term_memory_updated_successfully", user_id=user_id)
        except Exception as e:
            logger.exception("failed_to_update_long_term_memory", user_id=user_id, error=str(e))


memory_service = MemoryService()
