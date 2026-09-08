"""Tests for escalation exposure through the LangGraph learner-support path."""

from app.core.langgraph.specialists import SPECIALISTS
from app.core.langgraph.tools import (
    LEARNER_SUPPORT_TOOLS,
    TOOL_GROUPS,
)
from app.core.langgraph.tools.escalation import escalate_to_human

def test_escalation_tool_is_registered_for_learner_support():
    """The learner-support specialist must expose the escalation tool."""
    specialist = SPECIALISTS["learner_support"]

    assert specialist.tool_group == "learner_support"
    assert specialist.tool_group in TOOL_GROUPS
    assert escalate_to_human in LEARNER_SUPPORT_TOOLS
    assert escalate_to_human in TOOL_GROUPS["learner_support"]

def test_escalation_tool_schema_is_model_callable():
    """The graph-facing escalation tool exposes only the learner question."""
    assert escalate_to_human.name == "escalate_to_human"

    properties = escalate_to_human.args_schema.model_json_schema()["properties"]

    assert "question" in properties
    assert "ticket_type" not in properties
    assert "requester" not in properties
