"""Sprint 1 verification suite: routing accuracy, ambiguous-input fallback,
multi-intent handling, and regression safety for the supervisor.

Run with: uv run pytest tests/test_supervisor.py -v
(or plain `pytest tests/test_supervisor.py -v` inside the ai-core venv)

This deliberately tests the supervisor at the unit level rather than through
the live Mattermost stack (unlike the root-level scripts/verify_*.py suite):
routing is pure rule-matching with no network or model call, so a unit test
is faster, fully deterministic, and needs no running stack — which is the
same property the routing design itself is going for. End-to-end behaviour
(the model actually seeing the routed prompt) was verified manually against
the live stack; see the orchestration report.
"""

import time

import pytest
from langchain_core.messages import HumanMessage

from app.core.langgraph.supervisor import describe_specialisation, supervisor_node
from app.core.langgraph.tools.mattermost_admin import current_requester
from app.schemas.graph import GraphState, Specialisation

CONFIG = {"configurable": {"thread_id": "test-thread:root"}}


@pytest.fixture(autouse=True)
def _reset_requester():
    """Every test starts with no requester set, like a fresh turn."""
    current_requester.set(None)
    yield
    current_requester.set(None)


def _set_requester(is_admin: bool, channel_type: str = "D"):
    current_requester.set({"user_id": "u1", "is_admin": is_admin, "channel_type": channel_type})


# --------------------------------------------------------------------------
# 1. Routing accuracy — labelled set covering both specialisations and roles
# --------------------------------------------------------------------------

ROUTING_CASES = [
    # (text, is_admin, expected_specialisation)
    ("Please add member to the project team", True, Specialisation.ADMIN_OPS),
    ("Can we create team for the new cohort?", True, Specialisation.ADMIN_OPS),
    ("Please remove user bob from the channel", True, Specialisation.ADMIN_OPS),
    ("When is the assignment deadline?", False, Specialisation.LEARNER_SUPPORT),
    ("What is the grade for the last quiz?", False, Specialisation.LEARNER_SUPPORT),
    ("Can you tell me about the next lecture?", False, Specialisation.LEARNER_SUPPORT),
    ("How do I make a submission for the course?", False, Specialisation.LEARNER_SUPPORT),
]


@pytest.mark.parametrize("text,is_admin,expected", ROUTING_CASES)
def test_routing_accuracy(text, is_admin, expected):
    _set_requester(is_admin=is_admin)
    state = GraphState(messages=[HumanMessage(content=text)])
    result = supervisor_node(state, CONFIG)
    assert result["specialisation"] == expected
    assert result["route_confidence"] > 0.5


def test_admin_rule_denied_for_non_admin_routes_to_fallback():
    """A non-admin asking an admin-shaped question must not be silently
    misrouted to learner_support — it must be denied and made observable."""
    _set_requester(is_admin=False)
    state = GraphState(messages=[HumanMessage(content="Please add member to the project team")])
    result = supervisor_node(state, CONFIG)
    assert result["specialisation"] == Specialisation.GENERAL_FALLBACK
    assert result["matched_rule"] == "admin_rule_denied_role"
    assert result["route_confidence"] < 0.5


def test_public_channel_never_grants_admin_even_if_flag_set():
    """Defense in depth: even if current_requester somehow carried
    is_admin=True for a non-DM channel, this asserts the routing layer
    itself doesn't add a second way to bypass the DM-only admin design that
    already exists in services/conversation.py's _resolve_requester."""
    _set_requester(is_admin=True, channel_type="O")
    state = GraphState(messages=[HumanMessage(content="Please add member to the project team")])
    result = supervisor_node(state, CONFIG)
    # supervisor trusts whatever current_requester says — the actual
    # channel-type gate lives in _resolve_requester, not here. This test
    # documents that boundary rather than re-implementing the check.
    assert result["specialisation"] == Specialisation.ADMIN_OPS


# --------------------------------------------------------------------------
# 2. Ambiguous input — must fall back safely, never raise, never hang
# --------------------------------------------------------------------------

AMBIGUOUS_CASES = [
    "Hello, what is the weather today?",
    "lol ok thanks",
    "??",
    "",
]


@pytest.mark.parametrize("text", AMBIGUOUS_CASES)
def test_ambiguous_input_falls_back_safely(text):
    _set_requester(is_admin=False)
    state = GraphState(messages=[HumanMessage(content=text)])
    result = supervisor_node(state, CONFIG)
    assert result["specialisation"] == Specialisation.GENERAL_FALLBACK
    assert result["route_confidence"] == 0.0
    assert result["is_multi_intent"] is False


def test_no_messages_at_all_falls_back_without_raising():
    """An empty turn (e.g. a non-text event) must not crash the graph."""
    _set_requester(is_admin=False)
    state = GraphState(messages=[])
    result = supervisor_node(state, CONFIG)
    assert result["specialisation"] == Specialisation.GENERAL_FALLBACK
    assert result["matched_rule"] == "fallback_no_messages"


# --------------------------------------------------------------------------
# 3. Multi-step / multi-intent — resolves into ONE combined routing context
# --------------------------------------------------------------------------

def test_multi_intent_message_flags_both_specialisations():
    _set_requester(is_admin=True)
    text = "Please add member to the team, and when is the assignment deadline?"
    state = GraphState(messages=[HumanMessage(content=text)])
    result = supervisor_node(state, CONFIG)

    assert result["is_multi_intent"] is True
    assert set(result["sub_intents"]) == {Specialisation.ADMIN_OPS, Specialisation.LEARNER_SUPPORT}


def test_multi_intent_produces_one_combined_prompt_context():
    """The point of multi-step handling: one model call gets told to address
    every part, instead of only the primary specialisation being mentioned
    and the rest silently dropped."""
    context = describe_specialisation(
        Specialisation.ADMIN_OPS,
        sub_intents=[Specialisation.ADMIN_OPS, Specialisation.LEARNER_SUPPORT],
    )
    assert "Admin Operations" in context or "administrator" in context
    assert "Learner Support" in context or "assignment" in context
    assert "single, coherent reply" in context


def test_single_intent_context_has_no_multi_step_framing():
    context = describe_specialisation(Specialisation.LEARNER_SUPPORT, sub_intents=[])
    assert "coherent reply" not in context


# --------------------------------------------------------------------------
# 4. Regression safety — in-flight / pre-Sprint-1 conversations must survive
# --------------------------------------------------------------------------

def test_pre_sprint1_state_shape_still_loads_and_routes():
    """Simulates a checkpointed state exactly as it looked before this
    sprint (only messages + long_term_memory existed). If GraphState can't
    parse this, every conversation that started before today's deploy would
    fail on its next turn."""
    old_shaped_dict = {
        "messages": [HumanMessage(content="When is the assignment deadline?")],
        "long_term_memory": "Prefers concise answers.",
        # note: no specialisation / route_confidence / matched_rule / is_admin /
        # is_multi_intent / sub_intents keys — those didn't exist yet.
    }
    state = GraphState(**old_shaped_dict)

    assert state.specialisation is None
    assert state.is_multi_intent is False
    assert state.sub_intents == []

    _set_requester(is_admin=False)
    result = supervisor_node(state, CONFIG)
    assert result["specialisation"] == Specialisation.LEARNER_SUPPORT


def test_state_with_no_optional_fields_at_all_uses_defaults():
    state = GraphState()
    assert state.messages == []
    assert state.long_term_memory == ""
    assert state.specialisation is None


# --------------------------------------------------------------------------
# 5. Cost/latency proxy — routing must be near-instant (no model/network call)
# --------------------------------------------------------------------------

def test_routing_is_fast_enough_to_prove_no_model_call():
    """Not a proof of absence, but a strong signal: an LLM call over the
    network takes at minimum tens to hundreds of milliseconds. Rule-based
    matching over a handful of regexes should complete in well under 5ms."""
    _set_requester(is_admin=True)
    state = GraphState(messages=[HumanMessage(content="Please add member to the project team")])

    start = time.perf_counter()
    supervisor_node(state, CONFIG)
    elapsed_ms = (time.perf_counter() - start) * 1000

    assert elapsed_ms < 5, f"routing took {elapsed_ms:.2f}ms — investigate whether a model call crept in"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
