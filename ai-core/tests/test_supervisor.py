"""Supervisor node tests: decision shape, metrics, no model call, prompt context.

Pure logic — the node is synchronous and touches nothing but the routing table
and the Prometheus registry.
"""

from langchain_core.messages import (
    AIMessage,
    HumanMessage,
)
from prometheus_client import (
    Counter,
    Histogram,
)

from app.core.config import settings
from app.core.langgraph.routing_examples import REQUESTERS
from app.core.langgraph.specialists import describe_route
from app.core.langgraph.supervisor import (
    FALLBACK_NO_MESSAGES,
    supervisor_node,
)
from app.core.metrics import (
    routing_decisions_total,
    routing_latency_seconds,
    routing_model_calls_total,
)
from app.core.requester import current_requester
from app.schemas.graph import (
    CapabilityRoute,
    GraphState,
)

CONFIG = {"configurable": {"thread_id": "session-1"}}


def counter_total(metric: Counter) -> float:
    return sum(s.value for m in metric.collect() for s in m.samples if s.name.endswith("_total"))


def histogram_count(metric: Histogram) -> float:
    return sum(s.value for m in metric.collect() for s in m.samples if s.name.endswith("_count"))


def test_multi_intent_decision_shape_and_metrics():
    token = current_requester.set(REQUESTERS["authority"])
    decisions_before = counter_total(routing_decisions_total)
    model_calls_before = counter_total(routing_model_calls_total)
    latency_before = histogram_count(routing_latency_seconds)
    try:
        state = GraphState(
            messages=[HumanMessage(content="open sprint 2 for Backend-01 and tell me when the retro is")]
        )
        update = supervisor_node(state, CONFIG)
    finally:
        current_requester.reset(token)

    assert update == {
        "route": CapabilityRoute.BACK_OFFICE.value,
        "route_plan": [CapabilityRoute.LEARNER_SUPPORT.value],
        "route_confidence": 0.95,
        "matched_rule": "back_office_sprint",
        "is_multi_intent": True,
    }
    assert counter_total(routing_decisions_total) == decisions_before + 1
    assert histogram_count(routing_latency_seconds) == latency_before + 1
    # The supervisor has no code path that consults a model.
    assert counter_total(routing_model_calls_total) == model_calls_before


def test_state_stores_plain_strings_not_enums():
    token = current_requester.set(REQUESTERS["learner"])
    try:
        update = supervisor_node(GraphState(messages=[HumanMessage(content="when is the next standup?")]), CONFIG)
    finally:
        current_requester.reset(token)
    assert type(update["route"]) is str
    assert all(type(route) is str for route in update["route_plan"])


def test_no_messages_falls_back_to_general():
    update = supervisor_node(GraphState(), CONFIG)
    assert update["route"] == CapabilityRoute.GENERAL.value
    assert update["matched_rule"] == FALLBACK_NO_MESSAGES
    assert update["route_plan"] == [] and update["is_multi_intent"] is False


def test_reads_dict_shaped_and_block_shaped_messages():
    token = current_requester.set(REQUESTERS["learner"])
    try:
        as_dict = supervisor_node(
            GraphState(messages=[{"role": "user", "content": "what's the leave policy?"}]), CONFIG
        )
        as_blocks = supervisor_node(
            GraphState(messages=[HumanMessage(content=[{"type": "text", "text": "what's the leave policy?"}])]),
            CONFIG,
        )
    finally:
        current_requester.reset(token)
    assert as_dict["route"] == as_blocks["route"] == CapabilityRoute.LEARNER_SUPPORT.value
    assert as_dict["matched_rule"] == "learner_support"


def test_only_the_last_message_is_classified():
    token = current_requester.set(REQUESTERS["authority"])
    try:
        state = GraphState(
            messages=[
                HumanMessage(content="open sprint 2 for Backend-01"),
                AIMessage(content="Sprint 2 is open."),
                HumanMessage(content="thanks!"),
            ]
        )
        update = supervisor_node(state, CONFIG)
    finally:
        current_requester.reset(token)
    assert update["route"] == CapabilityRoute.GENERAL.value


def test_plan_length_is_capped_by_setting(monkeypatch):
    monkeypatch.setattr(settings, "ROUTING_MAX_ROUTES_PER_TURN", 1)
    token = current_requester.set(REQUESTERS["authority"])
    try:
        update = supervisor_node(
            GraphState(messages=[HumanMessage(content="open sprint 2 for Backend-01 and tell me when the retro is")]),
            CONFIG,
        )
    finally:
        current_requester.reset(token)
    assert update["route"] == CapabilityRoute.BACK_OFFICE.value
    assert update["route_plan"] == [] and update["is_multi_intent"] is False


def test_unbound_requester_routes_admin_phrase_to_learner_support():
    assert current_requester.get() is None
    update = supervisor_node(GraphState(messages=[HumanMessage(content="create cohort X")]), CONFIG)
    assert update["route"] == CapabilityRoute.LEARNER_SUPPORT.value
    assert update["matched_rule"] == "back_office_cohort_denied_role"


# --- Prompt context -------------------------------------------------------------------


def test_describe_route_is_empty_without_a_route():
    assert describe_route(None) == ""
    assert describe_route("") == ""


def test_learner_support_context_forbids_claiming_admin_actions():
    text = describe_route(CapabilityRoute.LEARNER_SUPPORT.value)
    assert text.startswith("# Routing")
    assert "Never say or imply that such an action was performed" in text
    assert "tech lead or scrum master" in text


def test_plan_and_continuation_text():
    first = describe_route(CapabilityRoute.BACK_OFFICE.value, [CapabilityRoute.LEARNER_SUPPORT.value])
    assert "the following will handle the rest: learner_support" in first
    assert "Earlier parts" not in first
    last = describe_route(CapabilityRoute.LEARNER_SUPPORT.value, [], continuation=True)
    assert "Earlier parts of this same request were already handled" in last
    assert "ONE final reply" in last
    assert "the following will handle" not in last


def test_unknown_route_uses_general_context():
    assert describe_route("renamed_route") == describe_route(CapabilityRoute.GENERAL.value)
