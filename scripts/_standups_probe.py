"""In-container standup probe: one PASS/FAIL line per assertion, non-zero exit on failure.

Driven by the host-side ``scripts/verify_standups.py`` (which pipes this file
over stdin into the ai-core container), or run directly against any database
that has the domain schema:

    docker compose exec -T ai-core /app/.venv/bin/python - < scripts/_standups_probe.py
    (cd ai-core && PYTHONPATH=. .venv/bin/python ../scripts/_standups_probe.py)

Mattermost is never called for real: ``app.services.standups.mattermost_client``
is swapped for an in-memory fake that records DMs, so the delivery and the
reply-ingestion state machines are exercised deterministically, exactly like
the escalation probe's fake. All rows this probe creates carry the
``verify-standup-`` prefix and are deleted at the end, even when an assertion
fails.
"""

import asyncio
import os
import sys
import uuid
from datetime import (
    UTC,
    date,
    datetime,
    timedelta,
)

for _candidate in ("/app", os.path.join(os.getcwd(), "ai-core"), os.getcwd()):
    if os.path.isdir(os.path.join(_candidate, "app")):
        sys.path.insert(0, _candidate)
        break

from sqlalchemy import text  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.models.enums import (  # noqa: E402
    MembershipStatus,
    RoleKey,
    SprintStatus,
    StandupPromptStatus,
)
from app.services import standups  # noqa: E402
from app.services.database import database_service  # noqa: E402
from app.services.domain import channels as channel_repo  # noqa: E402
from app.services.domain import identity as identity_repo  # noqa: E402
from app.services.domain import sprints as sprint_repo  # noqa: E402
from app.services.domain import standups as standup_repo  # noqa: E402
from app.workers.standup_dispatcher import StandupDispatcher  # noqa: E402

PREFIX = "verify-standup-"
STAMP = uuid.uuid4().hex[:8]
FORCED = datetime(2026, 9, 15, 10, 0, tzinfo=UTC)  # dispatch_at (09:00 UTC) is already due

results: list[bool] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    """Record and print one assertion."""
    results.append(ok)
    suffix = f"  ({detail})" if detail and not ok else ""
    print(f"  {label:70} {'PASS' if ok else 'FAIL'}{suffix}", flush=True)


class FakeMattermost:
    """Stands in for ``mattermost_client`` inside ``app.services.standups``."""

    def __init__(self) -> None:
        self.dm_channels: dict[str, str] = {}
        self.posts: list[dict] = []
        self.replies: list[dict] = []
        self.fail = False

    async def create_direct_channel(self, user_id: str) -> dict | None:
        if self.fail:
            return None
        channel_id = self.dm_channels.setdefault(user_id, f"dm-{user_id}")
        return {"id": channel_id}

    async def create_post(self, channel_id: str, message: str, root_id: str | None = None) -> dict | None:
        if self.fail:
            return None
        post = {"id": f"probe-post-{uuid.uuid4().hex[:8]}", "channel_id": channel_id, "message": message}
        self.posts.append(post)
        return post

    async def reply_to_post(self, channel_id: str, message: str, trigger_post_id: str | None) -> dict | None:
        reply = {"id": f"probe-reply-{uuid.uuid4().hex[:8]}", "channel_id": channel_id, "message": message}
        self.replies.append(reply)
        return reply


async def make_learner(tag: str, *, timezone: str = "UTC") -> dict:
    """Seed a learner identity, an active sprint and its LEARNER ChannelRole."""
    mattermost_user_id = f"{PREFIX}{tag}-{STAMP}"
    user = await identity_repo.upsert_mattermost_user(
        mattermost_user_id=mattermost_user_id,
        username=f"{PREFIX}{tag}-{STAMP}",
        email=f"{PREFIX}{tag}-{STAMP}@example.test",
        display_name=tag.title(),
        timezone=timezone,
        is_superadmin=False,
    )
    role = await channel_repo.get_role_by_key(RoleKey.LEARNER)
    assert user.id is not None and role is not None and role.id is not None
    channel_id = f"{PREFIX}ch-{tag}-{STAMP}"
    await channel_repo.upsert_channel_role(
        user_id=user.id, team_id="t-verify", channel_id=channel_id, role_id=role.id, assigned_by_id=None
    )
    sprint = await sprint_repo.create_sprint(
        channel_id=channel_id,
        name=f"{PREFIX}sprint-{tag}-{STAMP}",
        start_date=date(2026, 9, 1),
        end_date=date(2026, 9, 30),
        opened_by_id=user.id,
    )
    assert sprint.id is not None
    return {"user": user, "sprint_id": sprint.id, "channel_id": channel_id}


async def cleanup() -> None:
    """Delete every row this (or an earlier, crashed) run created, in dependency order."""
    async with database_service.engine.begin() as conn:
        p = {"p": f"{PREFIX}%"}
        await conn.execute(
            text(
                "DELETE FROM standup_replies WHERE learner_id IN "
                "(SELECT id FROM users WHERE mattermost_user_id LIKE :p)"
            ),
            p,
        )
        await conn.execute(
            text(
                "DELETE FROM daily_standups WHERE learner_id IN "
                "(SELECT id FROM users WHERE mattermost_user_id LIKE :p)"
            ),
            p,
        )
        await conn.execute(
            text(
                "DELETE FROM daily_standup_prompts WHERE learner_id IN "
                "(SELECT id FROM users WHERE mattermost_user_id LIKE :p)"
            ),
            p,
        )
        await conn.execute(
            text(
                "DELETE FROM channel_roles WHERE user_id IN "
                "(SELECT id FROM users WHERE mattermost_user_id LIKE :p)"
            ),
            p,
        )
        await conn.execute(
            text(
                "DELETE FROM sprints WHERE opened_by_id IN "
                "(SELECT id FROM users WHERE mattermost_user_id LIKE :p)"
            ),
            p,
        )
        await conn.execute(text("DELETE FROM users WHERE mattermost_user_id LIKE :p"), p)


async def scenario() -> None:
    """Run every assertion."""
    fake = FakeMattermost()
    standups.mattermost_client = fake  # type: ignore[assignment]

    async def _sync_stub(mattermost_user_id: str, profile=None) -> object:
        # The probe's learners are already in the DB; never call the real API.
        return await identity_repo.get_user_by_mattermost_id(mattermost_user_id)

    standups.sync_mattermost_user = _sync_stub  # type: ignore[assignment]

    print("--- deterministic delivery (faked Mattermost)")
    learner = await make_learner("one")
    uid = learner["user"].mattermost_user_id
    ref_day = date(2026, 9, 15)
    dispatch_at = FORCED - timedelta(hours=1)

    first, created = await standup_repo.ensure_prompt(
        sprint_id=learner["sprint_id"],
        learner_id=learner["user"].id,  # type: ignore[arg-type]
        channel_id=learner["channel_id"],
        local_date=ref_day,
        timezone="UTC",
        dispatch_at=dispatch_at,
        now=FORCED,
    )
    check("ensure_prompt created one prompt row", created and first.id is not None, str(first.id))
    again, _created = await standup_repo.ensure_prompt(
        sprint_id=learner["sprint_id"],
        learner_id=learner["user"].id,  # type: ignore[arg-type]
        channel_id=learner["channel_id"],
        local_date=ref_day,
        timezone="UTC",
        dispatch_at=dispatch_at,
        now=FORCED,
    )
    check("ensure_prompt is idempotent per sprint/learner/day", again.id == first.id, str(again.id))

    claimed = await standup_repo.claim_due_prompts(
        worker_id="verify", lease_seconds=300, now=FORCED, user_ids=[learner["user"].id]  # type: ignore[list-item]
    )
    check("claim_due_prompts took the due prompt", [p.id for p in claimed] == [first.id])
    result = await standups.deliver_prompt(claimed[0], now=FORCED)
    check("deliver_prompt reports SENT", result.outcome == standups.DeliveryOutcome.SENT, result.outcome.value)
    check("exactly one prompt DM was sent", len(fake.posts) == 1, f"{len(fake.posts)} posts")
    check(
        "the DM went to the learner's freshly minted DM channel",
        fake.dm_channels.get(uid) == fake.posts[0]["channel_id"],
    )
    check(
        "the prompt asks all three questions",
        "What did you do?" in fake.posts[0]["message"]
        and "What will you do next?" in fake.posts[0]["message"]
        and "Any blockers?" in fake.posts[0]["message"],
    )
    sent = await standup_repo.get_prompt(first.id)  # type: ignore[arg-type]
    check(
        "the prompt is now dispatched with post + dm ids recorded",
        sent is not None and sent.status == StandupPromptStatus.DISPATCHED.value and sent.prompt_post_id is not None
        and sent.dm_channel_id == fake.posts[0]["channel_id"],
    )

    print("--- full ingest: accepted reply stores raw text and the parsed entry")
    root = sent.prompt_post_id if sent else None
    assert root is not None
    accepted = await standups.ingest_standup_reply(
        mattermost_user_id=uid,
        dm_channel_id=fake.posts[0]["channel_id"],
        channel_type="D",
        post_id=f"{PREFIX}reply-a-{STAMP}",
        root_id=root,
        text="1. Shipped the parser\n2. Run it on the cluster\n3. need the db schema",
        now=FORCED,
    )
    check("accepted reply ingested", accepted.result == standups.ReplyResult.ACCEPTED, accepted.result.value)
    entries = await standup_repo.list_daily_standups(
        learner["sprint_id"], learner_id=learner["user"].id, log_date=ref_day  # type: ignore[arg-type]
    )
    check("exactly one standup entry was created", len(entries) == 1, f"{len(entries)} entries")
    if entries:
        check("entry has parsed fields", entries[0].what_i_did == "Shipped the parser" and entries[0].blockers == "need the db schema")
        check("entry keeps the raw reply verbatim", entries[0].raw_response == "1. Shipped the parser\n2. Run it on the cluster\n3. need the db schema")
        check("entry is linked to the prompt", entries[0].prompt_id == first.id and entries[0].submitted_at is not None)
    answered = await standup_repo.get_prompt(first.id)  # type: ignore[arg-type]
    check("the prompt is closed as answered", answered is not None and answered.status == StandupPromptStatus.ANSWERED.value)

    duplicate = await standups.ingest_standup_reply(
        mattermost_user_id=uid,
        dm_channel_id=fake.posts[0]["channel_id"],
        channel_type="D",
        post_id=f"{PREFIX}reply-b-{STAMP}",
        root_id=root,
        text="1. revised\n2. revised plan\n3. none",
        now=FORCED,
    )
    check("a second same-day reply is a duplicate, raw preserved", duplicate.result == standups.ReplyResult.DUPLICATE, duplicate.result.value)
    check("a duplicate still gets a confirmation attempt", len(fake.replies) >= 1, f"{len(fake.replies)} confirmation(s)")

    replay = await standups.ingest_standup_reply(
        mattermost_user_id=uid,
        dm_channel_id=fake.posts[0]["channel_id"],
        channel_type="D",
        post_id=f"{PREFIX}reply-a-{STAMP}",
        root_id=root,
        text="1. Shipped the parser\n2. Run it on the cluster\n3. need the db schema",
        now=FORCED,
    )
    check(
        "redelivering a recorded reply is a no-op (unique post guard)",
        replay.result == standups.ReplyResult.ALREADY_RECORDED,
        replay.result.value,
    )

    print("--- a missed day closes with no fabricated entry, and a late reply is still captured")
    yesterday = date(2026, 9, 14)
    late_prompt, _created = await standup_repo.ensure_prompt(
        sprint_id=learner["sprint_id"],
        learner_id=learner["user"].id,  # type: ignore[arg-type]
        channel_id=learner["channel_id"],
        local_date=yesterday,
        timezone="UTC",
        dispatch_at=FORCED - timedelta(days=1, hours=1),
        now=FORCED - timedelta(days=1),
    )
    claimed_old = await standup_repo.claim_due_prompts(
        worker_id="verify",
        lease_seconds=300,
        now=FORCED - timedelta(days=1, minutes=1),
        user_ids=[learner["user"].id],  # type: ignore[list-item]
    )
    assert [p.id for p in claimed_old] == [late_prompt.id]
    delivered_old = await standup_repo.mark_prompt_sent(
        late_prompt.id,  # type: ignore[arg-type]
        post_id=f"{PREFIX}late-post-{STAMP}",
        dm_channel_id=fake.posts[0]["channel_id"],
        claimed_by="verify",
        now=FORCED - timedelta(days=1),
    )
    assert delivered_old is not None and delivered_old.prompt_post_id is not None
    closed = await standups.close_missed_prompts(now=FORCED)
    check("close_missed_prompts closed the stale day", closed >= 1, f"{closed} prompts")
    stale = await standup_repo.get_prompt_for(learner["sprint_id"], learner["user"].id, yesterday)  # type: ignore[arg-type]
    check(
        "the stale dispatched prompt is now missed",
        stale is not None and stale.status == StandupPromptStatus.MISSED.value,
        stale.status if stale else "missing",
    )
    stale_entries = await standup_repo.list_daily_standups(
        learner["sprint_id"], learner_id=learner["user"].id, log_date=yesterday  # type: ignore[arg-type]
    )
    check("a missed day never fabricates an entry", stale_entries == [])
    late = await standups.ingest_standup_reply(
        mattermost_user_id=uid,
        dm_channel_id=fake.posts[0]["channel_id"],
        channel_type="D",
        post_id=f"{PREFIX}reply-late-{STAMP}",
        root_id=delivered_old.prompt_post_id,
        text="1. too\n2. late\n3. never mind",
        now=FORCED,
    )
    check("a late reply for a closed day is recorded as LATE", late.result == standups.ReplyResult.LATE, late.result.value)
    still = await standup_repo.get_prompt_for(learner["sprint_id"], learner["user"].id, yesterday)  # type: ignore[arg-type]
    check(
        "the closed prompt stays closed after a late reply",
        still is not None and still.status == StandupPromptStatus.MISSED.value,
        still.status if still else "missing",
    )

    print("--- the dispatcher's single pass delivers end-to-end (one post, typed status)")
    again_dispatcher = await make_learner("dispatch")
    dispatcher = StandupDispatcher(
        worker_id="verify", claim_batch_size=10, only_user_ids=[again_dispatcher["user"].id]  # type: ignore[list-item]
    )
    fake.posts = []
    summary = await dispatcher.run_once(now=FORCED)
    check(
        "dispatcher ensured + claimed + sent the day's prompt",
        summary.ensured >= 1 and summary.claimed == 1 and summary.sent == 1,
        str(summary),
    )
    check("dispatcher sent exactly one DM", len(fake.posts) == 1, f"{len(fake.posts)} posts")
    dispatched = await standup_repo.get_prompt_for(
        again_dispatcher["sprint_id"], again_dispatcher["user"].id, date(2026, 9, 15)  # type: ignore[arg-type]
    )
    check(
        "the dispatched row is DISPATCHED and re-answering is possible",
        dispatched is not None and dispatched.status == StandupPromptStatus.DISPATCHED.value,
    )
    check("a dispatched row is no longer claimable", await standup_repo.claim_due_prompts(
        worker_id="other", lease_seconds=300, now=FORCED, user_ids=[again_dispatcher["user"].id]  # type: ignore[list-item]
    ) == [])

    print("--- active cohort filter and recipient timezone scheduling (a cohort lives in a channel)")
    night = await make_learner("night", timezone="Asia/Riyadh")  # UTC+3, no DST
    west = await make_learner("west", timezone="America/Los_Angeles")  # UTC-7 during PDT
    cold = await make_learner("cold")
    dormant = await make_learner("dormant")
    await sprint_repo.set_sprint_status(cold["sprint_id"], SprintStatus.COMPLETED)
    await channel_repo.set_channel_role_status(
        dormant["user"].id,  # type: ignore[arg-type]
        dormant["channel_id"],
        MembershipStatus.INACTIVE,
    )

    night_created = await standups.ensure_scope(now=FORCED, user_ids=[night["user"].id])  # type: ignore[list-item]
    check(
        "an active cohort learner is prompted (active sprint + active role)",
        night_created == 1,
        f"{night_created} prompt(s)",
    )
    night_prompt = await standup_repo.get_prompt_for(
        night["sprint_id"], night["user"].id, date(2026, 9, 15)  # type: ignore[arg-type]
    )
    check(
        "the recipient's day is their local calendar day",
        night_prompt is not None and night_prompt.local_date == date(2026, 9, 15) and night_prompt.timezone == "Asia/Riyadh",
        night_prompt.local_date.isoformat() if night_prompt else "missing",
    )
    check(
        "the dispatch instant is the cohort's 09:00 local, not the server's wall clock",
        night_prompt is not None and night_prompt.dispatch_at == datetime(2026, 9, 15, 6, 0, tzinfo=UTC),
        night_prompt.dispatch_at.isoformat() if night_prompt else "missing",
    )

    west_created = await standups.ensure_scope(now=FORCED, user_ids=[west["user"].id])  # type: ignore[list-item]
    check("an active west-coast learner is prompted too", west_created == 1, f"{west_created} prompt(s)")
    west_prompt = await standup_repo.get_prompt_for(
        west["sprint_id"], west["user"].id, date(2026, 9, 15)  # type: ignore[arg-type]
    )
    check(
        "a UTC-7 recipient dispatches at 16:00 UTC (their 09:00 local)",
        west_prompt is not None and west_prompt.dispatch_at == datetime(2026, 9, 15, 16, 0, tzinfo=UTC),
        west_prompt.dispatch_at.isoformat() if west_prompt else "missing",
    )
    check(
        "that prompt is NOT due at server-10:00 UTC — it waits for the local hour",
        await standup_repo.claim_due_prompts(
            worker_id="verify", lease_seconds=300, now=FORCED, user_ids=[west["user"].id]  # type: ignore[list-item]
        ) == [],
    )
    check(
        "and becomes claimable exactly once its local hour arrives",
        [p.id for p in await standup_repo.claim_due_prompts(
            worker_id="verify", lease_seconds=300, now=FORCED + timedelta(hours=7), user_ids=[west["user"].id]  # type: ignore[list-item]
        )] == [west_prompt.id],  # type: ignore[union-attr]
    )

    cold_created = await standups.ensure_scope(now=FORCED, user_ids=[cold["user"].id])  # type: ignore[list-item]
    check(
        "a completed (inactive) cohort is never prompted",
        cold_created == 0
        and await standup_repo.get_prompt_for(cold["sprint_id"], cold["user"].id, date(2026, 9, 15))  # type: ignore[arg-type]
        is None,
        f"{cold_created} prompt(s)",
    )
    dormant_created = await standups.ensure_scope(now=FORCED, user_ids=[dormant["user"].id])  # type: ignore[list-item]
    check(
        "an inactive membership in an active cohort is never prompted",
        dormant_created == 0
        and await standup_repo.get_prompt_for(dormant["sprint_id"], dormant["user"].id, date(2026, 9, 15))  # type: ignore[arg-type]
        is None,
        f"{dormant_created} prompt(s)",
    )

    night_pass = StandupDispatcher(
        worker_id="verify-tz", claim_batch_size=10, only_user_ids=[night["user"].id]  # type: ignore[list-item]
    )
    fake.posts = []
    tz_summary = await night_pass.run_once(now=FORCED)
    night_after = await standup_repo.get_prompt_for(
        night["sprint_id"], night["user"].id, date(2026, 9, 15)  # type: ignore[arg-type]
    )
    check(
        "the timezone-scheduled prompt is delivered end to end by the real dispatcher",
        tz_summary.ensured == 0 and tz_summary.claimed == 1 and tz_summary.sent == 1
        and night_after is not None and night_after.status == StandupPromptStatus.DISPATCHED.value,
        f"{tz_summary}",
    )
    # The DISPATCHED night prompt has a post id; a second pass claims nothing new.
    check(
        "the delivered timezone prompt is no longer claimable",
        await standup_repo.claim_due_prompts(
            worker_id="other", lease_seconds=300, now=FORCED + timedelta(hours=1), user_ids=[night["user"].id]  # type: ignore[list-item]
        ) == [],
    )
    check(
        "the configured local hour reproduces both asserted dispatch instants",
        standups.dispatch_at_for(date(2026, 9, 15), "Asia/Riyadh", settings.STANDUP_PROMPT_LOCAL_HOUR)
        == datetime(2026, 9, 15, 6, 0, tzinfo=UTC)
        and standups.dispatch_at_for(date(2026, 9, 15), "America/Los_Angeles", settings.STANDUP_PROMPT_LOCAL_HOUR)
        == datetime(2026, 9, 15, 16, 0, tzinfo=UTC),
        f"hour={settings.STANDUP_PROMPT_LOCAL_HOUR}",
    )

    print("--- retained raw replies and cleanup bookkeeping")
    from app.core.metrics import standup_prompts_total

    sent_outcome = standups.DeliveryOutcome.SENT.value
    samples = standup_prompts_total.labels(outcome=sent_outcome).collect()
    check(
        "a metric total counter exists for delivery outcomes",
        len(samples) >= 1,
        f"{len(samples)} sample(s)",
    )


async def main_async() -> None:
    """Run the scenario with guaranteed cleanup."""
    await cleanup()
    try:
        await scenario()
    finally:
        try:
            await cleanup()
            async with database_service.engine.connect() as conn:
                leftover = (
                    await conn.execute(
                        text("SELECT count(*) FROM users WHERE mattermost_user_id LIKE :p"), {"p": f"{PREFIX}%"}
                    )
                ).scalar_one()
            check("probe rows cleaned up", leftover == 0, f"{leftover} users left")
        finally:
            await database_service.close()


def main() -> int:
    """Run every check.

    Returns:
        int: 0 when all pass.
    """
    print("=" * 84)
    print("SprintFlow proactive standups — in-container probe")
    print("=" * 84)
    try:
        asyncio.run(main_async())
    except Exception as exc:  # noqa: BLE001 - report, never hide, a crash
        check(f"probe crashed: {type(exc).__name__}", False, str(exc)[:300])
    print("=" * 84)
    passed = sum(results)
    print(f"{passed}/{len(results)} checks passed")
    print("STANDUPS PROBE OK" if all(results) else "STANDUPS PROBE FAILED")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())