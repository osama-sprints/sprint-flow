"""Routing table tests: the labelled corpus, the hard cases, authority gating and latency.

Pure logic — no database, no network. The corpus in
``app.core.langgraph.routing_examples`` is the specification; every entry is
asserted here and re-run by ``scripts/_routing_probe.py`` in the container.
"""

import statistics
import time
from types import MappingProxyType

import pytest

from app.core.langgraph.routing_examples import (
    BACK_OFFICE,
    GENERAL,
    LEARNER,
    REQUESTERS,
    ROUTING_EXAMPLES,
)
from app.core.langgraph.routing_rules import (
    DENIED_SUFFIX,
    FALLBACK_RULE,
    ROUTING_RULES,
    classify_text,
    detect_intents,
    is_question_shaped,
    normalise_text,
)
from app.core.requester import RequesterContext
from app.schemas.graph import CapabilityRoute

MIN_LABELLED_SENTENCES = 40
# The exact corpus size, quoted in reports/orchestration_report.md.
LABELLED_SENTENCES = 85
LATENCY_P95_BUDGET_MS = 2.0
LATENCY_MIN_SAMPLES = 1000


def _ids() -> list[str]:
    return [f"{ex.requester}:{ex.text[:48]}" for ex in ROUTING_EXAMPLES]


@pytest.mark.parametrize("example", ROUTING_EXAMPLES, ids=_ids())
def test_labelled_sentence_routes_as_expected(example):
    results = detect_intents(example.text, REQUESTERS[example.requester])
    routes = tuple(result.route.value for result in results)
    assert routes == example.routes, f"{example.text!r} ({example.requester}) -> {routes}, note: {example.note}"
    if example.rule is not None:
        assert results[0].matched_rule == example.rule, f"{example.text!r}: rule {results[0].matched_rule}"


def test_corpus_is_large_enough_and_covers_every_rule_and_route():
    assert len(ROUTING_EXAMPLES) >= MIN_LABELLED_SENTENCES
    # Pinned so the corpus size quoted in reports/orchestration_report.md cannot
    # drift silently: change both together.
    assert len(ROUTING_EXAMPLES) == LABELLED_SENTENCES
    expected_rules = {rule.name for rule in ROUTING_RULES}
    seen_rules = {ex.rule.removesuffix(DENIED_SUFFIX) for ex in ROUTING_EXAMPLES if ex.rule}
    assert expected_rules <= seen_rules, f"rules without a labelled sentence: {expected_rules - seen_rules}"
    assert {FALLBACK_RULE} <= {ex.rule for ex in ROUTING_EXAMPLES if ex.rule}
    seen_routes = {route for ex in ROUTING_EXAMPLES for route in ex.routes}
    assert seen_routes == {LEARNER, BACK_OFFICE, GENERAL}
    assert any(len(ex.routes) > 1 for ex in ROUTING_EXAMPLES), "no multi-intent sentence in the corpus"
    assert any(ex.hard for ex in ROUTING_EXAMPLES), "no deliberately ambiguous sentence in the corpus"
    assert any(ex.rule and ex.rule.endswith(DENIED_SUFFIX) for ex in ROUTING_EXAMPLES)


def test_corpus_covers_the_sprint_one_vocabulary():
    texts = " || ".join(ex.text.lower() for ex in ROUTING_EXAMPLES)
    for word in (
        "create cohort",
        "archive cohort",
        "scrum master",
        "tech lead",
        "open sprint",
        "close sprint",
        "standup",
        "planning",
        "review",
        "retro",
        "q&a",
        "office hours",
        "demo",
        "agenda",
        "when is the next",
        "what's on this week",
        "calendar",
        "deadline",
        "policy",
        "blocked",
        "who do i ask",
        "team growth",
        "hello",
        "thanks",
    ):
        assert word in texts, f"vocabulary not covered by the corpus: {word!r}"


def test_routing_latency_p95_under_budget():
    samples_ms: list[float] = []
    while len(samples_ms) < LATENCY_MIN_SAMPLES:
        for example in ROUTING_EXAMPLES:
            requester = REQUESTERS[example.requester]
            started = time.perf_counter()
            detect_intents(example.text, requester)
            samples_ms.append((time.perf_counter() - started) * 1000.0)
    samples_ms.sort()
    p95 = samples_ms[int(len(samples_ms) * 0.95)]
    assert p95 < LATENCY_P95_BUDGET_MS, f"p95 {p95:.3f} ms over {len(samples_ms)} calls"
    assert statistics.median(samples_ms) < LATENCY_P95_BUDGET_MS


# --- Authority gating -----------------------------------------------------------


def test_learner_saying_create_cohort_is_learner_support_with_denied_rule():
    result = classify_text("create a cohort", REQUESTERS["learner"])
    assert result.route is CapabilityRoute.LEARNER_SUPPORT
    assert result.matched_rule == "back_office_cohort_denied_role"
    assert result.confidence < 0.5


def test_superadmin_without_memberships_has_authority():
    admin = REQUESTERS["admin"]
    assert admin is not None and admin.cohort_roles == {}
    assert classify_text("create cohort Growth-01", admin).route is CapabilityRoute.BACK_OFFICE


def test_inactive_or_learner_only_roles_do_not_grant_authority():
    learner_twice = RequesterContext(
        mattermost_user_id="x", cohort_roles=MappingProxyType({1: "learner", 2: "ops_support"})
    )
    assert classify_text("open sprint 2", learner_twice).route is CapabilityRoute.LEARNER_SUPPORT
    tech_lead = RequesterContext(mattermost_user_id="y", cohort_roles=MappingProxyType({3: "tech_lead"}))
    assert classify_text("open sprint 2", tech_lead).route is CapabilityRoute.BACK_OFFICE


def test_anonymous_requester_never_reaches_back_office():
    for text in ("create cohort X", "open sprint 2", "schedule a retro tomorrow", "make @bob tech lead"):
        assert classify_text(text, None).route is CapabilityRoute.LEARNER_SUPPORT


def test_public_channel_learner_admin_phrase_routes_to_learner_support():
    # The old test of this name asserted the opposite of its title; the contract is:
    # a learner saying an admin phrase goes to learner support, whatever the channel.
    public_learner = RequesterContext(
        mattermost_user_id="pl", channel_type="O", cohort_roles=MappingProxyType({1: "learner"})
    )
    result = classify_text("create a cohort", public_learner)
    assert result.route is CapabilityRoute.LEARNER_SUPPORT
    assert result.matched_rule.endswith(DENIED_SUFFIX)


def test_routing_is_not_the_authorisation_boundary():
    # A superadmin in a public channel still ROUTES to the back office: routing
    # is capability selection. The tools refuse in code (DM-only, stored flags).
    public_admin = RequesterContext(mattermost_user_id="pa", channel_type="O", is_superadmin=True)
    assert classify_text("create cohort Growth-01", public_admin).route is CapabilityRoute.BACK_OFFICE
    # And the workspace-admin route is reachable by anyone by text alone; the
    # Mattermost tools themselves refuse non-superadmins.
    assert classify_text("add alice@x.com to team Growth", REQUESTERS["learner"]).route is CapabilityRoute.GENERAL


# --- Multi-intent ----------------------------------------------------------------


def test_multi_intent_orders_mutation_before_read_and_dedupes():
    results = detect_intents("open sprint 2 for Backend-01 and tell me when the retro is", REQUESTERS["authority"])
    assert [r.route for r in results] == [CapabilityRoute.BACK_OFFICE, CapabilityRoute.LEARNER_SUPPORT]
    results = detect_intents("create cohort A and open sprint 1 and schedule the retro", REQUESTERS["admin"])
    assert [r.route for r in results] == [CapabilityRoute.BACK_OFFICE]


def test_always_returns_at_least_one_result():
    assert detect_intents("", None)[0].matched_rule == FALLBACK_RULE
    assert detect_intents("   ", REQUESTERS["admin"])[0].route is CapabilityRoute.GENERAL


# --- Question detection ---------------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("when should we schedule the retro?", True),
        ("hey @sprintflow-assistant, when is the retro?", True),
        ("did you cancel the retro", True),
        ("what's on this week and open sprint 2", False),
        ("can you open sprint 2?", False),
        ("please schedule the retro", False),
        ("is there a retro? also open sprint 2", False),
    ],
)
def test_is_question_shaped(text, expected):
    assert is_question_shaped(normalise_text(text)) is expected


def test_normalise_text_straightens_apostrophes_and_whitespace():
    assert normalise_text("what’s   on\nthis week") == "what's on this week"
    assert classify_text("what’s on this week?", REQUESTERS["learner"]).matched_rule == "learner_calendar"
