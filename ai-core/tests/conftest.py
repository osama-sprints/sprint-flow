"""Shared pytest fixtures for the SprintFlow testing suite.

Provides common test doubles (Mattermost client, LLM service, Identity repository),
deterministic embeddings, database integration lifecycle, and RequesterContext management.
"""

import asyncio  # noqa: F401 - kept for test helpers that may use it
from contextlib import contextmanager
from typing import Callable, Generator
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.requester import RequesterContext, current_requester
from app.models.enums import RoleKey


# --- Context and Authorisation -----------------------------------------------


@contextmanager
def set_requester(requester: RequesterContext) -> Generator[None, None, None]:
    """Bind a requester context for the duration of a test block."""
    token = current_requester.set(requester)
    try:
        yield
    finally:
        current_requester.reset(token)


@pytest.fixture
def fake_requester() -> Callable[..., RequesterContext]:
    """Factory for standard requester contexts used across tests."""

    def _make(
        role: RoleKey | None = RoleKey.LEARNER,
        channel_id: str = "chan_1",
        is_superadmin: bool = False,
        user_id: int = 1,
        mattermost_user_id: str = "mm_123",
        username: str = "testuser",
        email: str = "test@example.test",
        team_id: str = "team_1",
        channel_type: str = "O",
    ) -> RequesterContext:
        roles = {channel_id: role.value} if role else {}
        return RequesterContext(
            mattermost_user_id=mattermost_user_id,
            username=username,
            email=email,
            channel_id=channel_id,
            team_id=team_id,
            channel_type=channel_type,
            user_id=user_id,
            is_superadmin=is_superadmin,
            channel_roles=roles,
        )

    return _make


# --- External Service Doubles ------------------------------------------------


class FakeMattermostClient:
    """In-memory stub for Mattermost REST calls."""

    def __init__(self):
        self.posts: list[dict] = []
        self.channels: dict[str, dict] = {}
        self.users: dict[str, dict] = {}
        self.dm_channels: dict[str, str] = {}

        # Method mocks
        self.create_post = AsyncMock(side_effect=self._create_post)
        self.create_direct_channel = AsyncMock(side_effect=self._create_direct_channel)
        self.get_user = AsyncMock(side_effect=self._get_user)

    async def _create_post(self, channel_id: str, message: str, **kwargs) -> dict:
        post = {"id": f"post_{len(self.posts)}", "channel_id": channel_id, "message": message, **kwargs}
        self.posts.append(post)
        return post

    async def _create_direct_channel(self, user_id: str) -> dict:
        if user_id not in self.dm_channels:
            self.dm_channels[user_id] = f"dm_{user_id}"
        return {"id": self.dm_channels[user_id]}

    async def _get_user(self, user_id: str) -> dict | None:
        return self.users.get(user_id)


class FakeLLMService:
    """Deterministic LLM responder."""

    def __init__(self):
        self.call = AsyncMock()
        self.embeddings = AsyncMock()

    def set_response(self, text: str):
        response = MagicMock()
        response.content = text
        self.call.return_value = response
        return self

    def set_embedding(self, vector: list[float]):
        self.embeddings.return_value = vector
        return self


@pytest.fixture
def fake_mattermost() -> FakeMattermostClient:
    return FakeMattermostClient()


@pytest.fixture
def fake_llm() -> FakeLLMService:
    return FakeLLMService()


# --- Database Integration ----------------------------------------------------


@pytest.fixture
def anyio_backend():
    return "asyncio"


# --- Singleton lifecycle management ------------------------------------------


@pytest.fixture(autouse=True)
def reset_mattermost_client_between_tests():
    """Reset the global mattermost_client httpx connection after every test.

    The singleton ``mattermost_client = MattermostClient()`` lazily creates an
    ``httpx.AsyncClient`` on the current event loop. When a test (often an
    anyio/asyncio test) creates that client and the test ends, the event loop is
    closed. Any subsequent test that tries to *close* the still-open client hits
    ``RuntimeError('Event loop is closed')`` because it's still bound to the
    previous loop.

    Forcibly setting ``_client = None`` after each test ensures every test
    gets a fresh client on its own event loop.
    """
    yield
    try:
        from app.services.mattermost import mattermost_client  # noqa: PLC0415

        mattermost_client._client = None
        mattermost_client._bot_user_id = None
    except Exception:  # noqa: BLE001
        pass  # never fail a test for cleanup
