"""Orchestration on the real Postgres checkpointer (skipped unless SPRINTFLOW_INTEGRATION_DB=1).

``MemorySaver`` keeps Python objects; ``AsyncPostgresSaver`` serialises them.
These tests prove that the routing fields round-trip through the real
checkpointer, that an old-shaped checkpoint (messages + long_term_memory only)
still loads and routes, and that a pre-supervisor conversation paused inside
``tool_call`` resumes there — all against a live database.

Run from ``ai-core/`` with the throwaway database environment set:

    SPRINTFLOW_INTEGRATION_DB=1 APP_ENV=test .venv/bin/python -m pytest -q tests/integration
"""

import asyncio
import os
import uuid
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
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.graph import (
    END,
    StateGraph,
)
from langgraph.graph.message import add_messages
from langgraph.types import Command
from pydantic import (
    BaseModel,
    Field,
)

from app.core.config import settings
from app.core.langgraph.graph import (
    LangGraphAgent,
    pending_interrupt,
)
from app.core.langgraph.routing_examples import REQUESTERS
from app.core.langgraph.tools.ask_human import ask_human
from app.core.requester import current_requester
from app.schemas.graph import CapabilityRoute

pytestmark = pytest.mark.skipif(
    not os.getenv("SPRINTFLOW_INTEGRATION_DB"), reason="needs SPRINTFLOW_INTEGRATION_DB=1 and a Postgres"
)

DSN = (
    f"postgresql://{settings.POSTGRES_USER}:{settings.POSTGRES_PASSWORD}"
    f"@{settings.POSTGRES_HOST}:{settings.POSTGRES_PORT}/{settings.POSTGRES_DB}"
)


class FakeLLMService:
    """Scripted stand-in for the LLM service."""

    def __init__(self, script: Sequence[AIMessage]):
        self.script = list(script)
        self.calls: list[list[str]] = []

    def bind_tools(self, tools: Any) -> "FakeLLMService":
        return self

    def get_llm(self) -> Any:
        return None

    async def call(self, messages: Any, tools: Optional[Sequence[Any]] = None, **_: Any) -> AIMessage:
        self.calls.append(sorted(t.name for t in (tools or [])))
        return self.script.pop(0) if self.script else AIMessage(content="ok")


executed: list[str] = []


@tool
async def create_channel(name: str) -> str:
    """Create a channel (fake)."""
    executed.append(name)
    return f"[COHORT_CREATED] {name}"


TOOL_GROUPS = {"general": [ask_human], "learner_support": [ask_human], "back_office": [create_channel, ask_human]}


class LegacyState(BaseModel):
    messages: Annotated[list, add_messages] = Field(default_factory=list)
    long_term_memory: str = Field(default="")


def build_legacy_graph(fake: FakeLLMService, saver: AsyncPostgresSaver):
    async def chat(state: LegacyState) -> Command:
        response = await fake.call([{"role": "system", "content": "legacy"}])
        return Command(update={"messages": [response]}, goto="tool_call" if response.tool_calls else END)

    async def tool_call(state: LegacyState) -> Command:
        outputs = []
        for call in state.messages[-1].tool_calls:
            content = await ask_human.ainvoke(call["args"])
            outputs.append(ToolMessage(content=content, name=call["name"], tool_call_id=call["id"]))
        return Command(update={"messages": outputs}, goto="chat")

    builder = StateGraph(LegacyState)
    builder.add_node("chat", chat, destinations=("tool_call", END))
    builder.add_node("tool_call", tool_call, destinations=("chat",))
    builder.set_entry_point("chat")
    return builder.compile(checkpointer=saver)


async def run(graph, payload, config, requester) -> list[str]:
    visited: list[str] = []
    token = current_requester.set(requester)
    try:
        async for update in graph.astream(payload, config, stream_mode="updates"):
            visited.extend(update.keys())
    finally:
        current_requester.reset(token)
    return visited


def config_for() -> dict:
    return {"configurable": {"thread_id": f"it-{uuid.uuid4()}"}, "metadata": {"username": "it"}}


def test_routing_fields_round_trip_through_postgres():
    async def scenario():
        async with AsyncPostgresSaver.from_conn_string(DSN) as saver:
            await saver.setup()
            config = config_for()
            fake = FakeLLMService([AIMessage(content="Sprint 2 is open."), AIMessage(content="Open, retro Friday.")])
            graph = LangGraphAgent(llm=fake, tool_groups=TOOL_GROUPS).build_graph(saver)
            visited = await run(
                graph,
                {"messages": [HumanMessage(content="open sprint 2 for Backend-01 and tell me when the retro is")]},
                config,
                REQUESTERS["authority"],
            )
            state = await graph.aget_state(config)
            return visited, state, fake.calls

    visited, state, calls = asyncio.run(scenario())
    assert visited == ["supervisor", "back_office", "learner_support"]
    assert calls == [["ask_human", "create_channel"], ["ask_human"]]
    assert state.values["route"] == CapabilityRoute.LEARNER_SUPPORT.value
    assert state.values["route_plan"] == []
    assert state.values["is_multi_intent"] is True
    assert state.values["matched_rule"] == "back_office_sprint"
    assert state.values["messages"][-1].content == "Open, retro Friday."


def test_old_shaped_postgres_checkpoint_loads_and_routes():
    async def scenario():
        async with AsyncPostgresSaver.from_conn_string(DSN) as saver:
            await saver.setup()
            config = config_for()
            legacy = build_legacy_graph(FakeLLMService([AIMessage(content="old reply")]), saver)
            await legacy.ainvoke({"messages": [HumanMessage(content="hi")], "long_term_memory": ""}, config)

            fake = FakeLLMService([AIMessage(content="Tomorrow 09:00.")])
            graph = LangGraphAgent(llm=fake, tool_groups=TOOL_GROUPS).build_graph(saver)
            before = await graph.aget_state(config)
            visited = await run(
                graph, {"messages": [HumanMessage(content="when is the next standup?")]}, config, REQUESTERS["learner"]
            )
            return set(before.values.keys()), visited, await graph.aget_state(config)

    before_keys, visited, state = asyncio.run(scenario())
    assert before_keys <= {"messages", "long_term_memory"}
    assert visited == ["supervisor", "learner_support"]
    assert state.values["route"] == CapabilityRoute.LEARNER_SUPPORT.value
    assert len(state.values["messages"]) == 4


def test_legacy_tool_call_interrupt_resumes_in_place_through_postgres():
    async def scenario():
        async with AsyncPostgresSaver.from_conn_string(DSN) as saver:
            await saver.setup()
            config = config_for()
            legacy_fake = FakeLLMService(
                [
                    AIMessage(
                        content="", tool_calls=[{"name": "ask_human", "args": {"question": "Go ahead?"}, "id": "q"}]
                    )
                ]
            )
            legacy = build_legacy_graph(legacy_fake, saver)
            await legacy.ainvoke({"messages": [HumanMessage(content="add bob to team Growth")]}, config)
            paused = await legacy.aget_state(config)

            fake = FakeLLMService([AIMessage(content="Done.")])
            graph = LangGraphAgent(llm=fake, tool_groups=TOOL_GROUPS).build_graph(saver)
            visited = await run(graph, Command(resume="yes"), config, REQUESTERS["admin"])
            return paused, visited, await graph.aget_state(config)

    paused, visited, state = asyncio.run(scenario())
    assert paused.next == ("tool_call",) and pending_interrupt(paused) == "Go ahead?"
    assert visited == ["tool_call", "chat"]
    assert state.values.get("route") is None
    assert state.values["messages"][-1].content == "Done."
    assert state.next == ()
    assert executed == []
