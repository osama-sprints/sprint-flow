"""Graph-level tests for specialist isolation, multi-step turns, old checkpoints and resume.

The real graph is built with ``LangGraphAgent.build_graph(MemorySaver())``, a
scripted fake LLM service and recording fake tools — no Postgres, no network.
Node names visited are read from ``astream(stream_mode="updates")`` so each
test asserts the path a turn actually took, not only its output.
"""

import asyncio
from typing import (
    Annotated,
    Any,
    Optional,
    Sequence,
)

import pytest
from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    ToolMessage,
)
from langchain_core.tools import tool
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import (
    END,
    StateGraph,
)
from langgraph.graph.message import add_messages
from langgraph.types import Command
from prometheus_client import Counter
from pydantic import (
    BaseModel,
    Field,
)

from app.core.langgraph.graph import (
    LangGraphAgent,
    pending_interrupt,
    replies_since_last_human,
)
from app.core.langgraph.routing_examples import REQUESTERS
from app.core.langgraph.specialists import SPECIALISTS
from app.core.langgraph.tools.ask_human import ask_human
from app.core.metrics import routing_decisions_total
from app.core.requester import current_requester
from app.schemas.graph import CapabilityRoute

EXPECTED_NODES = {
    "supervisor",
    "chat",
    "tool_call",
    "learner_support",
    "learner_support_tools",
    "back_office",
    "back_office_tools",
}


# --- Fakes ---------------------------------------------------------------------


class FakeLLMService:
    """Scripted stand-in for ``llm_service``: records every call and its bound tools."""

    def __init__(self, script: Sequence[AIMessage]):
        self.script = list(script)
        self.calls: list[dict[str, Any]] = []

    def bind_tools(self, tools: Any) -> "FakeLLMService":
        return self

    def get_llm(self) -> Any:
        return None

    async def call(self, messages: Any, tools: Optional[Sequence[Any]] = None, **_: Any) -> AIMessage:
        self.calls.append(
            {
                "tools": sorted(t.name for t in (tools or [])),
                "system": messages[0]["content"],
                "message_count": len(messages),
            }
        )
        if not self.script:
            return AIMessage(content="ok")
        return self.script.pop(0)


class Recorder:
    """Counts executions of the fake mutating tool."""

    def __init__(self):
        self.calls: list[str] = []


recorder = Recorder()


@tool
async def create_channel(name: str) -> str:
    """Create a channel (fake, records the call)."""
    recorder.calls.append(name)
    return f"[COHORT_CREATED] Channel '{name}' created."


@tool
async def list_ceremonies(channel: str) -> str:
    """List a channel's ceremonies (fake)."""
    return f"[OK] {channel}: retrospective on Friday 15:00 UTC."


@tool
async def duckduckgo_search(query: str) -> str:
    """Search the web (fake)."""
    return f"[OK] results for {query}"


TOOL_GROUPS = {
    "general": [ask_human, duckduckgo_search],
    "learner_support": [list_ceremonies, ask_human],
    "back_office": [create_channel, ask_human],
}


def tool_call_message(name: str, args: dict, call_id: str) -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": call_id}])


def counter_total(metric: Counter) -> float:
    return sum(s.value for m in metric.collect() for s in m.samples if s.name.endswith("_total"))


def make_agent(script: Sequence[AIMessage], saver: Optional[MemorySaver] = None):
    fake = FakeLLMService(script)
    agent = LangGraphAgent(llm=fake, tool_groups=TOOL_GROUPS)
    graph = agent.build_graph(saver or MemorySaver())
    return fake, graph


def config_for(thread_id: str) -> dict:
    return {"configurable": {"thread_id": thread_id}, "metadata": {"username": "tester"}}


async def run_turn(graph, payload, config, requester):
    """Run one turn and return (visited node names in order, final state)."""
    visited: list[str] = []
    token = current_requester.set(requester)
    try:
        async for update in graph.astream(payload, config, stream_mode="updates"):
            visited.extend(update.keys())
    finally:
        current_requester.reset(token)
    return visited, await graph.aget_state(config)


def user_turn(text: str) -> dict:
    return {"messages": [{"role": "user", "content": text}], "long_term_memory": "none"}


# --- Legacy (pre-Sprint-1) graph, for checkpoint compatibility --------------------


class LegacyState(BaseModel):
    """The GraphState shape before the supervisor existed."""

    messages: Annotated[list, add_messages] = Field(default_factory=list)
    long_term_memory: str = Field(default="")


def build_legacy_graph(fake: FakeLLMService, saver: MemorySaver):
    """chat -> (tool_call | END); tool_call -> chat; entry chat. All tools bound."""
    all_tools = {t.name: t for group in TOOL_GROUPS.values() for t in group}

    async def chat(state: LegacyState) -> Command:
        response = await fake.call(
            [{"role": "system", "content": "legacy"}] + [m.model_dump() for m in state.messages]
        )
        goto = "tool_call" if response.tool_calls else END
        return Command(update={"messages": [response]}, goto=goto)

    async def tool_call(state: LegacyState) -> Command:
        outputs = []
        for call in state.messages[-1].tool_calls:
            content = await all_tools[call["name"]].ainvoke(call["args"])
            outputs.append(ToolMessage(content=content, name=call["name"], tool_call_id=call["id"]))
        return Command(update={"messages": outputs}, goto="chat")

    builder = StateGraph(LegacyState)
    builder.add_node("chat", chat, destinations=("tool_call", END))
    builder.add_node("tool_call", tool_call, destinations=("chat",))
    builder.set_entry_point("chat")
    return builder.compile(checkpointer=saver)


# --- Tests ---------------------------------------------------------------------


def test_compiled_graph_has_the_contracted_node_names():
    _, graph = make_agent([])
    assert EXPECTED_NODES <= set(graph.nodes.keys())
    assert {spec.node_name for spec in SPECIALISTS.values()} == {"learner_support", "back_office", "chat", "policy_support"}
    assert {spec.tools_node_name for spec in SPECIALISTS.values()} == {
        "learner_support_tools",
        "back_office_tools",
        "policy_support_tools",
        "tool_call",
    }


def test_learner_route_never_executes_a_back_office_tool():
    """(a) The fake LLM emits create_channel on a learner turn; nothing runs."""
    recorder.calls.clear()
    fake, graph = make_agent(
        [
            tool_call_message("create_channel", {"name": "Rogue-01"}, "call-1"),
            AIMessage(content="I can't create channels; please ask your tech lead or scrum master."),
        ]
    )
    visited, state = asyncio.run(run_turn(graph, user_turn("create a channel"), config_for("a"), REQUESTERS["learner"]))

    assert visited == ["supervisor", "learner_support", "learner_support_tools", "learner_support"]
    assert "back_office_tools" not in visited
    assert recorder.calls == [], "the back-office tool executed on a learner route"
    # The model could only see the learner group when it hallucinated the call.
    assert fake.calls[0]["tools"] == ["ask_human", "list_ceremonies"]
    tool_messages = [m for m in state.values["messages"] if isinstance(m, ToolMessage)]
    assert len(tool_messages) == 1
    assert tool_messages[0].name == "create_channel"
    assert tool_messages[0].content.startswith("[VALIDATION_ERROR]")
    assert "not available here" in tool_messages[0].content
    assert state.values["route"] == CapabilityRoute.LEARNER_SUPPORT.value
    assert state.values["matched_rule"] == "back_office_channel_denied_role"
    assert isinstance(state.values["messages"][-1], AIMessage)
    assert state.next == ()


def test_general_route_cannot_reach_back_office_tools_either():
    recorder.calls.clear()
    fake, graph = make_agent([tool_call_message("create_channel", {"name": "X"}, "c"), AIMessage(content="no")])
    visited, state = asyncio.run(run_turn(graph, user_turn("hello there"), config_for("g"), REQUESTERS["admin"]))
    assert visited == ["supervisor", "chat", "tool_call", "chat"]
    assert recorder.calls == []
    assert fake.calls[0]["tools"] == ["ask_human", "duckduckgo_search"]
    assert [m for m in state.values["messages"] if isinstance(m, ToolMessage)][0].content.startswith(
        "[VALIDATION_ERROR]"
    )


def test_back_office_route_executes_its_own_tool():
    recorder.calls.clear()
    fake, graph = make_agent(
        [tool_call_message("create_channel", {"name": "Growth-01"}, "c1"), AIMessage(content="Done.")]
    )
    visited, state = asyncio.run(
        run_turn(graph, user_turn("create channel Growth-01"), config_for("b"), REQUESTERS["admin"])
    )
    assert visited == ["supervisor", "back_office", "back_office_tools", "back_office"]
    assert recorder.calls == ["Growth-01"]
    assert fake.calls[0]["tools"] == ["ask_human", "create_channel"]
    assert "[COHORT_CREATED]" in [m for m in state.values["messages"] if isinstance(m, ToolMessage)][0].content


def test_multi_intent_runs_back_office_then_learner_support_with_one_final_reply():
    """(b) Two specialists, in plan order, each with only its own tools; one composed answer last."""
    recorder.calls.clear()
    fake, graph = make_agent(
        [
            AIMessage(content="Sprint 2 is now open for Backend-01."),
            tool_call_message("list_ceremonies", {"channel": "Backend-01"}, "l1"),
            AIMessage(content="Sprint 2 is open for Backend-01, and the retro is on Friday at 15:00 UTC."),
        ]
    )
    visited, state = asyncio.run(
        run_turn(
            graph,
            user_turn("open sprint 2 for Backend-01 and tell me when the retro is"),
            config_for("m"),
            REQUESTERS["authority"],
        )
    )

    assert visited == [
        "supervisor",
        "back_office",
        "learner_support",
        "learner_support_tools",
        "learner_support",
    ]
    assert [c["tools"] for c in fake.calls] == [
        ["ask_human", "create_channel"],
        ["ask_human", "list_ceremonies"],
        ["ask_human", "list_ceremonies"],
    ]
    assert "the following will handle the rest: learner_support" in fake.calls[0]["system"]
    assert "Earlier parts of this same request were already handled" not in fake.calls[0]["system"]
    assert "Earlier parts of this same request were already handled" in fake.calls[1]["system"]
    assert "ONE final reply" in fake.calls[2]["system"]

    messages = state.values["messages"]
    assert isinstance(messages[-1], AIMessage) and not messages[-1].tool_calls
    assert messages[-1].content.startswith("Sprint 2 is open for Backend-01, and the retro")
    replies = [m for m in messages if isinstance(m, AIMessage) and not m.tool_calls]
    assert len(replies) == 2, "one reply per specialist, the last one composed"
    assert state.values["route"] == CapabilityRoute.LEARNER_SUPPORT.value
    assert state.values["route_plan"] == []
    assert state.values["is_multi_intent"] is True
    assert state.next == ()


def test_old_shaped_checkpoint_loads_and_routes():
    """(c) A thread whose checkpoint holds only messages + long_term_memory keeps working."""
    saver = MemorySaver()
    config = config_for("legacy-thread")
    legacy_fake = FakeLLMService([AIMessage(content="Hello from before the supervisor.")])
    legacy = build_legacy_graph(legacy_fake, saver)
    asyncio.run(legacy.ainvoke({"messages": [HumanMessage(content="hi")], "long_term_memory": ""}, config))

    fake, graph = make_agent([AIMessage(content="The next standup is tomorrow at 09:00.")], saver)
    before = asyncio.run(graph.aget_state(config))
    assert set(before.values.keys()) <= {"messages", "long_term_memory"}, "fixture must be old-shaped"
    assert before.next == ()

    visited, state = asyncio.run(
        run_turn(graph, user_turn("when is the next standup?"), config, REQUESTERS["learner"])
    )
    assert visited == ["supervisor", "learner_support"]
    assert state.values["route"] == CapabilityRoute.LEARNER_SUPPORT.value
    assert state.values["matched_rule"] == "learner_calendar"
    assert [type(m).__name__ for m in state.values["messages"]] == [
        "HumanMessage",
        "AIMessage",
        "HumanMessage",
        "AIMessage",
    ]
    assert fake.calls[0]["message_count"] == 4  # system + the full old history


def test_legacy_interrupt_in_tool_call_resumes_without_rerunning_supervisor():
    """(d) A pre-supervisor checkpoint paused inside ``tool_call`` resumes there."""
    saver = MemorySaver()
    config = config_for("legacy-paused")
    legacy_fake = FakeLLMService([tool_call_message("ask_human", {"question": "Send the welcome message?"}, "q1")])
    legacy = build_legacy_graph(legacy_fake, saver)
    asyncio.run(
        legacy.ainvoke({"messages": [HumanMessage(content="add bob to team Growth")], "long_term_memory": ""}, config)
    )
    paused = asyncio.run(legacy.aget_state(config))
    assert paused.next == ("tool_call",)
    assert pending_interrupt(paused) == "Send the welcome message?"

    fake, graph = make_agent([AIMessage(content="Sent the welcome message to bob.")], saver)
    decisions_before = counter_total(routing_decisions_total)
    visited, state = asyncio.run(run_turn(graph, Command(resume="yes"), config, REQUESTERS["admin"]))

    assert visited == ["tool_call", "chat"]
    assert "supervisor" not in visited
    assert counter_total(routing_decisions_total) == decisions_before
    assert state.values.get("route") is None, "route untouched: the supervisor did not run"
    tool_messages = [m for m in state.values["messages"] if isinstance(m, ToolMessage)]
    assert tool_messages[-1].name == "ask_human" and tool_messages[-1].content == "yes"
    assert state.values["messages"][-1].content == "Sent the welcome message to bob."
    assert fake.calls[0]["tools"] == ["ask_human", "duckduckgo_search"]
    assert state.next == ()


def test_specialist_interrupt_resumes_in_its_own_tools_node():
    """(in-flight) A back-office confirmation resumes in back_office_tools, route unchanged."""
    saver = MemorySaver()
    config = config_for("paused-back-office")
    fake, graph = make_agent(
        [
            tool_call_message("ask_human", {"question": "Schedule the standup tomorrow 09:00 UTC?"}, "q1"),
            AIMessage(content="Scheduled the standup for tomorrow at 09:00 UTC."),
        ],
        saver,
    )
    visited, paused = asyncio.run(
        run_turn(
            graph,
            user_turn("schedule the standup for tomorrow at 9am for channel Backend-01"),
            config,
            REQUESTERS["authority"],
        )
    )
    assert visited[:2] == ["supervisor", "back_office"]
    assert paused.next == ("back_office_tools",)
    assert pending_interrupt(paused) == "Schedule the standup tomorrow 09:00 UTC?"
    assert paused.values["route"] == CapabilityRoute.BACK_OFFICE.value

    decisions_before = counter_total(routing_decisions_total)
    # The person's next message is "yes"; the conversation layer resumes rather than starting a turn.
    visited, state = asyncio.run(run_turn(graph, Command(resume="yes"), config, REQUESTERS["authority"]))
    assert visited == ["back_office_tools", "back_office"]
    assert counter_total(routing_decisions_total) == decisions_before
    assert state.values["route"] == CapabilityRoute.BACK_OFFICE.value
    assert state.values["messages"][-1].content.startswith("Scheduled the standup")
    assert state.next == ()


def test_stale_failed_node_is_not_an_interrupt():
    """A checkpoint whose ``next`` points at a node that raised carries no interrupt."""
    saver = MemorySaver()
    config = config_for("failed")

    class Boom(FakeLLMService):
        async def call(self, messages: Any, tools: Optional[Sequence[Any]] = None, **_: Any) -> AIMessage:
            raise RuntimeError("model down")

    agent = LangGraphAgent(llm=Boom([]), tool_groups=TOOL_GROUPS)
    graph = agent.build_graph(saver)
    token = current_requester.set(REQUESTERS["learner"])
    try:
        with pytest.raises(Exception, match="failed to get llm response"):
            asyncio.run(graph.ainvoke(user_turn("hello there"), config))
    finally:
        current_requester.reset(token)
    state = asyncio.run(graph.aget_state(config))
    assert state.next == ("chat",)
    assert pending_interrupt(state) is None, "a failed node must not be mistaken for a question to resume"


def test_unclear_message_ends_in_chat_with_a_reply():
    """(e) The fallback still answers."""
    fake, graph = make_agent([AIMessage(content="Sorry, I didn't catch that — what do you need?")])
    visited, state = asyncio.run(run_turn(graph, user_turn("asdfgh ???"), config_for("e"), REQUESTERS["learner"]))
    assert visited == ["supervisor", "chat"]
    assert state.values["route"] == CapabilityRoute.GENERAL.value
    assert state.values["matched_rule"] == "general_fallback"
    assert state.values["messages"][-1].content.startswith("Sorry, I didn't catch that")
    assert "# Routing" in fake.calls[0]["system"]


def test_replies_since_last_human_only_counts_text_replies_after_the_last_human_turn():
    messages = [
        HumanMessage(content="a"),
        AIMessage(content="old reply"),
        HumanMessage(content="b"),
        tool_call_message("x", {}, "1"),
        ToolMessage(content="r", name="x", tool_call_id="1"),
        AIMessage(content="step one done"),
    ]
    assert [m.content for m in replies_since_last_human(messages)] == ["step one done"]
    assert replies_since_last_human([HumanMessage(content="b")]) == []
