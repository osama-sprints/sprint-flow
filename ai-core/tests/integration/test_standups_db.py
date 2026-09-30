"""Database-backed standup tests against a throwaway PostgreSQL.

Skipped unless ``SPRINTFLOW_INTEGRATION_DB=1``. Run from ``ai-core/`` with the
POSTGRES_* variables pointing at a migrated throwaway database, e.g.::

    SPRINTFLOW_INTEGRATION_DB=1 APP_ENV=test POSTGRES_DB=s1e7 \
        .venv/bin/python -m pytest -q tests/integration/test_standups_db.py

Mattermost is replaced by a fake client that records posts and fails on demand,
and the identity sync is stubbed so no REST call is made during ingestion.
"""

import asyncio
import os
import uuid
from collections.abc import Awaitable
from datetime import (
    UTC,
    date,
    datetime,
    timedelta,
)
from typing import (
    Any,
    TypeVar,
)

import pytest
from sqlalchemy import text

from app.core.config import settings
from app.models import (
    DailyStandupPrompt,
    User,
)
from app.models.enums import (
    StandupPromptStatus,
    RoleKey,
)
from app.services import standups
from app.services.database import database_service
from app.services.domain import (
    channels as channel_repo,
    identity as identity_repo,
    sprints as sprint_repo,
    standups as repo,
)
from app.workers.standup_dispatcher import StandupDispatcher

pytestmark = pytest.mark.skipif(not os.getenv("SPRINTFLOW_INTEGRATION_DB"), reason="needs SPRINTFLOW_INTEGRATION_DB=1")

PREFIX = "test-standup-"
T = TypeVar("T")

NOW = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)


class FakeMattermost:
    """Stand-in for ``MattermostClient``: records posts, fails on demand."""

    def __init__(self) -> None:
        self.posts: list[dict[str, Any]] = []
        self.fail_mode: str | None = None  # None | "raise" | "none"
        self.dm_calls = 0
        self.replies: list[dict[str, Any]] = []

    async def create_direct_channel(self, user_id: str) -> dict[str, Any] | None:
        self.dm_calls += 1
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

    async def reply_to_post(self, channel_id: str, message: str, trigger_post_id: str | None) -> dict[str, Any] | None:
        reply = await self.create_post(channel_id, message)
        if reply is not None:
            self.replies.append(reply)
        return reply


def run(coro: Awaitable[T]) -> T:
    """Run a coroutine on a fresh loop and dispose the pool so the next loop starts clean."""

    async def wrapped() -> T:
        try:
            return await coro
        finally:
            await database_service.engine.dispose()

    return asyncio.run(wrapped())


async def seed_reference_roles() -> None:
    from app.services.domain.reference_data import seed_reference_data

    await seed_reference_data()


async def cleanup() -> None:
    async with database_service.engine.begin() as conn:
        like = {"p": f"{PREFIX}%"}
        await conn.execute(
            text(
                "DELETE FROM standup_replies WHERE learner_id IN "
                "(SELECT id FROM users WHERE mattermost_user_id LIKE :p)"
            ),
            like,
        )
        await conn.execute(
            text(
                "DELETE FROM daily_standups WHERE learner_id IN "
                "(SELECT id FROM users WHERE mattermost_user_id LIKE :p)"
            ),
            like,
        )
        await conn.execute(
            text(
                "DELETE FROM daily_standup_prompts WHERE learner_id IN "
                "(SELECT id FROM users WHERE mattermost_user_id LIKE :p)"
            ),
            like,
        )
        await conn.execute(
            text("DELETE FROM channel_roles WHERE user_id IN (SELECT id FROM users WHERE mattermost_user_id LIKE :p)"),
            like,
        )
        await conn.execute(
            text("DELETE FROM sprints WHERE opened_by_id IN (SELECT id FROM users WHERE mattermost_user_id LIKE :p)"),
            like,
        )
        await conn.execute(text("DELETE FROM users WHERE mattermost_user_id LIKE :p"), like)


@pytest.fixture(autouse=True)
def fake(monkeypatch: pytest.MonkeyPatch):
    client = FakeMattermost()
    monkeypatch.setattr(standups, "mattermost_client", client)
    monkeypatch.setattr(settings, "STANDUP_ENABLED", True)
    monkeypatch.setattr(settings, "STANDUP_MAX_ATTEMPTS", 3)
    monkeypatch.setattr(settings, "STANDUP_RETRY_BACKOFF_SECONDS", 60)

    async def sync_stub(mattermost_user_id: str, profile: Any = None) -> User | None:
        return await identity_repo.get_user_by_mattermost_id(mattermost_user_id)

    monkeypatch.setattr(standups, "sync_mattermost_user", sync_stub)
    run(seed_reference_roles())
    run(cleanup())
    yield client
    run(cleanup())


async def make_user(tag: str, *, timezone: str = "UTC") -> User:
    uid = uuid.uuid4().hex[:10]
    return await identity_repo.upsert_mattermost_user(
        mattermost_user_id=f"{PREFIX}{tag}-{uid}",
        username=f"su{tag}{uid}",
        email=f"{PREFIX}{uid}@example.test",
        display_name=f"Standup {tag}",
        timezone=timezone,
        is_superadmin=False,
    )


async def make_learner(tag: str, *, timezone: str = "UTC") -> tuple[User, int, str]:
    user = await make_user(tag, timezone=timezone)
    role = await channel_repo.get_role_by_key(RoleKey.LEARNER)
    channel_id = f"ch-{tag}-{uuid.uuid4().hex[:6]}"
    await channel_repo.upsert_channel_role(
        user_id=user.id,  # type: ignore[arg-type]
        team_id="t1",
        channel_id=channel_id,
        role_id=role.id,  # type: ignore[arg-type]
        assigned_by_id=None,
    )
    sprint = await sprint_repo.create_sprint(
        channel_id=channel_id,
        name=f"Sprint {tag}",
        start_date=date(2026, 9, 1),
        end_date=date(2026, 9, 30),
        opened_by_id=user.id,
    )
    return user, sprint.id, channel_id  # type: ignore[return-value]


async def create_prompt(
    *,
    user: User,
    sprint_id: int,
    channel_id: str,
    local_date: date,
    zone: str = "UTC",
    dispatch_at: datetime | None = None,
) -> DailyStandupPrompt:
    prompt, _ = await repo.ensure_prompt(
        sprint_id=sprint_id,
        learner_id=user.id,  # type: ignore[arg-type]
        channel_id=channel_id,
        local_date=local_date,
        timezone=zone,
        dispatch_at=dispatch_at or NOW - timedelta(hours=1),
        now=NOW,
    )
    return prompt


async def dispatched_prompt(
    *,
    user: User,
    sprint_id: int,
    channel_id: str,
    local_date: date,
    dm_channel_id: str = "dm-x",
    zone: str = "UTC",
    post_id: str | None = None,
) -> DailyStandupPrompt:
    prompt = await create_prompt(
        user=user, sprint_id=sprint_id, channel_id=channel_id, local_date=local_date, zone=zone
    )
    claimed = await repo.claim_due_prompts(
        worker_id="w1",
        lease_seconds=300,
        now=NOW,
        user_ids=[user.id],  # type: ignore[list-item]
    )
    assert claimed, "no prompt claimed"
    await repo.mark_prompt_sent(
        claimed[0].id,  # type: ignore[arg-type]
        post_id=post_id or f"post-prompt-{uuid.uuid4().hex[:8]}",
        dm_channel_id=dm_channel_id,
        claimed_by="w1",
        now=NOW,
    )
    return await repo.get_prompt(prompt.id)  # type: ignore[arg-type,return-value]


def test_ensure_prompt_is_idempotent():
    async def scenario():
        user, sprint_id, channel_id = await make_learner("idem")
        first = await create_prompt(
            user=user, sprint_id=sprint_id, channel_id=channel_id, local_date=date(2026, 9, 15)
        )
        second = await create_prompt(
            user=user, sprint_id=sprint_id, channel_id=channel_id, local_date=date(2026, 9, 15)
        )
        assert second.id == first.id
        assert await repo.count_prompts(status=StandupPromptStatus.PENDING) >= 1
        assert (
            await repo.get_prompt_for(  # type: ignore[no-any-return]
                sprint_id,
                user.id,
                date(2026, 9, 15),  # type: ignore[arg-type]
            )
            is not None
        )

    run(scenario())


def test_claim_lease_makes_rows_exclusive_then_reusable():
    async def scenario():
        user, sprint_id, channel_id = await make_learner("lease")
        prompt = await create_prompt(
            user=user, sprint_id=sprint_id, channel_id=channel_id, local_date=date(2026, 9, 15)
        )
        first = await repo.claim_due_prompts(
            worker_id="w1",
            lease_seconds=300,
            now=NOW,
            user_ids=[user.id],  # type: ignore[list-item]
        )
        assert [p.id for p in first] == [prompt.id]
        # A second worker cannot take the live lease.
        second = await repo.claim_due_prompts(
            worker_id="w2",
            lease_seconds=300,
            now=NOW,
            user_ids=[user.id],  # type: ignore[list-item]
        )
        assert second == []
        # After the lease expires, a second worker may take it over.
        overtaken = await repo.claim_due_prompts(
            worker_id="w2",
            lease_seconds=300,
            now=NOW + timedelta(seconds=600),
            user_ids=[user.id],  # type: ignore[list-item]
        )
        assert [p.id for p in overtaken] == [prompt.id]
        # But the settled row is no longer pending: releasing forwarded work.
        await repo.release_prompt_claim(prompt.id)  # type: ignore[arg-type]
        assert (await repo.get_prompt(prompt.id)).status == StandupPromptStatus.PENDING.value  # type: ignore[union-attr]

    run(scenario())


def test_deliver_prompt_sends_one_dm_and_marks_dispatched(fake: FakeMattermost):
    async def scenario():
        user, sprint_id, channel_id = await make_learner("sent")
        prompt = await create_prompt(
            user=user, sprint_id=sprint_id, channel_id=channel_id, local_date=date(2026, 9, 15)
        )
        claimed = await repo.claim_due_prompts(
            worker_id="w1",
            lease_seconds=300,
            now=NOW,
            user_ids=[user.id],  # type: ignore[list-item]
        )
        result = await standups.deliver_prompt(claimed[0], now=NOW)
        assert result.outcome == standups.DeliveryOutcome.SENT
        assert result.post_id is not None
        updated = await repo.get_prompt(prompt.id)  # type: ignore[arg-type]
        assert updated is not None
        assert updated.status == StandupPromptStatus.DISPATCHED.value
        assert updated.dm_channel_id == f"dm-{user.mattermost_user_id}"
        assert updated.prompt_post_id == result.post_id
        assert updated.dispatched_at == NOW
        # Exactly one prompt post went out.
        assert len(fake.posts) == 1

    run(scenario())


def test_concurrent_prompt_delivery_sends_one_dm(fake: FakeMattermost):
    async def scenario():
        user, sprint_id, channel_id = await make_learner("race")
        prompt = await create_prompt(
            user=user, sprint_id=sprint_id, channel_id=channel_id, local_date=date(2026, 9, 15)
        )
        claimed = await repo.claim_due_prompts(
            worker_id="w1",
            lease_seconds=300,
            now=NOW,
            user_ids=[user.id],  # type: ignore[list-item]
        )
        assert claimed

        results = await asyncio.gather(
            standups.deliver_prompt(claimed[0], now=NOW),
            standups.deliver_prompt(claimed[0], now=NOW),
        )

        assert {result.outcome for result in results} == {
            standups.DeliveryOutcome.SKIPPED,
            standups.DeliveryOutcome.SENT,
        }
        assert len(fake.posts) == 1
        updated = await repo.get_prompt(prompt.id)  # type: ignore[arg-type]
        assert updated is not None and updated.status == StandupPromptStatus.DISPATCHED.value

    run(scenario())


def test_deliver_prompt_retries_then_gives_up(fake: FakeMattermost):
    async def scenario():
        user, sprint_id, channel_id = await make_learner("fail")
        prompt = await create_prompt(
            user=user, sprint_id=sprint_id, channel_id=channel_id, local_date=date(2026, 9, 15)
        )
        fake.fail_mode = "raise"
        claimed = await repo.claim_due_prompts(
            worker_id="w1",
            lease_seconds=300,
            now=NOW,
            user_ids=[user.id],  # type: ignore[list-item]
        )
        first = await standups.deliver_prompt(claimed[0], now=NOW)
        assert first.outcome == standups.DeliveryOutcome.RETRY
        row = await repo.get_prompt(prompt.id)  # type: ignore[arg-type]
        assert row is not None and row.next_attempt_at is not None and row.status == StandupPromptStatus.PENDING.value
        assert fake.posts == []
        # Exhaust the remaining attempts (the first delivery was attempt 1).
        for _ in range(settings.STANDUP_MAX_ATTEMPTS - 1):
            claimed = await repo.claim_due_prompts(
                worker_id="w1",
                lease_seconds=300,
                now=row.next_attempt_at or NOW,  # type: ignore[arg-type]
                user_ids=[user.id],  # type: ignore[list-item]
            )
            await standups.deliver_prompt(claimed[0], now=row.next_attempt_at or NOW)  # type: ignore[arg-type]
            row = await repo.get_prompt(prompt.id)  # type: ignore[arg-type]
        assert row is not None and row.status == StandupPromptStatus.FAILED.value
        assert row.dispatch_count >= settings.STANDUP_MAX_ATTEMPTS

    run(scenario())


def test_close_missed_never_fabricates_an_entry():
    async def scenario():
        user, sprint_id, channel_id = await make_learner("missed")
        yesterday = date(2026, 9, 14)
        await create_prompt(user=user, sprint_id=sprint_id, channel_id=channel_id, local_date=yesterday)
        closed = await standups.close_missed_prompts(now=NOW)
        assert closed == 1
        prompt = await repo.get_prompt_for(sprint_id, user.id, yesterday)  # type: ignore[arg-type]
        assert prompt is not None and prompt.status == StandupPromptStatus.MISSED.value
        # A silent day is a fact about the prompt, not a fabricated entry.
        entries = await repo.list_daily_standups(sprint_id, learner_id=user.id, log_date=yesterday)  # type: ignore[arg-type]
        assert entries == []

    run(scenario())


def test_ingest_accepted_reply_stores_entry_and_raw_text():
    async def scenario():
        user, sprint_id, channel_id = await make_learner("accepted", timezone="UTC")
        prompt = await dispatched_prompt(
            user=user, sprint_id=sprint_id, channel_id=channel_id, local_date=date(2026, 9, 15)
        )
        result = await standups.ingest_standup_reply(
            mattermost_user_id=user.mattermost_user_id,
            dm_channel_id="dm-x",
            channel_type="D",
            post_id="post-reply-1",
            root_id="post-prompt-root",
            text="1. Shipped the parser\n2. Run it on the cluster\n3. need the db",
            now=NOW,
        )
        assert result.result == standups.ReplyResult.ACCEPTED
        prompt = await repo.get_prompt(prompt.id)  # type: ignore[arg-type,union-attr]
        assert prompt is not None and prompt.status == StandupPromptStatus.ANSWERED.value
        entries = await repo.list_daily_standups(sprint_id, learner_id=user.id)  # type: ignore[arg-type]
        assert len(entries) == 1
        assert entries[0].what_i_did == "Shipped the parser"
        assert entries[0].what_i_will_do == "Run it on the cluster"
        assert entries[0].blockers == "need the db"
        assert entries[0].raw_response == "1. Shipped the parser\n2. Run it on the cluster\n3. need the db"
        assert entries[0].prompt_id == prompt.id
        assert entries[0].submitted_at is not None

    run(scenario())


def test_ingest_duplicate_and_late():
    async def scenario():
        user, sprint_id, channel_id = await make_learner("dup")
        prompt = await dispatched_prompt(
            user=user, sprint_id=sprint_id, channel_id=channel_id, local_date=date(2026, 9, 15)
        )
        root = prompt.prompt_post_id
        assert root is not None
        first = await standups.ingest_standup_reply(
            mattermost_user_id=user.mattermost_user_id,
            dm_channel_id="dm-x",
            channel_type="D",
            post_id="post-reply-a",
            root_id=root,
            text="1. A\n2. B\n3. C",
            now=NOW,
        )
        assert first.result == standups.ReplyResult.ACCEPTED
        second = await standups.ingest_standup_reply(
            mattermost_user_id=user.mattermost_user_id,
            dm_channel_id="dm-x",
            channel_type="D",
            post_id="post-reply-b",
            root_id=root,
            text="1. revised\n2. revised plan\n3. none",
            now=NOW,
        )
        assert second.result == standups.ReplyResult.DUPLICATE
        prompt = await repo.get_prompt(prompt.id)  # type: ignore[arg-type,union-attr]
        assert prompt is not None and prompt.status == StandupPromptStatus.ANSWERED.value
        entries = await repo.list_daily_standups(sprint_id, learner_id=user.id)  # type: ignore[arg-type]
        assert len(entries) == 1 and entries[0].what_i_did == "A"
        # A redelivery of the same event is a no-op.
        replay = await standups.ingest_standup_reply(
            mattermost_user_id=user.mattermost_user_id,
            dm_channel_id="dm-x",
            channel_type="D",
            post_id="post-reply-a",
            root_id=root,
            text="1. A\n2. B\n3. C",
            now=NOW,
        )
        assert replay.result == standups.ReplyResult.ALREADY_RECORDED

        # A reply for yesterday is late even after the day's close loop ran:
        # raw-recorded only, the prompt stays closed.
        yesterday = await dispatched_prompt(
            user=user, sprint_id=sprint_id, channel_id=channel_id, local_date=date(2026, 9, 14), dm_channel_id="dm-x"
        )
        assert (await standups.close_missed_prompts(now=NOW)) >= 1
        late_root = yesterday.prompt_post_id
        assert late_root is not None
        late = await standups.ingest_standup_reply(
            mattermost_user_id=user.mattermost_user_id,
            dm_channel_id="dm-x",
            channel_type="D",
            post_id="post-reply-c",
            root_id=late_root,
            text="1. too\n2. late\n3. never mind",
            now=NOW,
        )
        assert late.result == standups.ReplyResult.LATE
        closed = await repo.get_prompt(yesterday.id)  # type: ignore[arg-type]
        assert closed is not None and closed.status == StandupPromptStatus.MISSED.value
        rows = await repo.list_outstanding_prompts()  # type: ignore[no-any-call]
        assert all(p.id != yesterday.id for p in rows if p.id is not None)

    run(scenario())


def test_not_a_standup_when_attribution_fails():
    async def scenario():
        user, sprint_id, channel_id = await make_learner("noattr")
        # No dispatched prompt: a random DM is not a standup reply.
        result = await standups.ingest_standup_reply(
            mattermost_user_id=user.mattermost_user_id,
            dm_channel_id="dm-nothing",
            channel_type="D",
            post_id="post-x",
            root_id="",
            text="1. stuff\n2. more stuff\n3. none",
            now=NOW,
        )
        assert result.result == standups.ReplyResult.NOT_A_STANDUP
        # A bare acknowledgement sent as an answer is routed to the agent.
        prompt = await dispatched_prompt(
            user=user, sprint_id=sprint_id, channel_id=channel_id, local_date=date(2026, 9, 15)
        )
        ack = await standups.ingest_standup_reply(
            mattermost_user_id=user.mattermost_user_id,
            dm_channel_id="dm-x",
            channel_type="D",
            post_id="post-ack",
            root_id="post-prompt-root",
            text="thanks!",
            now=NOW,
        )
        assert ack.result == standups.ReplyResult.NOT_A_STANDUP
        assert (await repo.get_prompt(prompt.id)).status == StandupPromptStatus.DISPATCHED.value  # type: ignore[union-attr]

    run(scenario())


def test_reply_in_someone_elses_prompt_thread_is_not_ours():
    async def scenario():
        owner, sprint_id, channel_id = await make_learner("owner")
        stranger, _, _ = await make_learner("stranger")
        prompt = await dispatched_prompt(
            user=owner, sprint_id=sprint_id, channel_id=channel_id, local_date=date(2026, 9, 15)
        )
        assert prompt is not None and prompt.prompt_post_id is not None and prompt.dm_channel_id is not None
        # The stranger replies under the owner's prompt post in the owner's DM.
        # The thread root resolves to a prompt whose learner is NOT the author,
        # and the stranger has no outstanding prompt of their own, so the
        # message is not a standup and nothing is stored or confirmed.
        result = await standups.ingest_standup_reply(
            mattermost_user_id=stranger.mattermost_user_id,
            dm_channel_id=prompt.dm_channel_id,
            channel_type="D",
            post_id="post-intruder",
            root_id=prompt.prompt_post_id,
            text="1. I sneak in\n2. done\n3. none",
            now=NOW,
        )
        assert result.result == standups.ReplyResult.NOT_A_STANDUP
        assert (await repo.get_prompt(prompt.id)).status == StandupPromptStatus.DISPATCHED.value  # type: ignore[union-attr]
        entries = await repo.list_daily_standups(
            sprint_id,
            learner_id=stranger.id,
            log_date=date(2026, 9, 15),  # type: ignore[arg-type]
        )
        assert entries == []
        # And the owner's own answer in that thread is still accepted.
        accepted = await standups.ingest_standup_reply(
            mattermost_user_id=owner.mattermost_user_id,
            dm_channel_id=prompt.dm_channel_id,
            channel_type="D",
            post_id="post-owner",
            root_id=prompt.prompt_post_id,
            text="1. mine\n2. own plan\n3. none",
            now=NOW,
        )
        assert accepted.result == standups.ReplyResult.ACCEPTED

    run(scenario())


def test_dispatcher_run_once_delivers_and_metric_counts():
    from app.core.metrics import standup_prompts_total

    async def scenario():
        user, sprint_id, channel_id = await make_learner("dispatch")
        dispatcher = StandupDispatcher(worker_id="w-test", claim_batch_size=10, only_user_ids=[user.id])  # type: ignore[list-item]
        # Ensure + claim + deliver can all happen in a single pass when the
        # prompt is already due at start-of-pass.
        summary = await dispatcher.run_once(now=NOW)
        assert summary.ensured == 1
        assert summary.claimed == 1 and summary.sent == 1
        prompt = await repo.get_prompt_for(sprint_id, user.id, date(2026, 9, 15))  # type: ignore[arg-type]
        assert prompt is not None and prompt.status == StandupPromptStatus.DISPATCHED.value
        # The counter label fires with the delivery outcome.
        _ = standup_prompts_total.labels(outcome=standups.DeliveryOutcome.SENT.value).collect()

    run(scenario())


def test_list_standups_for_channel_returns_cohort_entries_across_a_date_range():
    async def scenario():
        user_a, sprint_a, channel_a = await make_learner("coho")
        user_b, sprint_b, channel_b = await make_learner("cohd")
        await repo.upsert_daily_standup(
            sprint_id=sprint_a,
            learner_id=user_a.id,  # type: ignore[arg-type]
            log_date=date(2026, 9, 14),
            what_i_did="day one",
            what_i_will_do="keep going",
        )
        await repo.upsert_daily_standup(
            sprint_id=sprint_a,
            learner_id=user_a.id,  # type: ignore[arg-type]
            log_date=date(2026, 9, 16),
            what_i_did="day three",
            what_i_will_do="finish",
            blockers="tired",
        )
        # One day before the requested range and one in another cohort's channel
        # must never leak into the result.
        await repo.upsert_daily_standup(
            sprint_id=sprint_a,
            learner_id=user_a.id,  # type: ignore[arg-type]
            log_date=date(2026, 9, 13),
            what_i_did="early",
            what_i_will_do="later",
        )
        await repo.upsert_daily_standup(
            sprint_id=sprint_b,
            learner_id=user_b.id,  # type: ignore[arg-type]
            log_date=date(2026, 9, 15),
            what_i_did="other cohort",
            what_i_will_do="none",
        )
        rows = await repo.list_standups_for_channel(
            channel_a, start_date=date(2026, 9, 14), end_date=date(2026, 9, 16)
        )
        assert [r.log_date for r in rows] == [date(2026, 9, 14), date(2026, 9, 16)]
        assert {r.sprint_id for r in rows} == {sprint_a}
        assert {r.what_i_did for r in rows} == {"day one", "day three"}
        # Single-day and learner-narrowed reads.
        single = await repo.list_standups_for_channel(
            channel_a, start_date=date(2026, 9, 14), end_date=date(2026, 9, 14)
        )
        assert [r.log_date for r in single] == [date(2026, 9, 14)]
        narrowed = await repo.list_standups_for_channel(
            channel_a,
            start_date=date(2026, 9, 1),
            end_date=date(2026, 9, 30),
            learner_ids=[user_b.id],  # type: ignore[list-item]
        )
        assert narrowed == []

    run(scenario())


def test_remove_prompts_between_cleans_verification_rows():
    async def scenario():
        user, sprint_id, channel_id = await make_learner("remove")
        await create_prompt(user=user, sprint_id=sprint_id, channel_id=channel_id, local_date=date(2026, 9, 15))
        removed = await repo.remove_prompts_between(user.id, date(2026, 9, 15), date(2026, 9, 15))  # type: ignore[arg-type]
        assert removed == 1
        assert (
            await repo.get_prompt_for(sprint_id, user.id, date(2026, 9, 15))  # type: ignore[arg-type]
        ) is None
        # Next ensure_prompt creates a fresh row, not the deleted one.
        fresh = await create_prompt(
            user=user, sprint_id=sprint_id, channel_id=channel_id, local_date=date(2026, 9, 15)
        )
        assert fresh.id != removed

    run(scenario())
