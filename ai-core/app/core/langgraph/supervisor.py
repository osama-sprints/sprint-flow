"""Rule-based supervisor node: decides which specialist handles a turn without calling a model.

The supervisor reads the requester from the ``current_requester`` ContextVar
(the same one the tools trust) and the last human message, consults the
routing table, and writes an observable decision into the graph state. It is
deliberately synchronous and I/O-free: the whole decision is a few regular
expressions, measured and exported as Prometheus metrics.

It never consults a model. ``sprintflow_routing_model_calls_total`` exists so
the escalation rate can be read off the metrics endpoint; this module has no
code path that increments it, and that is the design, not an omission.

The prompt text each specialist receives lives with the specialist
(``specialists.py``); ``describe_route`` is re-exported here for callers that
knew it under this module.
"""

import time
from typing import (
    Any,
    Dict,
    List,
    Optional,
)

from langchain_core.runnables import RunnableConfig

from app.core.config import settings
from app.core.langgraph.routing_rules import detect_intents
from app.core.langgraph.specialists import describe_route
from app.core.logging import logger
from app.core.metrics import (
    routing_decisions_total,
    routing_latency_seconds,
)
from app.core.requester import current_requester
from app.schemas.graph import (
    CapabilityRoute,
    GraphState,
)

FALLBACK_NO_MESSAGES = "fallback_no_messages"


def _last_human_text(state: GraphState) -> Optional[str]:
    """Pull the text of the most recent message out of the graph state."""
    if not state.messages:
        return None
    last = state.messages[-1]
    text = getattr(last, "content", None)
    if text is None and isinstance(last, dict):
        text = last.get("content")
    if isinstance(text, list):
        text = " ".join(str(part.get("text", "")) if isinstance(part, dict) else str(part) for part in text)
    return str(text) if text else None


def supervisor_node(state: GraphState, config: RunnableConfig) -> Dict[str, Any]:
    """Classify the current turn and record the routing decision.

    Args:
        state: The graph state; only the last message is inspected.
        config: The runnable configuration, for the session id in logs.

    Returns:
        dict: State update with ``route``, ``route_plan``, ``route_confidence``,
        ``matched_rule`` and ``is_multi_intent``.
    """
    started = time.perf_counter()
    thread_id = (config or {}).get("configurable", {}).get("thread_id")
    requester = current_requester.get()
    text = _last_human_text(state)

    if not text:
        primary_route = CapabilityRoute.GENERAL.value
        plan: List[str] = []
        confidence = 0.0
        matched_rule = FALLBACK_NO_MESSAGES
    else:
        intents = detect_intents(text, requester)[: max(1, settings.ROUTING_MAX_ROUTES_PER_TURN)]
        primary = intents[0]
        primary_route = primary.route.value
        plan = [result.route.value for result in intents[1:]]
        confidence = primary.confidence
        matched_rule = primary.matched_rule

    is_multi = bool(plan)
    latency = time.perf_counter() - started
    routing_decisions_total.labels(route=primary_route, matched_rule=matched_rule, multi_intent=str(is_multi)).inc()
    routing_latency_seconds.observe(latency)
    logger.info(
        "routing_decision_made",
        session_id=thread_id,
        route=primary_route,
        route_plan=plan,
        matched_rule=matched_rule,
        route_confidence=confidence,
        is_multi_intent=is_multi,
        requester=requester.describe() if requester else None,
        input_length=len(text) if text else 0,
        latency_ms=round(latency * 1000, 3),
        model_call=False,
    )
    return {
        "route": primary_route,
        "route_plan": plan,
        "route_confidence": confidence,
        "matched_rule": matched_rule,
        "is_multi_intent": is_multi,
    }


__all__ = ["FALLBACK_NO_MESSAGES", "describe_route", "supervisor_node"]
