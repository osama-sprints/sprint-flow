"""Long-term memory service: mem0 over an external Qdrant instance.

Qdrant is deliberately outside docker-compose. Keeping the vector store off the
Postgres cluster means a vector-side fault can never reach Mattermost's
database, and it removes the need for any table-creation workarounds — mem0
manages its own Qdrant collection.

Memory is scoped per PERSON (``user_id``), not per conversation. That is what
makes per-thread context isolation safe: durable facts about someone follow
them between threads, while the transcript of one thread never leaks into
another.

Following someone between threads is not the same as following them into any
room. A memory formed in a direct message is something the person told the
assistant privately, and repeating it in a channel discloses it to everybody
there — quietly, inside an answer to an unrelated question. So every memory
records where it was formed, and a search only returns what may be surfaced
where the reply will land (see ``discussion.access.may_disclose``). Memories
written before this existed carry no provenance and are treated as private:
they surface in direct messages and nowhere else.
"""

from typing import (
    Any,
    Mapping,
)

from mem0 import AsyncMemory

from app.core.cache import (
    cache_key,
    cache_service,
)
from app.core.config import settings
from app.core.logging import logger
from app.core.requester import current_requester
from app.services.discussion.access import may_disclose


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
        """Search a person's memories, returning only what may be said where the reply lands.

        Checks cache first; on miss, queries mem0 and caches the result. The
        cache key carries the destination, so the same question asked in a DM
        and in a public channel cannot share an answer.

        Args:
            user_id: The person the memories belong to.
            query: What to search for.

        Returns:
            str: The memories, one per line, or an empty string on failure or
            when no ``user_id`` is supplied (anonymous sessions skip long-term
            memory rather than pooling under a shared partition).
        """
        if user_id is None:
            return ""
        destination_type, destination_id = _destination()
        try:
            # Check cache first
            key = cache_key("memory", str(user_id), destination_type, destination_id, query)
            cached = await cache_service.get(key)
            if cached is not None:
                logger.debug("memory_search_cache_hit", user_id=user_id)
                return cached

            memory = await self._get_memory()
            results = await memory.search(user_id=str(user_id), query=query)
            found = list(results["results"])
            allowed = [
                entry for entry in found if may_surface(entry.get("metadata"), destination_type, destination_id)
            ]
            if len(allowed) != len(found):
                logger.info(
                    "memory_withheld_from_destination",
                    user_id=user_id,
                    withheld=len(found) - len(allowed),
                    destination_type=destination_type,
                )
            result = "\n".join([f"* {entry['memory']}" for entry in allowed])

            # Cache successful results
            if result:
                await cache_service.set(key, result)

            return result
        except Exception as e:
            logger.error("failed_to_get_relevant_memory", error=str(e), user_id=user_id, query=query)
            return ""

    async def add(self, user_id: str | None, messages: list[dict], metadata: dict | None = None) -> None:
        """Add messages to long-term memory for a user, recording where they were said.

        The provenance stamped here is what later lets a search withhold a
        private memory from a public reply, so it is added even when the caller
        passed no metadata at all.

        Args:
            user_id: The person the memories belong to.
            messages: The messages to learn from.
            metadata: Extra facts to store alongside.

        No-op when ``user_id`` is ``None`` (see ``search`` for rationale).
        """
        if user_id is None or not messages:
            return
        try:
            memory = await self._get_memory()
            await memory.add(messages, user_id=str(user_id), metadata=with_provenance(metadata))
            logger.info("long_term_memory_updated_successfully", user_id=user_id)
        except Exception as e:
            logger.exception("failed_to_update_long_term_memory", user_id=user_id, error=str(e))


def _destination() -> tuple[str, str]:
    """Where the current turn's reply will be posted.

    Returns:
        tuple: Channel type and channel id, both empty outside a turn.
    """
    requester = current_requester.get()
    if requester is None:
        return "", ""
    return requester.channel_type, requester.channel_id


def with_provenance(metadata: dict | None) -> dict:
    """Stamp the current conversation onto metadata about to be stored.

    Args:
        metadata: Whatever the caller already wanted to store.

    Returns:
        dict: The same facts, plus where they were said.
    """
    channel_type, channel_id = _destination()
    return {**(metadata or {}), "channel_type": channel_type, "channel_id": channel_id}


def may_surface(metadata: Mapping[str, Any] | None, destination_type: str, destination_id: str) -> bool:
    """Whether one stored memory may appear in a reply posted here.

    Args:
        metadata: The memory's stored metadata, carrying where it was formed.
        destination_type: Channel type the reply will be posted in.
        destination_id: That channel's id.

    Returns:
        bool: True when surfacing it discloses it to nobody new.
    """
    source_type = str((metadata or {}).get("channel_type") or "")
    source_id = str((metadata or {}).get("channel_id") or "")
    if not destination_type:
        # No turn bound (a probe, a test, an API caller): nothing is being
        # posted anywhere, so there is nobody to disclose to.
        return True
    if not source_type:
        # Written before provenance was recorded. Its origin is unknown, and an
        # unknown origin is treated as a private one.
        return destination_type == "D"
    return may_disclose(source_type, source_id, destination_type, destination_id)


memory_service = MemoryService()
