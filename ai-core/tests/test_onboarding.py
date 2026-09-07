"""Pure-logic tests for role-aware onboarding: content, halting, backoff, role choice.

No database, no network. DB-backed behaviour lives in
``tests/integration/test_onboarding_db.py``.
"""

from datetime import (
    UTC,
    datetime,
    timedelta,
)
from itertools import combinations

import pytest

from app.core.config import settings
from app.models import (
    Cohort,
    OnboardingStep,
    User,
)
from app.models.enums import (
    OnboardingStepKind,
    RoleKey,
)
from app.services import onboarding
from app.services.onboarding import (
    NO_LEADS_TEXT,
    NO_ROLE,
    ROLE_VARIANTS,
    TEMPLATES,
    DeliveryOutcome,
    MembershipInfo,
    OnboardingContext,
    RoleContext,
    backoff_seconds,
    first_name_of,
    follow_up_due,
    is_halted,
    pick_primary_membership,
    render_message,
)
from app.workers.onboarding_dispatcher import (
    DispatchSummary,
    _is_missing_table,
)

KINDS = [kind.value for kind in OnboardingStepKind]
NOW = datetime(2026, 9, 3, 9, 0, tzinfo=UTC)

# Phrases each role variant must mention — the actions that would be wrong
# for another role — and phrases it must not.
REQUIRED_PHRASES: dict[str, dict[str, list[str]]] = {
    "learner": {
        "welcome": ["standup", "escalate", "scheduled"],
        "orientation": ["standup", "escalat", "scheduled"],
        "follow_up": ["standup", "escalate", "scheduled"],
    },
    "tech_lead": {
        "welcome": ["escalation", "dm", "reply", "open sprint", "assign", "schedule"],
        "orientation": ["escalation", "reply", "open sprint", "assign", "schedule"],
        "follow_up": ["escalation", "reply", "open sprint", "assign"],
    },
    "ops_support": {
        "welcome": ["policy", "escalation", "reply", "list cohorts", "who is in"],
        "orientation": ["policy", "escalation", "repl", "list cohorts"],
        "follow_up": ["ticket", "reply", "policy", "list cohorts"],
    },
    "scrum_master": {
        "welcome": ["schedule", "confirm", "open sprint", "scheduled"],
        "orientation": ["schedule", "confirm", "open sprint", "calendar"],
        "follow_up": ["schedule", "confirm", "open sprint"],
    },
    NO_ROLE: {
        "welcome": ["role", "orientation", "cohort lead"],
        "orientation": ["role", "cohort lead", "orientation"],
        "follow_up": ["role", "orientation", "cohort lead"],
    },
}
FORBIDDEN_PHRASES: dict[str, list[str]] = {
    # A learner must never be told to open sprints or assign roles.
    "learner": ["open sprint", "assign @"],
    # Someone without a role has no cohort to be told about.
    NO_ROLE: ["open sprint", "assign @", "esc-"],
}


def make_user(display_name: str | None = "Sara Ahmed", username: str = "sara") -> User:
    return User(id=1, mattermost_user_id="mm-sara", username=username, display_name=display_name)


def make_role(role_key: str, leads: tuple[str, ...] = ("@lead1", "@lead2")) -> RoleContext:
    label = role_key.replace("_", " ").title()
    return RoleContext(role_key=role_key, role_label=label, cohort_id=7, cohort_name="Backend-01", lead_handles=leads)


def context_for(variant: str) -> RoleContext | None:
    return None if variant == NO_ROLE else make_role(variant)


def lines_of(text: str) -> set[str]:
    return {line.strip() for line in text.splitlines() if line.strip()}


def test_all_templates_loaded_non_empty():
    assert len(TEMPLATES) == len(KINDS) * len(ROLE_VARIANTS) == 15
    for key, body in TEMPLATES.items():
        assert body.strip(), f"empty template {key}"


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("variant", ROLE_VARIANTS)
def test_every_variant_renders(kind: str, variant: str):
    rendered = render_message(kind, context_for(variant), make_user())
    assert rendered.strip()
    assert "{" not in rendered and "}" not in rendered, "unrendered placeholder"
    assert "Sara" in rendered
    assert f"@{settings.MATTERMOST_BOT_USERNAME}" in rendered
    if variant != NO_ROLE:
        assert "Backend-01" in rendered
        assert "@lead1" in rendered
    else:
        # No blank cohort name or role label leaking into the no-role variants.
        assert "****" not in rendered


@pytest.mark.parametrize("kind", KINDS)
def test_role_variants_differ_materially(kind: str):
    rendered = {
        variant: lines_of(render_message(kind, context_for(variant), make_user())) for variant in ROLE_VARIANTS
    }
    for a, b in combinations(ROLE_VARIANTS, 2):
        shared = rendered[a] & rendered[b]
        ratio = len(shared) / min(len(rendered[a]), len(rendered[b]))
        assert ratio <= 0.4, f"{kind}: {a} and {b} share {ratio:.0%} of their lines"


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("variant", ROLE_VARIANTS)
def test_role_specific_phrases(kind: str, variant: str):
    text = render_message(kind, context_for(variant), make_user()).lower()
    for phrase in REQUIRED_PHRASES[variant][kind]:
        assert phrase in text, f"{kind}_{variant} should mention {phrase!r}"
    for phrase in FORBIDDEN_PHRASES.get(variant, []):
        assert phrase not in text, f"{kind}_{variant} must not mention {phrase!r}"


def test_render_falls_back_to_no_role_for_unknown_role():
    unknown = RoleContext(role_key="mentor", role_label="Mentor", cohort_id=1, cohort_name="X")
    assert render_message("welcome", unknown, make_user()) == render_message("welcome", None, make_user())


def test_render_without_leads_uses_placeholder_text():
    rendered = render_message("welcome", make_role("learner", leads=()), make_user())
    assert NO_LEADS_TEXT in rendered


def test_first_name_prefers_display_name_then_username():
    assert first_name_of(make_user("Sara Ahmed")) == "Sara"
    assert first_name_of(make_user("  ")) == "sara"
    assert first_name_of(make_user(None, username="")) == "there"


# ---------------------------------------------------------------------------
# is_halted
# ---------------------------------------------------------------------------


def step(cohort_id: int | None, kind: str = "welcome") -> OnboardingStep:
    return OnboardingStep(id=1, user_id=1, cohort_id=cohort_id, step_kind=kind, due_at=NOW)


def membership(cohort_id: int, active: bool, role: str = "learner", joined: datetime = NOW) -> MembershipInfo:
    return MembershipInfo(
        cohort_id=cohort_id, cohort_name=f"C{cohort_id}", cohort_active=active, role_key=role, joined_at=joined
    )


def test_halted_workspace_step_without_memberships_proceeds():
    assert is_halted(step(None), OnboardingContext(role=None)) is None


def test_halted_workspace_step_when_every_cohort_inactive():
    context = OnboardingContext(role=None, memberships=(membership(1, False), membership(2, False)))
    assert is_halted(step(None), context) == "all_cohorts_inactive"


def test_halted_workspace_step_proceeds_with_one_active_cohort():
    context = OnboardingContext(role=make_role("learner"), memberships=(membership(1, False), membership(7, True)))
    assert is_halted(step(None), context) is None


def test_halted_cohort_step_when_cohort_inactive():
    cohort = Cohort(id=7, name="Backend-01", is_active=False)
    context = OnboardingContext(role=make_role("learner"), memberships=(membership(7, False),), step_cohort=cohort)
    assert is_halted(step(7, "orientation"), context) == "cohort_inactive"


def test_halted_cohort_step_when_cohort_missing():
    assert is_halted(step(7, "orientation"), OnboardingContext(role=None, step_cohort=None)) == "cohort_inactive"


def test_halted_cohort_step_when_membership_missing():
    cohort = Cohort(id=7, name="Backend-01", is_active=True)
    assert is_halted(step(7, "orientation"), OnboardingContext(role=None, step_cohort=cohort)) == "membership_missing"
    other = make_role("learner")
    wrong = RoleContext(role_key="learner", role_label="Learner", cohort_id=8, cohort_name="Other")
    assert is_halted(step(7, "orientation"), OnboardingContext(role=wrong, step_cohort=cohort)) == "membership_missing"
    assert is_halted(step(7, "orientation"), OnboardingContext(role=other, step_cohort=cohort)) is None


# ---------------------------------------------------------------------------
# Backoff and follow-up timing
# ---------------------------------------------------------------------------


def test_backoff_schedule_is_exponential_and_capped():
    assert [backoff_seconds(n, base_seconds=60) for n in range(8)] == [60, 120, 240, 480, 960, 1920, 3600, 3600]
    assert backoff_seconds(-3, base_seconds=60) == 60
    assert backoff_seconds(0, base_seconds=0) == 1
    assert backoff_seconds(2, base_seconds=10, cap_seconds=25) == 25


def test_backoff_uses_configured_base(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "ONBOARDING_RETRY_BACKOFF_SECONDS", 5)
    assert backoff_seconds(3) == 40


def test_follow_up_due_uses_configured_delay(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "ONBOARDING_FOLLOW_UP_DELAY_HOURS", 48)
    assert follow_up_due(NOW) == NOW + timedelta(hours=48)


# ---------------------------------------------------------------------------
# Role context ordering
# ---------------------------------------------------------------------------


def test_pick_primary_membership_prefers_most_recent_active_cohort():
    older = membership(1, True, "learner", NOW - timedelta(days=30))
    newer = membership(2, True, "tech_lead", NOW)
    newest_but_inactive = membership(3, False, "scrum_master", NOW + timedelta(days=1))
    assert pick_primary_membership([older, newer, newest_but_inactive]) == newer
    assert pick_primary_membership([newest_but_inactive]) is None
    assert pick_primary_membership([]) is None


def test_pick_primary_membership_breaks_ties_by_cohort_id():
    a = membership(4, True, "learner", NOW)
    b = membership(9, True, "learner", NOW)
    assert pick_primary_membership([a, b]) == b


# ---------------------------------------------------------------------------
# Dispatcher helpers
# ---------------------------------------------------------------------------


def test_dispatch_summary_records_every_outcome():
    summary = DispatchSummary()
    for outcome in DeliveryOutcome:
        summary.record(outcome)
    assert (summary.sent, summary.retry, summary.failed, summary.halted, summary.skipped) == (1, 1, 1, 1, 1)


def test_missing_table_detection():
    assert _is_missing_table(RuntimeError('relation "onboarding_steps" does not exist'))
    assert not _is_missing_table(RuntimeError("connection refused"))


def test_wake_listener_registered_once_and_failures_are_swallowed():
    calls: list[int] = []

    def listener() -> None:
        calls.append(1)

    def broken() -> None:
        raise RuntimeError("boom")

    onboarding.register_wake_listener(listener)
    onboarding.register_wake_listener(listener)
    onboarding.register_wake_listener(broken)
    try:
        onboarding._notify_dispatcher()
    finally:
        onboarding._wake_listeners.remove(listener)
        onboarding._wake_listeners.remove(broken)
    assert calls == [1]


def test_role_variants_cover_every_role_key():
    assert set(ROLE_VARIANTS) == {role.value for role in RoleKey} | {NO_ROLE}
