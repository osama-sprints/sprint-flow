"""In-container onboarding probe. Piped over stdin by scripts/verify_onboarding_journey.py.

    docker compose exec -T ai-core /app/.venv/bin/python - < scripts/_onboarding_probe.py [command ...]

Commands:
    scenarios (default)   Run the fake-Mattermost scenarios against the live database
                          and print one PASS/FAIL line each. Rows use the
                          ``verify-onb-`` prefix and are removed afterwards.
    replay <mm_user_id>   Call ``start_journey`` again for a real user — the exact
                          code path the ``new_user`` event runs — and print JSON.
    due-follow-up <mm_user_id>          Make the user's follow-up due now; print JSON.
    inactive-cohort <mm_user_id> <name> Create cohort <name>, add the user as learner,
                                        deactivate it, enqueue an orientation; print JSON.
    steps <mm_user_id>    Print the user's outbox rows as JSON.
    cleanup <mm_user_id> <name>         Remove the probe cohort, memberships and steps.

Runs on a throwaway database too, piped the same way (from ai-core/ with the
POSTGRES_* env set, so ``app`` resolves from the working directory):
    .venv/bin/python - < ../scripts/_onboarding_probe.py
"""

import asyncio
import json
import sys
import uuid
from datetime import timedelta
from typing import Any

from sqlalchemy import text

from app.core.config import settings
from app.models import (
    User,
    utcnow,
)
from app.models.enums import (
    OnboardingStepKind,
    RoleKey,
)
from app.services import onboarding
from app.services.database import database_service
from app.services.domain import cohorts as cohort_repo
from app.services.domain import identity as identity_repo
from app.services.domain import onboarding as outbox
from app.services.mattermost_ws import mattermost_ws_listener
from app.workers.onboarding_dispatcher import OnboardingDispatcher

PREFIX = "verify-onb-"
# Command output is one JSON object on a line starting with this marker, so the
# host verifier can tell it apart from JSON-formatted log lines on stdout.
PROBE_JSON_PREFIX = "PROBE_JSON "
# Every user this probe creates. Dispatcher passes are scoped to these ids so the
# probe can never claim (and fake-deliver) a real person's pending step.
PROBE_USER_IDS: set[int] = set()
results: list[bool] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    """Record and print one assertion."""
    results.append(ok)
    suffix = f"  ({detail})" if detail else ""
    print(f"  {label:66} {'PASS' if ok else 'FAIL'}{suffix}", flush=True)


class FakeMattermost:
    """Records posts instead of calling Mattermost; fails on demand."""

    def __init__(self) -> None:
        self.posts: list[dict[str, Any]] = []
        self.fail = False

    async def create_direct_channel(self, user_id: str) -> dict[str, Any] | None:
        if self.fail:
            raise RuntimeError("mattermost is down (probe)")
        return {"id": f"dm-{user_id}"}

    async def create_post(self, channel_id: str, message: str, root_id: str | None = None) -> dict[str, Any] | None:
        post = {"id": f"probe-post-{uuid.uuid4().hex[:8]}", "channel_id": channel_id, "message": message}
        self.posts.append(post)
        return post


def step_json(step: Any) -> dict[str, Any]:
    return {
        "id": step.id,
        "kind": step.step_kind,
        "cohort_id": step.cohort_id,
        "status": step.status,
        "due_at": step.due_at.isoformat(),
        "sent_at": step.sent_at.isoformat() if step.sent_at else None,
        "attempt_count": step.attempt_count,
        "next_attempt_at": step.next_attempt_at.isoformat() if step.next_attempt_at else None,
        "claimed_by": step.claimed_by,
        "role_key_at_delivery": step.role_key_at_delivery,
        "mattermost_post_id": step.mattermost_post_id,
        "last_error": step.last_error,
    }


async def make_user(tag: str, display_name: str = "Probe Person") -> User:
    uid = uuid.uuid4().hex[:10]
    user = await identity_repo.upsert_mattermost_user(
        mattermost_user_id=f"{PREFIX}{tag}-{uid}",
        username=f"verifyonb{tag}{uid}",
        email=f"{PREFIX}{uid}@example.test",
        display_name=display_name,
        timezone="UTC",
        is_superadmin=False,
    )
    assert user.id is not None
    PROBE_USER_IDS.add(user.id)
    return user


async def assign(user: User, cohort_id: int, role_key: RoleKey) -> None:
    role = await cohort_repo.get_role_by_key(role_key)
    assert role is not None and role.id is not None and user.id is not None
    await cohort_repo.upsert_membership(user_id=user.id, cohort_id=cohort_id, role_id=role.id, assigned_by_id=None)


async def steps_of(user: User) -> dict[str, Any]:
    assert user.id is not None
    return {f"{s.step_kind}:{s.cohort_id}": s for s in await outbox.list_steps_for_user(user.id)}


async def remove_prefixed_rows() -> None:
    async with database_service.session() as s:
        like = {"p": f"{PREFIX}%"}
        await s.execute(
            text(
                "DELETE FROM onboarding_steps WHERE user_id IN (SELECT id FROM users WHERE mattermost_user_id LIKE :p)"
                " OR cohort_id IN (SELECT id FROM cohorts WHERE name LIKE :p)"
            ),
            like,
        )
        await s.execute(
            text(
                "DELETE FROM cohort_memberships WHERE user_id IN (SELECT id FROM users WHERE mattermost_user_id LIKE :p)"
                " OR cohort_id IN (SELECT id FROM cohorts WHERE name LIKE :p)"
            ),
            like,
        )
        await s.execute(text("DELETE FROM cohorts WHERE name LIKE :p"), like)
        await s.execute(text("DELETE FROM users WHERE mattermost_user_id LIKE :p"), like)
        await s.commit()


async def scenarios() -> None:
    """Exercise the journey with a fake client.

    Every row is due 30 days out and every pass receives that instant as ``now``,
    so the live dispatcher in this container never sees the probe's rows.
    """
    fake = FakeMattermost()
    onboarding.mattermost_client = fake  # type: ignore[assignment]
    base = utcnow() + timedelta(days=30)
    delay = timedelta(hours=settings.ONBOARDING_FOLLOW_UP_DELAY_HOURS)

    def worker(name: str, batch: int = 50) -> OnboardingDispatcher:
        return OnboardingDispatcher(worker_id=f"{PREFIX}{name}", claim_batch_size=batch, only_user_ids=PROBE_USER_IDS)

    await remove_prefixed_rows()

    # 1. Arrival twice -> one welcome row, one delivery; a second pass sends nothing.
    user = await make_user("arrival")
    first = await onboarding.start_journey(user, now=base)
    second = await onboarding.start_journey(user, now=base)
    check("arrival: first start_journey creates the welcome", first is True)
    check("arrival: replayed start_journey is a no-op", second is False)
    steps = await steps_of(user)
    check("arrival: exactly one welcome and one follow-up row", set(steps) == {"welcome:None", "follow_up:None"})
    check("arrival: follow-up due after the configured delay", steps["follow_up:None"].due_at == base + delay)
    s1 = await worker("a").run_once(now=base)
    s2 = await worker("a").run_once(now=base)
    check(
        "arrival: first pass delivers exactly one post",
        s1.sent == 1 and len(fake.posts) == 1,
        f"posts={len(fake.posts)}",
    )
    check(
        "arrival: second pass delivers nothing", s2.claimed == 0 and len(fake.posts) == 1, f"posts={len(fake.posts)}"
    )
    welcome = (await steps_of(user))["welcome:None"]
    check(
        "arrival: welcome marked sent only after the post exists",
        welcome.status == "sent" and welcome.mattermost_post_id == fake.posts[0]["id"],
    )
    check(
        "arrival: no role yet -> no-role content",
        welcome.role_key_at_delivery is None and "hasn't set your role" in fake.posts[0]["message"],
    )

    # 2. Delivery failure -> not sent, attempt 1, retry scheduled, sent when recovered.
    user = await make_user("retry")
    await onboarding.start_journey(user, now=base)
    fake.fail = True
    before = len(fake.posts)
    f1 = await worker("b").run_once(now=base)
    row = (await steps_of(user))["welcome:None"]
    check(
        "retry: failed delivery is not marked sent", f1.retry == 1 and row.status == "pending" and row.sent_at is None
    )
    check(
        "retry: attempt_count == 1 and next_attempt_at set", row.attempt_count == 1 and row.next_attempt_at is not None
    )
    backoff = settings.ONBOARDING_RETRY_BACKOFF_SECONDS
    check(
        "retry: backoff equals the configured base",
        row.next_attempt_at == base + timedelta(seconds=backoff),
        str(row.next_attempt_at),
    )
    f2 = await worker("b").run_once(now=base + timedelta(seconds=backoff // 2))
    check("retry: nothing claimed inside the backoff window", f2.claimed == 0)
    fake.fail = False
    f3 = await worker("b").run_once(now=base + timedelta(seconds=backoff + 1))
    row = (await steps_of(user))["welcome:None"]
    check(
        "retry: delivered once the client recovers",
        f3.sent == 1 and row.status == "sent" and len(fake.posts) == before + 1,
    )
    check("retry: attempt_count == 2 after the successful retry", row.attempt_count == 2 and row.last_error is None)

    # 3. Inactive cohort -> no post, step stays pending and unclaimed.
    user = await make_user("inactive")
    cohort = await cohort_repo.create_cohort(f"{PREFIX}inactive-{uuid.uuid4().hex[:6]}")
    assert cohort.id is not None and user.id is not None
    await assign(user, cohort.id, RoleKey.LEARNER)
    await cohort_repo.set_cohort_active(cohort.id, False)
    await onboarding.start_journey(user, now=base)
    await outbox.enqueue_step(
        user_id=user.id, cohort_id=cohort.id, step_kind=OnboardingStepKind.ORIENTATION, due_at=base
    )
    before = len(fake.posts)
    h = await worker("c").run_once(now=base)
    rows = await steps_of(user)
    check(
        "inactive cohort: both steps halted, nothing posted",
        h.halted == 2 and h.sent == 0 and len(fake.posts) == before,
    )
    check(
        "inactive cohort: steps stay pending with the claim released",
        all(r.status == "pending" and r.claimed_by is None and r.attempt_count == 0 for r in rows.values()),
    )
    await cohort_repo.set_cohort_active(cohort.id, True)
    r = await worker("c").run_once(now=base)
    check("inactive cohort: reactivation resumes delivery", r.sent == 2 and len(fake.posts) == before + 2)

    # 4. Two dispatchers over 20 due steps -> every step delivered exactly once.
    users = [await make_user(f"conc{i}") for i in range(20)]
    for u in users:
        await onboarding.start_journey(u, now=base)
    before = len(fake.posts)
    left, right = worker("left", 3), worker("right", 3)
    taken = {"left": 0, "right": 0}
    for _ in range(40):
        a, b = await asyncio.gather(left.run_once(now=base), right.run_once(now=base))
        taken["left"] += a.claimed
        taken["right"] += b.claimed
        if a.claimed == 0 and b.claimed == 0:
            break
    sent_rows = [(await steps_of(u))["welcome:None"] for u in users]
    post_ids = {row.mattermost_post_id for row in sent_rows}
    check(
        "concurrency: 20 due steps -> exactly 20 posts",
        len(fake.posts) - before == 20,
        f"posts={len(fake.posts) - before}",
    )
    check(
        "concurrency: every step sent once with a distinct post",
        all(r.status == "sent" and r.attempt_count == 1 for r in sent_rows) and len(post_ids) == 20,
    )
    check("concurrency: both workers claimed rows", taken["left"] > 0 and taken["right"] > 0, str(taken))

    # 5. Role assigned after a role-less welcome -> one orientation post.
    user = await make_user("late", display_name="Omar Said")
    cohort = await cohort_repo.create_cohort(f"{PREFIX}late-{uuid.uuid4().hex[:6]}")
    assert cohort.id is not None and user.id is not None
    await onboarding.start_journey(user, now=base)
    await worker("d").run_once(now=base)
    await assign(user, cohort.id, RoleKey.LEARNER)
    await onboarding.on_role_assigned(user.id, cohort.id, now=base)
    before = len(fake.posts)
    o = await worker("d").run_once(now=base)
    orientation = (await steps_of(user)).get(f"orientation:{cohort.id}")
    check("late role: exactly one orientation post", o.sent == 1 and len(fake.posts) == before + 1)
    check(
        "late role: orientation marked sent as learner",
        orientation is not None and orientation.status == "sent" and orientation.role_key_at_delivery == "learner",
    )
    check(
        "late role: learner content in the orientation",
        "your role in" in fake.posts[-1]["message"] and "standup" in fake.posts[-1]["message"].lower(),
    )
    await onboarding.on_role_assigned(user.id, cohort.id, now=base)
    o2 = await worker("d").run_once(now=base)
    check("late role: repeating the assignment sends nothing", o2.claimed == 0 and len(fake.posts) == before + 1)

    # 6. Role assigned before the welcome -> the welcome carries the orientation.
    user = await make_user("early", display_name="Nour")
    cohort = await cohort_repo.create_cohort(f"{PREFIX}early-{uuid.uuid4().hex[:6]}")
    assert cohort.id is not None and user.id is not None
    await assign(user, cohort.id, RoleKey.TECH_LEAD)
    await onboarding.start_journey(user, now=base)
    await onboarding.on_role_assigned(user.id, cohort.id, now=base)
    check(
        "early role: no separate orientation while the welcome is pending",
        f"orientation:{cohort.id}" not in await steps_of(user),
    )
    before = len(fake.posts)
    e = await worker("e").run_once(now=base)
    rows = await steps_of(user)
    carried = rows.get(f"orientation:{cohort.id}")
    check(
        "early role: one post, tech-lead content",
        e.sent == 1 and len(fake.posts) == before + 1 and "Escalations arrive" in fake.posts[-1]["message"],
    )
    check("early role: welcome recorded with the role", rows["welcome:None"].role_key_at_delivery == "tech_lead")
    check(
        "early role: orientation recorded as sent by the welcome",
        carried is not None and carried.status == "sent" and carried.mattermost_post_id == fake.posts[-1]["id"],
    )
    e2 = await worker("e").run_once(now=base)
    check("early role: nothing more to send", e2.claimed == 0)

    # 7. Follow-up already due -> sent. Earlier probe users' follow-ups are due at the
    # same instant, so this counts the posts in THIS person's DM channel only.
    user = await make_user("followup")
    await onboarding.start_journey(user, now=base)
    f = await worker("f").run_once(now=base + delay + timedelta(minutes=1))
    rows = await steps_of(user)
    own_posts = [p for p in fake.posts if p["channel_id"] == f"dm-{user.mattermost_user_id}"]
    check(
        "follow-up: welcome and due follow-up both sent to this person",
        f.sent >= 2 and len(own_posts) == 2,
        f"own_posts={len(own_posts)} sent={f.sent}",
    )
    check(
        "follow-up: row marked sent with follow-up content",
        rows["follow_up:None"].status == "sent" and "checking in" in own_posts[-1]["message"],
    )

    await remove_prefixed_rows()


async def get_user_or_exit(mm_user_id: str) -> User:
    user = await identity_repo.get_user_by_mattermost_id(mm_user_id)
    if user is None or user.id is None:
        print(PROBE_JSON_PREFIX + json.dumps({"error": "user not synced", "mattermost_user_id": mm_user_id}))
        await database_service.close()
        sys.exit(2)
    assert user.id is not None
    PROBE_USER_IDS.add(user.id)
    return user


async def command(args: list[str]) -> None:
    name = args[0]
    if name == "replay":
        # Replay the arrival through the SAME handler a redelivered `new_user`
        # event runs (bot filter, identity sync, journey start, team join), not
        # just the outbox insert.
        user = await get_user_or_exit(args[1])
        before = await steps_of(user)
        await mattermost_ws_listener._handle_new_user({"user_id": args[1]})
        rows = await steps_of(user)
        created = "welcome:None" not in before and "welcome:None" in rows
        print(PROBE_JSON_PREFIX + json.dumps({"created": created, "steps": [step_json(s) for s in rows.values()]}))
        return
    if name == "steps":
        user = await get_user_or_exit(args[1])
        print(PROBE_JSON_PREFIX + json.dumps({"steps": [step_json(s) for s in (await steps_of(user)).values()]}))
        return
    if name == "due-follow-up":
        user = await get_user_or_exit(args[1])
        row = (await steps_of(user)).get("follow_up:None")
        if row is None or row.id is None:
            print(PROBE_JSON_PREFIX + json.dumps({"error": "no follow-up row"}))
            return
        async with database_service.session() as s:
            await s.execute(
                text("UPDATE onboarding_steps SET due_at = now(), next_attempt_at = NULL WHERE id = :id"),
                {"id": row.id},
            )
            await s.commit()
        print(PROBE_JSON_PREFIX + json.dumps({"step_id": row.id, "status": row.status}))
        return
    if name == "inactive-cohort":
        user = await get_user_or_exit(args[1])
        cohort = await cohort_repo.get_cohort_by_name(args[2]) or await cohort_repo.create_cohort(args[2])
        assert cohort.id is not None and user.id is not None
        await assign(user, cohort.id, RoleKey.LEARNER)
        await cohort_repo.set_cohort_active(cohort.id, False)
        step, created = await outbox.enqueue_step(
            user_id=user.id, cohort_id=cohort.id, step_kind=OnboardingStepKind.ORIENTATION, due_at=utcnow()
        )
        print(PROBE_JSON_PREFIX + json.dumps({"cohort_id": cohort.id, "step_id": step.id, "created": created}))
        return
    if name == "cleanup":
        user = await get_user_or_exit(args[1])
        cohort = await cohort_repo.get_cohort_by_name(args[2])
        async with database_service.session() as s:
            if cohort is not None:
                await s.execute(text("DELETE FROM onboarding_steps WHERE cohort_id = :c"), {"c": cohort.id})
                await s.execute(text("DELETE FROM cohort_memberships WHERE cohort_id = :c"), {"c": cohort.id})
                await s.execute(text("DELETE FROM cohorts WHERE id = :c"), {"c": cohort.id})
            await s.commit()
        print(PROBE_JSON_PREFIX + json.dumps({"cleaned": True, "user_id": user.id}))
        return
    print(PROBE_JSON_PREFIX + json.dumps({"error": f"unknown command {name}"}))
    sys.exit(2)


async def main(args: list[str]) -> int:
    try:
        if args:
            await command(args)
            return 0
        print("=" * 84)
        print("SprintFlow onboarding — in-container probe (fake Mattermost, live database)")
        print("=" * 84)
        await scenarios()
        print("=" * 84)
        print(f"{sum(results)}/{len(results)} checks passed")
        print("ONBOARDING PROBE OK" if all(results) else "ONBOARDING PROBE FAILED")
        return 0 if all(results) else 1
    finally:
        await database_service.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:])))
