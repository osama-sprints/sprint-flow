"""Publication: threading shape, reconciliation and the text fallback.

``_deliver`` is the one place a reply becomes a post. These tests drive it
with a recording stand-in for the Mattermost client, so they pin the three
threading branches, the rich→plain fallback, and the crash-window
reconciliation without a server.
"""

import pytest

from app.schemas.rich_media import (
    RICH_MEDIA_POST_TYPE,
    Artifact,
    ArtifactKind,
    MermaidContent,
    ReplyEnvelope,
)
from app.services import conversation
from app.services import rich_media


class RecordingClient:
    """Records every publish call and answers with a canned post, or None."""

    def __init__(self, fail_first_rich: bool = False) -> None:
        self.calls: list[dict] = []
        self.fail_first_rich = fail_first_rich

    async def create_post(self, channel_id, message, root_id=None, *, post_type=None, props=None, file_ids=None):
        self.calls.append(
            {
                "via": "create_post",
                "channel_id": channel_id,
                "root_id": root_id,
                "post_type": post_type,
                "props": props,
                "file_ids": file_ids,
            }
        )
        if self.fail_first_rich and post_type:
            self.fail_first_rich = False
            return None
        return {"id": f"post-{len(self.calls)}", "channel_id": channel_id}

    async def reply_to_post(self, channel_id, message, trigger_post_id, *, post_type=None, props=None, file_ids=None):
        self.calls.append(
            {
                "via": "reply_to_post",
                "channel_id": channel_id,
                "trigger": trigger_post_id,
                "post_type": post_type,
                "props": props,
                "file_ids": file_ids,
            }
        )
        return {"id": f"post-{len(self.calls)}", "channel_id": channel_id}


def _envelope(turn_id: str = "turn-1") -> ReplyEnvelope:
    artifact = Artifact(
        id="a1",
        kind=ArtifactKind.MERMAID,
        content=MermaidContent(definition="graph TD; A-->B;"),
        turn_id=turn_id,
        channel_id="c1",
    )
    return ReplyEnvelope(turn_id=turn_id, artifacts=[artifact])


@pytest.fixture
def client(monkeypatch):
    """Swap the Mattermost client and neutralise the durable-store calls."""
    recorder = RecordingClient()
    monkeypatch.setattr(conversation, "mattermost_client", recorder)

    async def _not_published(turn_id):
        return None

    recorded: list[tuple] = []

    async def _record(turn_id, post_id, file_ids=None):
        recorded.append((turn_id, post_id))

    monkeypatch.setattr(rich_media, "already_published", _not_published)
    monkeypatch.setattr(rich_media, "record_publication", _record)
    recorder.recorded = recorded  # type: ignore[attr-defined]
    return recorder


def _message(**overrides):
    base = dict(
        channel_id="c1",
        post_id="trigger-1",
        text="hi",
        user_id="u1",
        user_name="u",
        channel_type="O",
        root_id="",
        source="test",
    )
    base.update(overrides)
    return conversation.IncomingMessage(**base)


async def test_public_channel_reply_opens_a_thread_under_the_trigger(client):
    """Branch 2: a fresh channel message is answered in a thread rooted at it."""
    await conversation._deliver(_message(channel_type="O"), "answer", _envelope())
    assert client.calls[0]["via"] == "reply_to_post"
    assert client.calls[0]["trigger"] == "trigger-1"
    assert client.calls[0]["post_type"] == RICH_MEDIA_POST_TYPE


async def test_private_channel_behaves_like_public(client):
    """Private channels thread too; only DMs and GMs stay flat."""
    await conversation._deliver(_message(channel_type="P"), "answer", _envelope())
    assert client.calls[0]["via"] == "reply_to_post"


async def test_direct_message_reply_is_flat_but_still_rich(client):
    """Branch 3: a DM reply is not threaded, and the artifacts still travel."""
    await conversation._deliver(_message(channel_type="D"), "answer", _envelope())
    call = client.calls[0]
    assert call["via"] == "create_post" and call["root_id"] is None
    assert call["post_type"] == RICH_MEDIA_POST_TYPE
    assert call["props"]["sf_artifacts"][0]["id"] == "a1"


async def test_existing_thread_is_respected_whatever_the_channel_type(client):
    """Branch 1: inside a thread the reply stays in that thread, even in a DM."""
    await conversation._deliver(_message(channel_type="D", root_id="root-9"), "answer", _envelope())
    call = client.calls[0]
    assert call["via"] == "create_post" and call["root_id"] == "root-9"


async def test_props_carry_turn_id_and_publication_is_recorded(client):
    """The turn id in props is what a later reconciliation looks for."""
    await conversation._deliver(_message(), "answer", _envelope("turn-42"))
    assert client.calls[0]["props"]["sf_turn_id"] == "turn-42"
    assert client.recorded == [("turn-42", "post-1")]


async def test_rejected_rich_post_falls_back_to_plain_text(monkeypatch):
    """Decoration must never cost the person their answer."""
    recorder = RecordingClient(fail_first_rich=True)
    monkeypatch.setattr(conversation, "mattermost_client", recorder)

    async def _none(turn_id):
        return None

    async def _noop(*a, **k):
        return None

    monkeypatch.setattr(rich_media, "already_published", _none)
    monkeypatch.setattr(rich_media, "record_publication", _noop)

    await conversation._deliver(_message(channel_type="D"), "answer", _envelope())
    assert [c["post_type"] for c in recorder.calls] == [RICH_MEDIA_POST_TYPE, None]


async def test_already_published_turn_is_not_posted_again(monkeypatch):
    """Reconciliation: a retry after the crash window adopts the existing post."""
    recorder = RecordingClient()
    monkeypatch.setattr(conversation, "mattermost_client", recorder)

    async def _found(turn_id):
        return "post-existing"

    monkeypatch.setattr(rich_media, "already_published", _found)

    await conversation._deliver(_message(), "answer", _envelope())
    assert recorder.calls == []


async def test_plain_reply_has_no_custom_type(client):
    """No artifacts → an ordinary post, nothing to reconcile or record."""
    await conversation._deliver(_message(channel_type="D"), "answer", ReplyEnvelope())
    call = client.calls[0]
    assert call["post_type"] is None and call["props"] is None
    assert client.recorded == []
