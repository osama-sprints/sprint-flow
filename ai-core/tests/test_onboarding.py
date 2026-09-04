"""Tests for proactive role-aware onboarding (Task 5).

--> Target path: tests/test_onboarding.py

Follows the same mocking convention as tests/test_authorisation_sprint1.py:
no live database or Mattermost connection, everything faked via
unittest.mock. Async calls are driven with asyncio.run() inside plain sync
test functions rather than pytest.mark.asyncio, since pytest-asyncio isn't
a declared dependency in this repo (checked: no other test file uses it).
"""

import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services import onboarding


class FakeSession:
    """Minimal stand-in for a SQLModel Session used as a context manager.

    Backed by a dict keyed by primary key, so session.get() / session.add()
    / session.commit() behave like a tiny in-memory table -- enough to
    exercise the real idempotency logic in handle_arrival() without a
    database.
    """

    _store: dict = {}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get(self, model, pk):
        return FakeSession._store.get(pk)

    def add(self, obj):
        FakeSession._store[obj.user_id] = obj

    def commit(self):
        pass


@pytest.fixture(autouse=True)
def reset_store():
    FakeSession._store = {}
    yield
    FakeSession._store = {}


@pytest.fixture
def mock_mattermost():
    with patch.object(onboarding, "mattermost_client") as mm:
        mm.create_direct_channel = AsyncMock(return_value={"id": "dm-channel-1"})
        mm.create_post = AsyncMock(return_value=None)
        yield mm


@pytest.fixture
def mock_session():
    with patch.object(onboarding, "Session", return_value=FakeSession()):
        yield


def test_handle_arrival_sends_exactly_one_greeting_on_replay(mock_session, mock_mattermost):
    """Calling handle_arrival twice for the same user (simulating a
    duplicate `new_user` event / a reconnect) must send exactly one DM,
    not two -- this is the property the brief calls non-negotiable."""
    with patch.object(onboarding, "resolve_role", AsyncMock(return_value="learner")):
        asyncio.run(onboarding.handle_arrival("user_replay_1"))
        asyncio.run(onboarding.handle_arrival("user_replay_1"))

    assert mock_mattermost.create_post.call_count == 1


def test_handle_arrival_is_idempotent_across_many_replays(mock_session, mock_mattermost):
    """Five duplicate events for the same arrival must still produce
    exactly one greeting -- not just "the second call is safe"."""
    with patch.object(onboarding, "resolve_role", AsyncMock(return_value=None)):
        for _ in range(5):
            asyncio.run(onboarding.handle_arrival("user_replay_many"))

    assert mock_mattermost.create_post.call_count == 1


def test_handle_arrival_stores_none_not_a_fake_default(mock_session, mock_mattermost):
    """An unresolved role must be stored as None, never a fabricated
    placeholder -- persisted state should reflect reality, not a display
    convenience."""
    with patch.object(onboarding, "resolve_role", AsyncMock(return_value=None)):
        asyncio.run(onboarding.handle_arrival("user_unassigned"))

    stored = FakeSession._store["user_unassigned"]
    assert stored.role is None


@pytest.mark.parametrize(
    "role,expected_snippet",
    [
        ("learner", "learner"),
        ("mentor", "mentor"),
        ("manager", "manager"),
        ("coordinator", "coordinator"),
        ("some_future_role_not_yet_mapped", "Glad to have you here"),
        (None, "Glad to have you here"),
    ],
)
def test_greeting_is_role_specific(role, expected_snippet):
    """Each known role gets distinct orientation text; unknown/unset roles
    fall back to the generic message rather than crashing or showing a
    blank/incorrect role name."""
    assert expected_snippet in onboarding._greeting_for(role)