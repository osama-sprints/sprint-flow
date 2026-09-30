"""Two confirming tool calls in one model turn are resolved one at a time (review fix, 2026-09-03).

Builds the real graph on ``MemorySaver`` with the fake LLM from ``test_specialists``.
The fake back-office group contains ``ask_human``, which interrupts; the scripted model
asks two questions in a single AI message. The person's first answer must resume the
FIRST question only; the second question is then asked on its own.
"""

import asyncio

from langchain_core.messages import AIMessage, ToolMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from app.core.langgraph.graph import pending_interrupt
from app.core.requester import RequesterContext
from tests.test_specialists import config_for, make_agent, run_turn, user_turn

AUTHORITY = RequesterContext(mattermost_user_id="mm-sm", username="sm", user_id=5, channel_roles={1: "scrum_master"})


def test_two_confirmations_in_one_turn_resume_in_order():
    two_questions = AIMessage(
        content="",
        tool_calls=[
            {"name": "ask_human", "args": {"question": "Book the retro on Friday?"}, "id": "first"},
            {"name": "ask_human", "args": {"question": "And the standup on Monday?"}, "id": "second"},
        ],
    )
    fake, graph = make_agent([two_questions, AIMessage(content="Both handled.")], MemorySaver())
    config = config_for("seq-confirm")

    async def scenario() -> None:
        # Turn 1: the model asks two questions; the graph pauses on the first.
        await run_turn(graph, user_turn("open sprint 2 and book the retro and the standup"), config, AUTHORITY)
        state = await graph.aget_state(config)
        assert state.next, "graph should be paused"
        assert pending_interrupt(state) == "Book the retro on Friday?"

        # Answer 1 resumes the first question; the second is asked next, not skipped.
        await run_turn(graph, Command(resume="yes"), config, AUTHORITY)
        state = await graph.aget_state(config)
        # LangGraph leaves ``next`` empty when a new interrupt is raised during a
        # resume; the saved task still carries it, which is what the graph reads.
        assert pending_interrupt(state) == "And the standup on Monday?"

        # Answer 2 completes the turn: both tool messages exist, in call order, with the right answers.
        await run_turn(graph, Command(resume="no"), config, AUTHORITY)
        state = await graph.aget_state(config)
        # `next` is empty both when the turn finished and when a resume raised a
        # new interrupt, so assert on the interrupt itself.
        assert pending_interrupt(state) is None, "turn should be finished, not paused again"
        tool_messages = [m for m in state.values["messages"] if isinstance(m, ToolMessage)]
        assert [m.tool_call_id for m in tool_messages] == ["first", "second"]
        assert [m.content for m in tool_messages] == ["yes", "no"]
        assert state.values["messages"][-1].content == "Both handled."

    asyncio.run(scenario())
