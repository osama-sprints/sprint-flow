"""Database-backed proof of the proactive ceremony reminder flow.

Runs only with ``SPRINTFLOW_INTEGRATION_DB=1`` (like ``test_ceremony_scheduling_db.py``).

Each test seeds its own people, channel memberships and ceremonies (prefix
``it-rem-``), runs the real poller path (``_run_once`` / ``send_reminder``)
against the real ``ceremony_reminders`` table through a fake Mattermost client,
asserts on the DMs actually posted and the idempotency rows, and deletes what
it made.

What this proves end to end (the pieces the unit tests only mock):
- the 24h and 1h windows each deliver exactly one DM per active member;
- a ``CeremonyReminder`` row exists for every (ceremony, recipient, window),
  so a second poll pass sends nothing (idempotent / restart-safe);
- inactive channel roles are excluded (scope filtering);
- a ceremony whose channel has no members is skipped without error;
- a failed Mattermost post records nothing and does not crash the poller.
"""

import asyncio
import os
import time as time_module
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from app.models.enums import CeremonyTypeKey, MembershipStatus, RoleKey
from app.services import ceremony_reminders
from app.services.domain import ceremonies as ceremony_repo
from app.services.domain import channels as channel_repo
from app.services.domain import identity as identity_repo
from app.services.database import database_service

pytestmark = pytest.mark.skipif(
    not os.getenv("SPRINTFLOW_INTEGRATION_DB"), reason="needs a real database (SPRINTFLOW_INTEGRATION_DB=1)"
)

_PREFIX = "it-rem-"


class FakeMattermost:
    """Stand-in for ``MattermostClient``: records DM posts, fails on demand."""

    def __init__(self) -> None:
        self.posts: list[dict[str, object]] = []
        self.fail_create_post = False

    async def get_user_timezone(self, user_id: str) -> str:
        return "UTC"

    async def create_direct_channel(self, user_id: str) -> dict[str, object] | None:
        return {"id": f"dm-{user_id}"}

    async def create_post(self, channel_id: str, message: str, root_id: str | None = None) -> dict[str, object] | None:
        if self.fail_create_post:
            return None
        post = {"id": f"post-{uuid.uuid4().hex[:8]}", "channel_id": channel_id, "message": message}
        self.posts.append(post)
        return post


def run(coro: object) -> object:
    """Run a coroutine on a fresh loop and dispose the pool so the next loop starts clean."""

    async def wrapped() -> object:
        try:
            return await coro  # type: ignore[misc]
        finally:
            await database_service.engine.dispose()

    return asyncio.run(wrapped())  # type: ignore[arg-type]


async def _seed_reference_data() -> None:
    from app.services.domain.reference_data import seed_reference_data

    await seed_reference_data()


async def _cleanup() -> None:
    async with database_service.engine.begin() as conn:
        like = {"p": f"{_PREFIX}%"}
        await conn.execute(
            text(
                "DELETE FROM ceremony_reminders WHERE ceremony_id IN "
                "(SELECT id FROM ceremonies WHERE channel_id LIKE :p)"
            ),
            like,
        )
        await conn.execute(
            text(
                "DELETE FROM ceremony_amendments WHERE ceremony_id IN "
                "(SELECT id FROM ceremonies WHERE channel_id LIKE :p)"
            ),
            like,
        )
        await conn.execute(text("DELETE FROM ceremonies WHERE channel_id LIKE :p"), like)
        await conn.execute(text("DELETE FROM sprints WHERE channel_id LIKE :p"), like)
        await conn.execute(text("DELETE FROM channel_roles WHERE channel_id LIKE :p"), like)
        await conn.execute(text("DELETE FROM users WHERE mattermost_user_id LIKE :p"), like)


@pytest.fixture(autouse=True)
def fake(monkeypatch: pytest.MonkeyPatch):
    client = FakeMattermost()
    monkeypatch.setattr(ceremony_reminders, "mattermost_client", client)
    run(_seed_reference_data())
    run(_cleanup())
    yield client
    run(_cleanup())


async def make_person(tag: str, *, timezone: str = "UTC") -> object:
    uid = uuid.uuid4().hex[:10]
    return await identity_repo.upsert_mattermost_user(
        mattermost_user_id=f"{_PREFIX}{tag}-{uid}",
        username=f"{_PREFIX}{tag}{uid}",
        email=f"{_PREFIX}{uid}@example.test",
        display_name=f"Rem {tag}",
        timezone=timezone,
        is_superadmin=False,
    )


async def seed_ceremony(
    channel_id: str,
    organizer_id: int,
    hours_before: int,
) -> object:
    """Insert a scheduled ceremony starting `hours_before` hours from now (UTC)."""
    planning = await ceremony_repo.get_ceremony_type_by_key(CeremonyTypeKey.SPRINT_PLANNING)
    assert planning is not None and planning.id is not None
    scheduled_at = datetime.now(UTC) + timedelta(hours=hours_before)
    return await ceremony_repo.create_ceremony(
        team_id=f"{_PREFIX}team",
        channel_id=channel_id,
        ceremony_type_id=planning.id,
        organizer_id=organizer_id,
        scheduled_at=scheduled_at,
        duration_minutes=60,
        agenda="Plan the sprint",
    )


async def add_member(
    person: object,
    channel_id: str,
    team_id: str,
    role: RoleKey = RoleKey.LEARNER,
    *,
    status: str = MembershipStatus.ACTIVE.value,
) -> None:
    role_id = await channel_repo.get_role_by_key(role)
    assert role_id is not None and role_id.id is not None
    await channel_repo.upsert_channel_role(
        user_id=person.id if person.id is not None else 0,
        team_id=team_id,
        channel_id=channel_id,
        role_id=role_id.id,
        assigned_by_id=None,
    )
    await _set_membership_status(person.id if person.id is not None else 0, channel_id, status)


async def _set_membership_status(user_id: int, channel_id: str, status: str) -> None:
    async with database_service.session() as s:
        await s.exec(
            text("UPDATE channel_roles SET status = :status WHERE user_id = :u AND channel_id = :c"),
            params={"status": status, "u": user_id, "c": channel_id},
        )
        await s.commit()


async def _reminder_count(ceremony_id: int) -> int:
    async with database_service.session() as s:
        result = await s.exec(
            text("SELECT count(*) FROM ceremony_reminders WHERE ceremony_id = :c"), params={"c": ceremony_id}
        )
        return int(result.scalar_one())


def test_poller_sends_next_day_window_and_records_idempotency_rows(fake: FakeMattermost):
    """A ceremony ~24h out gets one DM per active member; re-polling sends nothing."""

    async def scenario() -> None:
        channel = f"{_PREFIX}plan-{int(time_module.time() * 1000)}"
        team = f"{_PREFIX}team"
        organizer = await make_person("organizer")
        assert organizer.id is not None
        members = [await make_person(f"learner{i}") for i in range(2)]
        assert all(m.id is not None for m in members)

        for m in members:
            await add_member(m, channel, team, RoleKey.LEARNER)

        ceremony_24 = await seed_ceremony(channel, organizer.id, hours_before=24)
        assert ceremony_24.id is not None

        await ceremony_reminders._run_once()

        # 2 members x 1 window = 2 DMs.
        assert len([p for p in fake.posts if str(p["channel_id"]).startswith("dm-")]) == 2
        assert await _reminder_count(ceremony_24.id) == 2

        posts_before = len(fake.posts)
        # A second poll pass finds the same due ceremony but the rows say
        # "already sent" -> nothing new posted, nothing new recorded.
        await ceremony_reminders._run_once()
        assert len(fake.posts) == posts_before
        assert await _reminder_count(ceremony_24.id) == 2

    run(scenario())


def test_concurrent_workers_send_one_reminder(fake: FakeMattermost):
    """The database lock prevents duplicate DMs when workers race."""

    async def scenario() -> None:
        channel = f"{_PREFIX}race-{int(time_module.time() * 1000)}"
        organizer = await make_person("organizer")
        member = await make_person("member")
        assert organizer.id is not None and member.id is not None
        await add_member(member, channel, f"{_PREFIX}team", RoleKey.LEARNER)
        ceremony = await seed_ceremony(channel, organizer.id, hours_before=24)
        assert ceremony.id is not None

        results = await asyncio.gather(
            ceremony_reminders.send_reminder(ceremony, member.mattermost_user_id, "24h", "Sprint Planning"),
            ceremony_reminders.send_reminder(ceremony, member.mattermost_user_id, "24h", "Sprint Planning"),
        )

        assert sorted(results) == [False, True]
        assert len(fake.posts) == 1
        assert await _reminder_count(ceremony.id) == 1

    run(scenario())


def test_poller_sends_last_hour_window_with_catchup(fake: FakeMattermost):
    """A ceremony ~1h out gets both the last-hour window and the 24h catch-up DM."""

    async def scenario() -> None:
        channel = f"{_PREFIX}soon-{int(time_module.time() * 1000)}"
        team = f"{_PREFIX}team"
        organizer = await make_person("organizer")
        assert organizer.id is not None
        members = [await make_person(f"learner{i}") for i in range(2)]
        assert all(m.id is not None for m in members)

        for m in members:
            await add_member(m, channel, team, RoleKey.LEARNER)

        ceremony_1 = await seed_ceremony(channel, organizer.id, hours_before=1)
        assert ceremony_1.id is not None

        await ceremony_reminders._run_once()

        # 2 members x 2 windows (24h catch-up + 1h) = 4 DMs.
        assert len([p for p in fake.posts if str(p["channel_id"]).startswith("dm-")]) == 4
        assert await _reminder_count(ceremony_1.id) == 4

        posts_before = len(fake.posts)
        await ceremony_reminders._run_once()
        assert len(fake.posts) == posts_before
        assert await _reminder_count(ceremony_1.id) == 4

    run(scenario())


def test_inactive_membership_is_not_reminded(fake: FakeMattermost):
    """Scope filtering: an inactive channel role receives no reminder DM."""

    async def scenario() -> None:
        channel = f"{_PREFIX}inactive-{int(time_module.time() * 1000)}"
        team = f"{_PREFIX}team"
        organizer = await make_person("organizer")
        assert organizer.id is not None
        active = await make_person("active")
        inactive = await make_person("inactive")
        assert active.id is not None and inactive.id is not None

        await add_member(active, channel, team, RoleKey.LEARNER)
        await add_member(inactive, channel, team, RoleKey.LEARNER, status=MembershipStatus.INACTIVE.value)

        ceremony = await seed_ceremony(channel, organizer.id, hours_before=24)
        assert ceremony.id is not None

        await ceremony_reminders._run_once()

        dm_channels = {str(p["channel_id"]) for p in fake.posts}
        assert dm_channels == {f"dm-{active.mattermost_user_id}"}
        assert await _reminder_count(ceremony.id) == 1

    run(scenario())


def test_channel_with_no_members_is_skipped_without_error(fake: FakeMattermost):
    """A ceremony whose channel has no members posts nothing and does not crash."""

    async def scenario() -> None:
        channel = f"{_PREFIX}empty-{int(time_module.time() * 1000)}"
        organizer = await make_person("organizer")
        assert organizer.id is not None

        ceremony = await seed_ceremony(channel, organizer.id, hours_before=1)
        assert ceremony.id is not None

        await ceremony_reminders._run_once()  # must not raise

        assert fake.posts == []
        assert await _reminder_count(ceremony.id) == 0

    run(scenario())


def test_failed_mattermost_post_records_nothing(fake: FakeMattermost):
    """When the DM post fails, no idempotency row is written and the poller survives."""

    async def scenario() -> None:
        channel = f"{_PREFIX}fail-{int(time_module.time() * 1000)}"
        team = f"{_PREFIX}team"
        organizer = await make_person("organizer")
        learner = await make_person("learner")
        assert organizer.id is not None and learner.id is not None
        await add_member(learner, channel, team, RoleKey.LEARNER)

        ceremony = await seed_ceremony(channel, organizer.id, hours_before=24)
        assert ceremony.id is not None
        fake.fail_create_post = True
        await ceremony_reminders._run_once()  # create_post returns None -> logged, no crash
        assert fake.posts == []
        assert await _reminder_count(ceremony.id) == 0

        # On the next pass Mattermost is healthy again: exactly one DM now goes out.
        fake.fail_create_post = False
        await ceremony_reminders._run_once()
        assert len(fake.posts) == 1
        assert await _reminder_count(ceremony.id) == 1

    run(scenario())
