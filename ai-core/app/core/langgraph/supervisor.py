"""Rule-based supervisor node — routes each turn to a specialisation without
ever calling a model. See routing_rules.py for the matching table.
"""

import time
from typing import Any, Dict, List, Optional

from langchain_core.runnables import RunnableConfig

from app.core.langgraph.routing_rules import detect_intents
from app.core.langgraph.tools.mattermost_admin import current_requester
from app.core.logging import logger
from app.schemas.graph import GraphState, Specialisation

_SPECIALISATION_CONTEXT = {
    Specialisation.ADMIN_OPS: (
        "# Routing\n"
        "This request was routed to Admin Operations — the requester has been "
        "verified as an authorised administrator. Consider the administrative "
        "tools available to you for this request."
    ),
    Specialisation.LEARNER_SUPPORT: (
        "# Routing\n"
        "This request was routed to Learner Support — focus on course, "
        "assignment, and academic-support topics. Administrative actions "
        "(team/user management) are out of scope for this turn."
    ),
    Specialisation.GENERAL_FALLBACK: (
        "# Routing\n"
        "This request did not clearly match a specific specialisation, or the "
        "action implied needs permissions the requester does not have. Respond "
        "helpfully as a general assistant; if the request appears to need "
        "administrative rights the requester lacks, say so plainly rather than "
        "attempting it."
    ),
}


def describe_specialisation(
    specialisation: Optional[Specialisation],
    sub_intents: Optional[List[Specialisation]] = None,
) -> str:
    """Return the prompt text explaining this turn's routing to the model.

    Purely a behavioural nudge for the LLM — it does not change which tools
    are bound. The actual security boundary for admin actions remains the
    ADMIN_EMAILS check inside the admin tools themselves.

    When sub_intents spans more than one specialisation (a multi-step
    request), all of them are described together so one model call can
    address every part in a single coherent reply, instead of only acting on
    the primary specialisation and silently dropping the rest.
    """
    if sub_intents and len(sub_intents) > 1:
        header = (
            "# Routing\n"
            "This message spans more than one area. Address every part below "
            "in a single, coherent reply — do not answer only one part and "
            "ignore the rest:\n"
        )
        bodies = []
        for spec in sub_intents:
            block = _SPECIALISATION_CONTEXT.get(spec, "")
            body = block.split("\n", 1)[1] if "\n" in block else block
            bodies.append(f"- {body}")
        return header + "\n".join(bodies)

    if specialisation is None:
        return ""
    return _SPECIALISATION_CONTEXT.get(specialisation, "")


def _extract_last_text(state: GraphState) -> Optional[str]:
    """Pull the text of the most recent message out of the graph state."""
    if not state.messages:
        return None

    last_msg = state.messages[-1]
    text = getattr(last_msg, "content", None)
    if not text and isinstance(last_msg, dict):
        text = last_msg.get("content")

    return str(text) if text else None


def supervisor_node(state: GraphState, config: RunnableConfig) -> Dict[str, Any]:
    """Classify the current turn and record the routing decision.

    is_admin is read from `current_requester`, the same ContextVar the admin
    tools already trust — never from `state`, since nothing populates that
    field and a model-influenced state value would defeat the point of
    checking authorisation in code.
    """
    start_time = time.perf_counter()
    thread_id = (config or {}).get("configurable", {}).get("thread_id")

    requester = current_requester.get()
    is_admin = bool(requester and requester.get("is_admin"))

    text = _extract_last_text(state)

    if not text:
        result_update = {
            "specialisation": Specialisation.GENERAL_FALLBACK,
            "route_confidence": 0.0,
            "matched_rule": "fallback_no_messages",
            "is_admin": is_admin,
            "is_multi_intent": False,
            "sub_intents": [],
        }
    else:
        intents = detect_intents(text, is_admin=is_admin)
        primary = intents[0]
        is_multi = len(intents) > 1
        result_update = {
            "specialisation": primary.specialisation,
            "route_confidence": primary.confidence,
            "matched_rule": primary.matched_rule,
            "is_admin": is_admin,
            "is_multi_intent": is_multi,
            "sub_intents": [r.specialisation for r in intents] if is_multi else [],
        }

    latency_ms = round((time.perf_counter() - start_time) * 1000, 2)
    logger.info(
        "routing_decision_made",
        session_id=thread_id,
        input_text=(text[:200] if text else None),
        specialisation=result_update["specialisation"].value,
        route_confidence=result_update["route_confidence"],
        matched_rule=result_update["matched_rule"],
        is_admin=is_admin,
        is_multi_intent=result_update["is_multi_intent"],
        sub_intents=[s.value for s in result_update["sub_intents"]],
        latency_ms=latency_ms,
    )

    return result_update
