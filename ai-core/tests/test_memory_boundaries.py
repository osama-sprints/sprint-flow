"""What long-term memory may learn, and where it may be repeated.

Memory is a record of the person the assistant is talking to. Two things can
break that: a turn that read a discussion can file other people's statements
under the requester's name, and a memory formed in a direct message can be
repeated inside a channel answer where everybody reads it.

These tests pin both boundaries. They use the same fake Mattermost as
``test_discussion.py`` so a turn can genuinely read somebody else's message.
"""

from typing import (
    Any,
    List,
)

from langchain_core.messages import AIMessage

from app.core.requester import (
    RequesterContext,
    current_requester,
)
from app.services import discussion
from app.services.memory import (
    may_surface,
    with_provenance,
)
from app.utils import memory_messages
from tests.test_discussion import (
    CHANNEL,
    PRIVATE,
    TRIGGER,
    _world,
)


class ScriptedLLM:
    """An LLM that replies from a script."""

    def __init__(self, script: List[AIMessage]) -> None:
        """Hold the replies to give, in order."""
        self.script = list(script)
        self.seen: List[List[Any]] = []

    async def call(self, messages, model_name=None, *, tools=None, tool_choice=None, **kwargs):
        """Record the call and return the next scripted reply."""
        self.seen.append(list(messages))
        return self.script.pop(0) if self.script else AIMessage(content="")


def _read() -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": "read_channel_messages", "args": {}, "id": "call-read"}])


def _report(findings: str, sources: List[str]) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": "report_findings",
                "args": {"findings": findings, "sources": sources, "coverage": "read 1 message"},
                "id": "call-report",
            }
        ],
    )


async def test_other_peoples_statements_are_not_learned_as_the_requesters(monkeypatch):
    """A turn that read a discussion teaches memory only what the person wrote."""
    world = _world(monkeypatch)
    world.post("post-a", user_id="u-sara", message="I am leaving the team", create_at=1000)
    world.post(TRIGGER, user_id="u-asker", message="what did sara say?", create_at=2000)
    monkeypatch.setattr(
        "app.services.llm.llm_service", ScriptedLLM([_read(), _report("Sara said she is leaving.", ["post-a"])])
    )
    await discussion.investigate("what did sara say?")

    learnable = memory_messages(
        [
            {"role": "user", "content": "what did sara say?"},
            {"role": "assistant", "content": "Sara said she is leaving the team."},
            {"role": "tool", "content": "[DISCUSSION_READ] Sara said she is leaving the team."},
        ],
        own_words_only=discussion.read_other_people(),
    )

    assert discussion.read_other_people() is True
    assert learnable == [{"role": "user", "content": "what did sara say?"}]


async def test_reading_only_the_persons_own_earlier_messages_is_not_contamination(monkeypatch):
    """Retrieval that returned nobody else's words leaves memory alone."""
    world = _world(monkeypatch)
    world.post("post-a", user_id="u-asker", message="I said this myself earlier", create_at=1000)
    world.post(TRIGGER, user_id="u-asker", message="what did I say?", create_at=2000)
    monkeypatch.setattr("app.services.llm.llm_service", ScriptedLLM([_read(), _report("You said it.", ["post-a"])]))
    await discussion.investigate("what did I say?")

    assert discussion.read_other_people() is False


async def test_a_turn_that_read_nobody_else_still_learns_the_reply(monkeypatch):
    """The rule is targeted: an ordinary turn is unaffected, minus tool traffic."""
    _world(monkeypatch)

    learnable = memory_messages(
        [
            {"role": "user", "content": "I prefer mornings"},
            {"role": "assistant", "content": "Noted.", "tool_calls": [{"id": "1"}]},
            {"role": "assistant", "content": "Noted."},
            {"role": "tool", "content": "[OK] done"},
        ],
        own_words_only=discussion.read_other_people(),
    )

    assert discussion.read_other_people() is False
    assert learnable == [
        {"role": "user", "content": "I prefer mornings"},
        {"role": "assistant", "content": "Noted."},
    ]


def test_a_private_memory_is_withheld_from_a_public_reply():
    """What someone said in a DM does not surface in a channel answer."""
    current_requester.set(
        RequesterContext(mattermost_user_id="u-asker", channel_id="dm-1", channel_type="D", username="asker")
    )
    try:
        stored = with_provenance({"session_id": "dm-1"})
    finally:
        current_requester.set(None)

    assert stored["channel_type"] == "D"
    assert may_surface(stored, "D", "dm-1")
    assert not may_surface(stored, "O", CHANNEL)
    assert not may_surface(stored, "P", PRIVATE)


def test_a_memory_formed_in_public_may_be_repeated_anywhere():
    """Public content is already readable by everyone the reply reaches."""
    public = {"channel_type": "O", "channel_id": CHANNEL}

    assert may_surface(public, "O", "chan-other")
    assert may_surface(public, "P", PRIVATE)
    assert may_surface(public, "D", "dm-1")


def test_a_memory_stays_inside_the_private_channel_it_came_from():
    """The same private channel is fine; a different one is not."""
    private = {"channel_type": "P", "channel_id": PRIVATE}

    assert may_surface(private, "P", PRIVATE)
    assert not may_surface(private, "P", "chan-other-private")
    assert not may_surface(private, "O", CHANNEL)


def test_a_memory_with_no_recorded_origin_surfaces_only_in_a_direct_message():
    """Memories written before provenance existed are treated as private."""
    assert may_surface({}, "D", "dm-1")
    assert not may_surface({}, "O", CHANNEL)
    assert not may_surface(None, "P", PRIVATE)
    # Outside a turn nothing is being posted anywhere, so nothing is disclosed.
    assert may_surface({}, "", "")
