"""Guards around tool execution and interrupt payloads in the graph (review fixes, 2026-09-03).

Pure unit tests: fake tools, no graph run, no network, no database.
"""

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from langgraph.errors import GraphInterrupt
from pydantic import BaseModel

from app.core.langgraph.graph import (
    _invoke_guarded,
    interrupt_question,
    pending_interrupt,
    pending_interrupt_value,
    resume_value,
)
from app.core.langgraph.tools.results import guarded_tool


class _Args(BaseModel):
    ceremony_id: int


class _FakeTool:
    """Stands in for a LangChain tool: ``ainvoke`` validates like the real one would."""

    name = "amend_ceremony"

    def __init__(self, behaviour: str) -> None:
        self.behaviour = behaviour

    async def ainvoke(self, args: dict[str, Any]) -> str:
        if self.behaviour == "validate":
            _Args.model_validate(args)  # raises pydantic.ValidationError for bad input
            return "[CEREMONY_AMENDED] ok"
        if self.behaviour == "crash":
            raise RuntimeError("database exploded")
        if self.behaviour == "interrupt":
            raise GraphInterrupt(())
        return "[OK] fine"


def _run(coro):
    return asyncio.run(coro)


def test_invalid_model_arguments_become_a_validation_error_tool_message():
    result = _run(
        _invoke_guarded(_FakeTool("validate"), {"name": "amend_ceremony", "args": {"ceremony_id": "the retro"}})
    )  # type: ignore[arg-type]
    assert result.startswith("[VALIDATION_ERROR]")
    assert "ceremony_id" in result
    assert "nothing was done" in result


def test_valid_arguments_pass_through():
    result = _run(_invoke_guarded(_FakeTool("validate"), {"name": "amend_ceremony", "args": {"ceremony_id": 7}}))  # type: ignore[arg-type]
    assert result == "[CEREMONY_AMENDED] ok"


def test_unexpected_exception_becomes_a_readable_system_error():
    result = _run(_invoke_guarded(_FakeTool("crash"), {"name": "amend_ceremony", "args": {}}))  # type: ignore[arg-type]
    assert result.startswith("[SYSTEM_ERROR]")
    assert "database exploded" not in result  # no internals leak to the person


def test_graph_interrupt_is_never_swallowed_by_the_executor_guard():
    with pytest.raises(GraphInterrupt):
        _run(_invoke_guarded(_FakeTool("interrupt"), {"name": "amend_ceremony", "args": {}}))  # type: ignore[arg-type]


def test_guarded_tool_lets_cancellation_propagate():
    @guarded_tool
    async def body() -> str:
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        _run(body())


def test_guarded_tool_still_maps_ordinary_errors():
    @guarded_tool
    async def body() -> str:
        raise RuntimeError("boom")

    assert _run(body()).startswith("[SYSTEM_ERROR]")


def test_structured_interrupt_shows_only_the_question_and_is_echoed_on_resume():
    payload = {
        "question": "Schedule the retro on Friday 15:00 (Europe/Berlin) — 13:00 UTC?",
        "scheduled_at": "2026-09-04T13:00:00+00:00",
    }
    assert interrupt_question(payload) == payload["question"]
    assert interrupt_question("plain question") == "plain question"
    assert resume_value("yes", payload) == {"reply": "yes", "interrupt": payload}
    assert resume_value("yes", "plain question") == "yes"


def test_pending_interrupt_reads_the_first_real_interrupt_only():
    snapshot = SimpleNamespace(
        next=("back_office_tools",),
        tasks=[
            SimpleNamespace(interrupts=()),
            SimpleNamespace(interrupts=[SimpleNamespace(value={"question": "confirm?", "scheduled_at": None})]),
        ],
    )
    assert pending_interrupt_value(snapshot) == {"question": "confirm?", "scheduled_at": None}  # type: ignore[arg-type]
    assert pending_interrupt(snapshot) == "confirm?"  # type: ignore[arg-type]
    failed_node = SimpleNamespace(next=("tool_call",), tasks=[SimpleNamespace(interrupts=())])
    assert pending_interrupt(failed_node) is None  # type: ignore[arg-type]
