"""Which transport answers a post.

The websocket listener defers a trigger-word message to the outgoing webhook
only where that webhook actually fires. Before this, any message starting
with a trigger word in ANY public channel was assumed to be the webhook's and
went unanswered wherever the webhook was not configured.
"""

import pytest

from app.core.config import settings
from app.services import mattermost_ws
from app.services.mattermost_ws import MattermostWebSocketListener

HOOKS = [
    {
        "channel_id": "chan-hooked",
        "trigger_words": ["@sprintflow-assistant"],
        "callback_urls": ["http://ai-core:8000/api/v1/mattermost/webhook"],
    },
    {
        "channel_id": "chan-other-integration",
        "trigger_words": ["!jira"],
        "callback_urls": ["https://example.com/jira"],
    },
]


@pytest.fixture
def listener(monkeypatch):
    calls = {"count": 0}

    async def list_outgoing_webhooks():
        calls["count"] += 1
        return HOOKS

    monkeypatch.setattr(mattermost_ws.mattermost_client, "list_outgoing_webhooks", list_outgoing_webhooks)
    monkeypatch.setattr(settings, "MATTERMOST_TRIGGER_WORDS", ["@sprintflow-assistant", "!ask"])
    instance = MattermostWebSocketListener()
    instance.calls = calls  # type: ignore[attr-defined]
    return instance


async def test_trigger_word_is_deferred_only_where_the_webhook_fires(listener):
    message = "@sprintflow-assistant read page 7"
    assert await listener._should_handle("O", message, "", "chan-hooked") is False
    assert await listener._should_handle("O", message, "", "chan-unhooked") is True
    # Another integration's hook in a channel does not make it ours to skip.
    assert await listener._should_handle("O", message, "", "chan-other-integration") is True
    # Coverage is read once and cached.
    assert listener.calls["count"] == 1  # type: ignore[attr-defined]


async def test_mentions_mid_sentence_and_direct_messages_are_always_ours(listener):
    assert await listener._should_handle("O", "thanks @sprintflow-assistant, one more", "", "chan-hooked") is True
    assert await listener._should_handle("D", "@sprintflow-assistant hi", "", "dm") is True
    assert await listener._should_handle("O", "just chatting", "", "chan-hooked") is False


async def test_a_group_message_is_answered_only_when_the_bot_is_addressed(listener):
    """A group message has other people in it; the bot is not part of every line."""
    assert await listener._should_handle("G", "so shall we ship on Tuesday?", "", "gm-1") is False
    assert await listener._should_handle("G", "@sprintflow-assistant what did we agree?", "", "gm-1") is True


async def test_a_private_channel_mention_is_answered_by_this_listener(listener):
    """No outgoing webhook fires in a private channel, so nobody else will answer it."""
    assert await listener._should_handle("P", "@sprintflow-assistant summarise the above", "", "chan-p") is True
    assert await listener._should_handle("P", "unrelated chatter", "", "chan-p") is False


async def test_unknown_coverage_keeps_deferring(monkeypatch):
    async def unavailable():
        return None

    monkeypatch.setattr(mattermost_ws.mattermost_client, "list_outgoing_webhooks", unavailable)
    monkeypatch.setattr(settings, "MATTERMOST_TRIGGER_WORDS", ["@sprintflow-assistant"])
    instance = MattermostWebSocketListener()
    assert await instance._should_handle("O", "@sprintflow-assistant hi", "", "anywhere") is False


async def test_a_team_wide_hook_covers_every_channel(monkeypatch):
    async def team_wide():
        return [{"channel_id": "", "callback_urls": ["http://ai-core:8000/api/v1/mattermost/webhook"]}]

    monkeypatch.setattr(mattermost_ws.mattermost_client, "list_outgoing_webhooks", team_wide)
    monkeypatch.setattr(settings, "MATTERMOST_TRIGGER_WORDS", ["@sprintflow-assistant"])
    instance = MattermostWebSocketListener()
    assert await instance._should_handle("O", "@sprintflow-assistant hi", "", "any-channel") is False
