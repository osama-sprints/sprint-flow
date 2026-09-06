"""Database-backed onboarding tests against a throwaway PostgreSQL.

Skipped unless ``SPRINTFLOW_INTEGRATION_DB=1``. Run from ``ai-core/`` with the
POSTGRES_* variables pointing at a migrated throwaway database, e.g.::

    SPRINTFLOW_INTEGRATION_DB=1 APP_ENV=test POSTGRES_DB=s1e5 ... \
        .venv/bin/python -m pytest -q tests/integration/test_onboarding_db.py

Mattermost is replaced by a fake client that records posts and fails on demand.
"""

import asyncio
import os
import uuid
from collections.abc import Awaitable
from datetime import timedelta
from typing import (
    Any,
    TypeVar,
)

import pytest
from sqlalchemy import text

from app.core.config import settings
from app.models import (
    Cohort,
    OnboardingStep,
    User,
    utcnow,
)
from app.models.enums import (
    OnboardingStepKind,
    OnboardingStepStatus,
    RoleKey,
)
from app.services import onboarding
from app.services.database import database_service
from app.services.domain import cohorts as cohort_repo
from app.services.domain import identity as identity_repo
from app.services.domain import onboarding as outbox
from app.workers.onboarding_dispatcher import OnboardingDispatcher

pytestmark = pytest.mark.skipif(not os.getenv("SPRINTFLOW_INTEGRATION_DB"), reason="needs SPRINTFLOW_INTEGRATION_DB=1")

PREFIX = "test-onb-"
T = TypeVar("T")


class FakeMattermost:
    """Stand-in for ``MattermostClient``: records posts, fails on demand."""

    def __init__(self) -> None:
        self.posts: list[dict[str, Any]] = []
        self.fail_mode: str | None = None  # None | "raise" | "none"
        self.channel_calls = 0

    async def create_direct_channel(self, user_id: str) -> dict[str, Any] | None:
        self.channel_calls += 1
        if self.fail_mode == "raise":
            raise RuntimeError("mattermost is down")
        if self.fail_mode == "none":
            return None
        return {"id": f"dm-{user_id}"}

    async def create_post(self, channel_id: str, message: str, root_id: str | None = None) -> dict[str, Any] | None:
        if self.fail_mode == "none":
            return None
        post = {"id": f"post-{uuid.uuid4().hex[:8]}", "channel_id": channel_id, "message": message}
        self.posts.append(post)
        return post


def run(coro: Awaitable[T]) -> T:
    """Run a coroutine on a fresh loop and dispose the pool so the next loop starts clean."""

    async def wrapped() -> T:
        try:
            return await coro
        finally:
            await database_service.engine.dispose()

    return asyncio.run(wrapped())


async def cleanup() -> None:
    async with database_service.session() as s:
        await s.execute(
            text(
                "DELETE FROM onboarding_steps WHERE user_id IN (SELECT id FROM users WHERE mattermost_user_id LIKE :p)"
                " OR cohort_id IN (SELECT id FROM cohorts WHERE name LIKE :p)"
            ),
            {"p": f"{PREFIX}%"},
        )
        await s.execute(
            text(
                "DELETE FROM cohort_memberships WHERE user_id IN (SELECT id FROM users WHERE mattermost_user_id LIKE :p)"
                " OR cohort_id IN (SELECT id FROM cohorts WHERE name LIKE :p)"
            ),
            {"p": f"{PREFIX}%"},
        )
        await s.execute(text("DELETE FROM cohorts WHERE name LIKE :p"), {"p": f"{PREFIX}%"})
        await s.execute(text("DELETE FROM users WHERE mattermost_user_id LIKE :p"), {"p": f"{PREFIX}%"})
        await s.commit()


@pytest.fixture()
def fake(monkeypatch: pytest.MonkeyPatch):
    client = FakeMattermost()
    monkeypatch.setattr(onboarding, "mattermost_client", client)
    monkeypatch.setattr(settings, "ONBOARDING_ENABLED", True)
    monkeypatch.setattr(settings, "ONBOARDING_RETRY_BACKOFF_SECONDS", 60)
    monkeypatch.setattr(settings, "ONBOARDING_MAX_ATTEMPTS", 5)
    monkeypatch.setattr(settings, "ONBOARDING_FOLLOW_UP_DELAY_HOURS", 72)
    run(cleanup())
    yield client
    run(cleanup())


async def make_user(tag: str, display_name: str = "Test Person") -> User:
    uid = uuid.uuid4().hex[:10]
    return await identity_repo.upsert_mattermost_user(
        mattermost_user_id=f"{PREFIX}{tag}-{uid}",
        username=f"onb{tag}{uid}",
        email=f"{PREFIX}{uid}@example.test",
        display_name=display_name,
        timezone="UTC",
        is_superadmin=False,
    )


async def make_cohort(tag: str) -> Cohort:
    return await cohort_repo.create_cohort(f"{PREFIX}{tag}-{uuid.uuid4().hex[:6]}")


async def assign(user: User, cohort: Cohort, role_key: RoleKey) -> None:
    role = await cohort_repo.get_role_by_key(role_key)
    assert role is not None and role.id is not None and user.id is not None and cohort.id is not None
    await cohort_repo.upsert_membership(user_id=user.id, cohort_id=cohort.id, role_id=role.id, assigned_by_id=None)


async def steps_of(user: User) -> dict[str, OnboardingStep]:
    assert user.id is not None
    rows = await outbox.list_steps_for_user(user.id)
    return {f"{row.step_kind}:{row.cohort_id}": row for row in rows}


def dispatcher(name: str = "w1", batch: int = 50) -> OnboardingDispatcher:
    return OnboardingDispatcher(worker_id=f"{PREFIX}{name}", claim_batch_size=batch)


# ---------------------------------------------------------------------------


def test_arrival_twice_creates_one_welcome_and_one_delivery(fake: FakeMattermost):
    async def scenario() -> None:
        user = await make_user("arrive")
        assert await onboarding.start_journey(user) is True
        assert await onboarding.start_journey(user) is False
        steps = await steps_of(user)
        assert set(steps) == {"welcome:None", "follow_up:None"}
        assert steps["follow_up:None"].due_at - steps["welcome:None"].due_at == timedelta(hours=72)

        first = await dispatcher().run_once()
        assert (first.claimed, first.sent) == (1, 1)
        second = await dispatcher().run_once()
        assert (second.claimed, second.sent) == (0, 0)

        assert len(fake.posts) == 1
        assert "hasn't set your role yet" in fake.posts[0]["message"]
        welcome = (await steps_of(user))["welcome:None"]
        assert welcome.status == OnboardingStepStatus.SENT.value
        assert welcome.role_key_at_delivery is None
        assert welcome.mattermost_post_id == fake.posts[0]["id"]
        assert welcome.sent_at is not None and welcome.sent_at.tzinfo is not None
        assert welcome.claimed_at is None and welcome.claimed_by is None

    run(scenario())


def test_delivery_failure_is_retried_and_then_sent(fake: FakeMattermost):
    async def scenario() -> None:
        user = await make_user("retry")
        await onboarding.start_journey(user)
        fake.fail_mode = "raise"
        now = utcnow()

        summary = await dispatcher().run_once(now=now)
        assert (summary.claimed, summary.retry, summary.sent) == (1, 1, 0)
        assert fake.posts == []
        welcome = (await steps_of(user))["welcome:None"]
        assert welcome.status == OnboardingStepStatus.PENDING.value
        assert welcome.attempt_count == 1
        assert welcome.sent_at is None
        assert welcome.next_attempt_at is not None
        assert welcome.next_attempt_at - now == timedelta(seconds=60)
        assert welcome.last_error and "mattermost is down" in welcome.last_error
        assert welcome.claimed_at is None

        # Still inside the backoff window: nothing is claimed.
        again = await dispatcher().run_once(now=now + timedelta(seconds=30))
        assert again.claimed == 0

        fake.fail_mode = None
        recovered = await dispatcher().run_once(now=now + timedelta(seconds=61))
        assert (recovered.claimed, recovered.sent) == (1, 1)
        assert len(fake.posts) == 1
        welcome = (await steps_of(user))["welcome:None"]
        assert welcome.status == OnboardingStepStatus.SENT.value
        assert welcome.attempt_count == 2
        assert welcome.last_error is None
        assert welcome.next_attempt_at is None

    run(scenario())


def test_post_refused_counts_as_failure(fake: FakeMattermost):
    async def scenario() -> None:
        user = await make_user("refused")
        await onboarding.start_journey(user)
        fake.fail_mode = "none"
        summary = await dispatcher().run_once()
        assert summary.retry == 1
        welcome = (await steps_of(user))["welcome:None"]
        assert welcome.status == OnboardingStepStatus.PENDING.value and welcome.attempt_count == 1

    run(scenario())


def test_gives_up_after_max_attempts(fake: FakeMattermost, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "ONBOARDING_MAX_ATTEMPTS", 2)

    async def scenario() -> None:
        user = await make_user("giveup")
        await onboarding.start_journey(user)
        fake.fail_mode = "raise"
        now = utcnow()
        first = await dispatcher().run_once(now=now)
        assert first.retry == 1
        second = await dispatcher().run_once(now=now + timedelta(hours=1))
        assert (second.retry, second.failed) == (0, 1)
        welcome = (await steps_of(user))["welcome:None"]
        assert welcome.status == OnboardingStepStatus.FAILED.value
        assert welcome.attempt_count == 2
        assert welcome.next_attempt_at is None
        # Operators re-queue by resetting the status; nothing else is needed.
        third = await dispatcher().run_once(now=now + timedelta(hours=5))
        assert third.claimed == 0
        assert fake.posts == []

    run(scenario())


def test_inactive_cohort_halts_steps_and_reactivation_resumes(fake: FakeMattermost):
    async def scenario() -> None:
        user = await make_user("inactive")
        cohort = await make_cohort("inactive")
        assert cohort.id is not None and user.id is not None
        await assign(user, cohort, RoleKey.LEARNER)
        await cohort_repo.set_cohort_active(cohort.id, False)
        await onboarding.start_journey(user)
        await outbox.enqueue_step(
            user_id=user.id, cohort_id=cohort.id, step_kind=OnboardingStepKind.ORIENTATION, due_at=utcnow()
        )

        summary = await dispatcher().run_once()
        assert (summary.claimed, summary.halted, summary.sent) == (2, 2, 0)
        assert fake.posts == []
        steps = await steps_of(user)
        for key in ("welcome:None", f"orientation:{cohort.id}"):
            assert steps[key].status == OnboardingStepStatus.PENDING.value
            assert steps[key].attempt_count == 0
            assert steps[key].claimed_at is None and steps[key].claimed_by is None

        # A second pass is still silent.
        assert (await dispatcher().run_once()).sent == 0
        assert fake.posts == []

        # Resetting the kill switch lets the journey continue.
        await cohort_repo.set_cohort_active(cohort.id, True)
        resumed = await dispatcher().run_once()
        assert resumed.sent == 2
        assert len(fake.posts) == 2
        welcome = (await steps_of(user))["welcome:None"]
        assert welcome.role_key_at_delivery == RoleKey.LEARNER.value

    run(scenario())


def test_two_dispatchers_deliver_twenty_steps_exactly_once(fake: FakeMattermost):
    async def scenario() -> None:
        users = [await make_user(f"conc{i}") for i in range(20)]
        for user in users:
            await onboarding.start_journey(user)
        left, right = dispatcher("left", batch=3), dispatcher("right", batch=3)
        claimed_by: dict[str, int] = {"left": 0, "right": 0}
        for _ in range(40):
            a, b = await asyncio.gather(left.run_once(), right.run_once())
            claimed_by["left"] += a.claimed
            claimed_by["right"] += b.claimed
            if a.claimed == 0 and b.claimed == 0:
                break
        assert len(fake.posts) == 20
        assert sum(claimed_by.values()) == 20
        assert claimed_by["left"] > 0 and claimed_by["right"] > 0, "both workers should take part"
        post_ids = set()
        for user in users:
            welcome = (await steps_of(user))["welcome:None"]
            assert welcome.status == OnboardingStepStatus.SENT.value
            assert welcome.attempt_count == 1
            post_ids.add(welcome.mattermost_post_id)
        assert len(post_ids) == 20

    run(scenario())


def test_role_assigned_after_roleless_welcome_sends_one_orientation(fake: FakeMattermost):
    async def scenario() -> None:
        user = await make_user("late-role", display_name="Omar Said")
        cohort = await make_cohort("late-role")
        lead = await make_user("late-lead")
        assert cohort.id is not None and user.id is not None
        await assign(lead, cohort, RoleKey.SCRUM_MASTER)

        await onboarding.start_journey(user)
        assert (await dispatcher().run_once()).sent == 1
        assert (await steps_of(user))["welcome:None"].role_key_at_delivery is None

        await assign(user, cohort, RoleKey.LEARNER)
        await onboarding.on_role_assigned(user.id, cohort.id)
        orientation = (await steps_of(user))[f"orientation:{cohort.id}"]
        assert orientation.status == OnboardingStepStatus.PENDING.value

        assert (await dispatcher().run_once()).sent == 1
        assert len(fake.posts) == 2
        message = fake.posts[1]["message"]
        assert "Omar, your role in" in message and cohort.name in message
        assert f"@{lead.username}" in message
        orientation = (await steps_of(user))[f"orientation:{cohort.id}"]
        assert orientation.status == OnboardingStepStatus.SENT.value
        assert orientation.role_key_at_delivery == RoleKey.LEARNER.value

        # Repeating the assignment hook creates nothing and sends nothing.
        await onboarding.on_role_assigned(user.id, cohort.id)
        assert (await dispatcher().run_once()).claimed == 0
        assert len(fake.posts) == 2

    run(scenario())


def test_role_before_welcome_makes_welcome_carry_orientation(fake: FakeMattermost):
    async def scenario() -> None:
        user = await make_user("early-role", display_name="Nour")
        cohort = await make_cohort("early-role")
        assert cohort.id is not None and user.id is not None
        await assign(user, cohort, RoleKey.TECH_LEAD)
        await onboarding.start_journey(user)
        await onboarding.on_role_assigned(user.id, cohort.id)
        assert f"orientation:{cohort.id}" not in await steps_of(user), "welcome pending: no separate orientation"

        assert (await dispatcher().run_once()).sent == 1
        assert len(fake.posts) == 1
        assert "Escalations arrive as DMs" in fake.posts[0]["message"]
        steps = await steps_of(user)
        assert steps["welcome:None"].role_key_at_delivery == RoleKey.TECH_LEAD.value
        carried = steps[f"orientation:{cohort.id}"]
        assert carried.status == OnboardingStepStatus.SENT.value
        assert carried.mattermost_post_id == fake.posts[0]["id"]

        await onboarding.on_role_assigned(user.id, cohort.id)
        assert (await dispatcher().run_once()).claimed == 0
        assert len(fake.posts) == 1

    run(scenario())


def test_follow_up_due_in_the_past_is_sent(fake: FakeMattermost):
    async def scenario() -> None:
        user = await make_user("followup")
        arrived = utcnow() - timedelta(hours=100)
        await onboarding.start_journey(user, now=arrived)
        summary = await dispatcher().run_once()
        assert summary.sent == 2
        assert len(fake.posts) == 2
        assert "checking in" in fake.posts[1]["message"]
        follow_up = (await steps_of(user))["follow_up:None"]
        assert follow_up.status == OnboardingStepStatus.SENT.value
        assert follow_up.due_at == arrived + timedelta(hours=72)

    run(scenario())


def test_only_inactive_memberships_halt_workspace_steps(fake: FakeMattermost):
    async def scenario() -> None:
        user = await make_user("halt-all")
        inactive = await make_cohort("halt-inactive")
        assert inactive.id is not None
        await assign(user, inactive, RoleKey.LEARNER)
        await cohort_repo.set_cohort_active(inactive.id, False)
        await onboarding.start_journey(user)
        assert (await dispatcher().run_once()).halted == 1

        active = await make_cohort("halt-active")
        await assign(user, active, RoleKey.OPS_SUPPORT)
        summary = await dispatcher().run_once()
        assert summary.sent == 1
        assert "policy" in fake.posts[0]["message"].lower()
        assert (await steps_of(user))["welcome:None"].role_key_at_delivery == RoleKey.OPS_SUPPORT.value

    run(scenario())


def test_start_journey_is_noop_when_disabled(fake: FakeMattermost, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "ONBOARDING_ENABLED", False)

    async def scenario() -> None:
        user = await make_user("disabled")
        assert await onboarding.start_journey(user) is False
        assert await steps_of(user) == {}

    run(scenario())


# --- Review fixes (2026-09-03): claim ownership and orientation while a welcome is in flight ---


def test_settlement_requires_the_claim_holder(fake: FakeMattermost):
    async def scenario() -> None:
        user = await make_user("claim")
        assert user.id is not None
        await onboarding.start_journey(user)
        claimed = await outbox.claim_due_steps(worker_id=f"{PREFIX}holder", lease_seconds=600, limit=5)
        welcome = next(step for step in claimed if step.step_kind == "welcome")
        assert welcome.id is not None and welcome.claimed_by == f"{PREFIX}holder"
        # Another worker (whose lease expired and was taken over) may not settle the row.
        assert (
            await outbox.mark_step_sent(welcome.id, mattermost_post_id="p", role_key=None, claimed_by="intruder")
            is None
        )
        assert (
            await outbox.mark_step_failed(
                welcome.id, error="x", next_attempt_at=utcnow(), max_attempts=5, claimed_by="intruder"
            )
            is None
        )
        still = await outbox.get_step(welcome.id)
        assert still is not None and still.status == "pending" and still.claimed_by == f"{PREFIX}holder"
        # The holder can.
        settled = await outbox.mark_step_sent(
            welcome.id, mattermost_post_id="p", role_key=None, claimed_by=f"{PREFIX}holder"
        )
        assert settled is not None and settled.status == "sent"

    run(scenario())


def test_role_assigned_while_welcome_is_claimed_still_gets_an_orientation(fake: FakeMattermost):
    async def scenario() -> None:
        user = await make_user("inflight")
        cohort = await make_cohort("inflight")
        assert user.id is not None and cohort.id is not None
        await onboarding.start_journey(user)
        # A worker claims the welcome (delivery in progress, role resolved as "none yet").
        await outbox.claim_due_steps(worker_id=f"{PREFIX}slow", lease_seconds=600, limit=5)
        # The role lands while the welcome is in flight: the orientation must be queued on its own.
        await assign(user, cohort, RoleKey.LEARNER)
        await onboarding.on_role_assigned(user.id, cohort.id)
        steps = await steps_of(user)
        assert f"orientation:{cohort.id}" in steps
        assert steps[f"orientation:{cohort.id}"].status == "pending"

    run(scenario())
