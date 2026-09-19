"""Real-database tests for the back-office service (skipped unless SPRINTFLOW_INTEGRATION_DB=1).

Run against a throwaway database that has the domain schema applied:

    SPRINTFLOW_INTEGRATION_DB=1 APP_ENV=test POSTGRES_DB=<db> ... .venv/bin/python -m pytest -q tests/integration

Identities are seeded straight into ``users`` (no Mattermost); the two REST
lookups ``resolve_person`` needs are replaced with an in-memory fake so the
tests run offline. Every row created here is deleted at the end of the module.
"""

import asyncio
import os
import secrets
from types import MappingProxyType
from typing import Any

import pytest
from sqlalchemy import text

from app.core.langgraph.tools.results import ResultCode
from app.core.requester import (
    RequesterContext,
    current_requester,
)
from app.models.enums import RoleKey
from app.services import back_office
from app.services.authorisation import (
    REFUSAL_MESSAGE,
    AuthorisationRefused,
    ValidationFailed,
)
from app.services.database import database_service
from app.services.domain import channels as channel_repo
from app.services.domain import identity as identity_repo
from app.services.mattermost import mattermost_client

pytestmark = pytest.mark.skipif(
    not os.getenv("SPRINTFLOW_INTEGRATION_DB"), reason="needs a real database (SPRINTFLOW_INTEGRATION_DB=1)"
)

PREFIX = "verify-auth-test-"
STAMP = secrets.token_hex(3)
PROFILES: dict[str, dict[str, Any]] = {}


async def fake_get_user_by_username(username: str) -> dict[str, Any] | None:
    return PROFILES.get(username.strip().lstrip("@").lower())


async def fake_get_user_by_email(email: str) -> dict[str, Any] | None:
    return PROFILES.get(email.strip().lower())


async def seed_user(handle: str, *, is_superadmin: bool = False):
    mattermost_user_id = f"{PREFIX}{handle}-{STAMP}"
    email = f"{mattermost_user_id}@example.test"
    PROFILES[mattermost_user_id] = PROFILES[email] = {
        "id": mattermost_user_id,
        "username": mattermost_user_id,
        "email": email,
    }
    return await identity_repo.upsert_mattermost_user(
        mattermost_user_id=mattermost_user_id,
        username=mattermost_user_id,
        email=email,
        display_name=handle,
        timezone="UTC",
        is_superadmin=is_superadmin,
    )


async def purge() -> None:
    async with database_service.engine.begin() as conn:
        like = {"p": f"{PREFIX}%"}
        await conn.execute(
            text(
                "DELETE FROM onboarding_steps WHERE user_id IN (SELECT id FROM users WHERE mattermost_user_id LIKE :p)"
            ),
            like,
        )
        await conn.execute(
            text("DELETE FROM sprints WHERE channel_id IN (SELECT id FROM channels WHERE lower(name) LIKE :p)"), like
        )
        await conn.execute(
            text(
                "DELETE FROM channel_memberships WHERE channel_id IN (SELECT id FROM channels WHERE lower(name) LIKE :p) "
                "OR user_id IN (SELECT id FROM users WHERE mattermost_user_id LIKE :p)"
            ),
            like,
        )
        await conn.execute(
            text(
                "UPDATE channels SET created_by_id = NULL WHERE created_by_id IN "
                "(SELECT id FROM users WHERE mattermost_user_id LIKE :p)"
            ),
            like,
        )
        await conn.execute(text("DELETE FROM channels WHERE lower(name) LIKE :p"), like)
        await conn.execute(text("DELETE FROM users WHERE mattermost_user_id LIKE :p"), like)


async def counts() -> tuple[int, int, tuple[Any, ...]]:
    async with database_service.engine.connect() as conn:
        channels = (await conn.execute(text("SELECT count(*) FROM channels"))).scalar_one()
        sprints = (await conn.execute(text("SELECT count(*) FROM sprints"))).scalar_one()
        rows = (
            await conn.execute(
                text(
                    "SELECT user_id, channel_id, role_id, status FROM channel_memberships ORDER BY user_id, channel_id"
                )
            )
        ).all()
    return channels, sprints, tuple(tuple(r) for r in rows)


class World:
    """Seeded identities and channels shared by the tests in this module."""

    superadmin: Any
    learner: Any
    scrum_master: Any
    target: Any
    channel_a: Any
    channel_b: Any


@pytest.fixture(scope="module")
def loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.run_until_complete(database_service.close())
    loop.close()


@pytest.fixture(scope="module")
def world(loop):
    original = (mattermost_client.get_user_by_username, mattermost_client.get_user_by_email)
    mattermost_client.get_user_by_username = fake_get_user_by_username  # type: ignore[method-assign]
    mattermost_client.get_user_by_email = fake_get_user_by_email  # type: ignore[method-assign]

    async def build() -> World:
        await purge()
        w = World()
        w.superadmin = await seed_user("super", is_superadmin=True)
        w.learner = await seed_user("learner")
        w.scrum_master = await seed_user("sm")
        w.target = await seed_user("target")
        bind(w.superadmin)
        w.channel_a = (await back_office.create_channel(f"{PREFIX}A-{STAMP}")).channel
        w.channel_b = (await back_office.create_channel(f"{PREFIX}B-{STAMP}")).channel
        await back_office.assign_role(f"@{w.scrum_master.username}", "scrum master", w.channel_a.name)
        await back_office.assign_role(w.learner.email, "learner", str(w.channel_a.id))
        unbind()
        return w

    world = loop.run_until_complete(build())
    yield world
    unbind()
    loop.run_until_complete(purge())
    mattermost_client.get_user_by_username, mattermost_client.get_user_by_email = original  # type: ignore[method-assign]


def bind(user: Any, **overrides: Any) -> None:
    fields: dict[str, Any] = {
        "mattermost_user_id": user.mattermost_user_id,
        "username": user.username,
        "email": user.email,
        "channel_type": "D",
        "user_id": user.id,
        "is_superadmin": user.is_superadmin,
        "channel_roles": MappingProxyType({}),
    }
    fields.update(overrides)
    current_requester.set(RequesterContext(**fields))


def unbind() -> None:
    current_requester.set(None)


def refused(loop, coro) -> None:
    """Assert the coroutine is refused with the fixed sentence and that nothing changed."""
    before = loop.run_until_complete(counts())
    with pytest.raises(AuthorisationRefused) as excinfo:
        loop.run_until_complete(coro)
    assert str(excinfo.value) == REFUSAL_MESSAGE
    assert loop.run_until_complete(counts()) == before


# ---------------------------------------------------------------------------


def test_superadmin_creates_a_channel_and_a_second_create_is_a_no_op(loop, world):
    bind(world.superadmin)
    before = loop.run_until_complete(counts())
    again = loop.run_until_complete(back_office.create_channel(world.channel_a.name.upper()))
    assert again.code == ResultCode.COHORT_ALREADY_EXISTS and again.channel.id == world.channel_a.id
    assert loop.run_until_complete(counts()) == before
    assert isinstance(world.channel_a.id, int) and world.channel_a.created_by_id == world.superadmin.id


def test_learner_is_refused_on_all_three_mutations_and_tables_are_unchanged(loop, world):
    bind(world.learner)
    refused(loop, back_office.create_channel(f"{PREFIX}rogue-{STAMP}"))
    refused(loop, back_office.assign_role(f"@{world.learner.username}", "scrum master", world.channel_a.name))
    refused(loop, back_office.open_sprint(world.channel_a.name, "Sprint 1"))
    role = loop.run_until_complete(channel_repo.get_role_for_user_in_channel(world.learner.id, world.channel_a.id))
    assert role is not None and role.key == RoleKey.LEARNER


def test_forged_context_hints_do_not_grant_authority(loop, world):
    bind(world.learner, is_superadmin=True, channel_roles=MappingProxyType({world.channel_a.id: "scrum_master"}))
    refused(loop, back_office.create_channel(f"{PREFIX}forged-{STAMP}"))
    refused(loop, back_office.assign_role(f"@{world.target.username}", "learner", world.channel_a.name))


def test_scrum_master_of_a_can_assign_in_a_but_is_refused_in_b(loop, world):
    bind(world.scrum_master)
    assigned = loop.run_until_complete(
        back_office.assign_role(f"@{world.target.username}", "learner", world.channel_a.name)
    )
    assert assigned.code == ResultCode.ROLE_ASSIGNED and assigned.membership.assigned_by_id == world.scrum_master.id
    refused(loop, back_office.assign_role(f"@{world.target.username}", "learner", world.channel_b.name))
    refused(loop, back_office.open_sprint(world.channel_b.name, "Sprint 1"))
    refused(loop, back_office.create_channel(f"{PREFIX}sm-{STAMP}"))


def test_assigning_the_same_role_twice_is_a_no_op_and_a_new_role_replaces_it(loop, world):
    bind(world.scrum_master)
    before = loop.run_until_complete(counts())
    same = loop.run_until_complete(
        back_office.assign_role(f"@{world.target.username}", "Learner", world.channel_a.name)
    )
    assert same.code == ResultCode.ROLE_ALREADY_ASSIGNED
    assert loop.run_until_complete(counts()) == before
    changed = loop.run_until_complete(back_office.assign_role(world.target.email, "tech lead", world.channel_a.name))
    assert changed.code == ResultCode.ROLE_CHANGED
    assert changed.previous_role is not None and changed.previous_role.key == RoleKey.LEARNER
    assert "Learner" in changed.message and "Tech Lead" in changed.message
    assert len(loop.run_until_complete(counts())[2]) == len(before[2])


def test_opening_the_same_sprint_twice_is_a_no_op(loop, world):
    bind(world.scrum_master)
    before = loop.run_until_complete(counts())
    opened = loop.run_until_complete(
        back_office.open_sprint(world.channel_a.name, "Sprint 1", "2031-01-06", "2031-01-17")
    )
    assert opened.code == ResultCode.SPRINT_OPENED and opened.sprint.status == "active"
    assert opened.sprint.opened_by_id == world.scrum_master.id
    again = loop.run_until_complete(back_office.open_sprint(str(world.channel_a.id), "sprint 1"))
    assert again.code == ResultCode.SPRINT_ALREADY_OPEN and again.sprint.id == opened.sprint.id
    assert loop.run_until_complete(counts())[1] == before[1] + 1


def test_validation_failures_are_distinct_from_refusals(loop, world):
    bind(world.scrum_master)
    before = loop.run_until_complete(counts())
    with pytest.raises(ValidationFailed, match="Unknown role 'emperor'"):
        loop.run_until_complete(back_office.assign_role(f"@{world.target.username}", "emperor", world.channel_a.name))
    with pytest.raises(ValidationFailed, match="does not exist"):
        loop.run_until_complete(back_office.open_sprint(f"{PREFIX}nope-{STAMP}", "Sprint 9"))
    with pytest.raises(ValidationFailed, match="is before the start date"):
        loop.run_until_complete(back_office.open_sprint(world.channel_a.name, "Sprint 2", "2031-03-10", "2031-03-01"))
    with pytest.raises(ValidationFailed, match="overlaps sprint 'Sprint 1'"):
        loop.run_until_complete(back_office.open_sprint(world.channel_a.name, "Sprint 2", "2031-01-10", "2031-01-20"))
    with pytest.raises(ValidationFailed, match="could not find anyone"):
        loop.run_until_complete(back_office.assign_role("@nobody-at-all", "learner", world.channel_a.name))
    assert loop.run_until_complete(counts()) == before


def test_scoped_reads(loop, world):
    bind(world.learner)
    mine = loop.run_until_complete(back_office.list_channel_roles_for_requester())
    assert [c.id for c, _ in mine.entries] == [world.channel_a.id]
    assert mine.entries[0][1] is not None and mine.entries[0][1].key == RoleKey.LEARNER
    members = loop.run_until_complete(back_office.list_channel_members(world.channel_a.name))
    assert {m.user.id for m in members.members} >= {world.learner.id, world.scrum_master.id, world.target.id}
    refused(loop, back_office.list_channel_members(world.channel_b.name))
    bind(world.superadmin)
    everything = loop.run_until_complete(back_office.list_channel_roles_for_requester())
    assert everything.is_superadmin and {world.channel_a.id, world.channel_b.id} <= {
        c.id for c, _ in everything.entries
    }


def test_inactive_channel_is_a_validation_failure_for_an_authorised_requester(loop, world):
    bind(world.superadmin)
    loop.run_until_complete(channel_repo.set_channel_active(world.channel_b.id, False))
    with pytest.raises(ValidationFailed, match="inactive"):
        loop.run_until_complete(back_office.open_sprint(world.channel_b.name, "Sprint 1"))
    loop.run_until_complete(channel_repo.set_channel_active(world.channel_b.id, True))


# --- Review fix (2026-09-03): re-activating an inactive membership is a change, not a no-op -------


def test_reassigning_an_inactive_membership_reactivates_and_says_so(loop, world):
    from app.models.enums import MembershipStatus
    from app.services.domain import channels as channel_repo

    bind(world.superadmin)
    assert world.learner.id is not None and world.channel_a.id is not None
    loop.run_until_complete(
        channel_repo.set_membership_status(world.learner.id, world.channel_a.id, MembershipStatus.INACTIVE)
    )
    result = loop.run_until_complete(back_office.assign_role(world.learner.email, "learner", world.channel_a.name))
    assert result.code == ResultCode.ROLE_ASSIGNED
    assert "Re-added" in result.message
    membership = loop.run_until_complete(channel_repo.get_membership(world.learner.id, world.channel_a.id))
    assert membership is not None and membership.status == MembershipStatus.ACTIVE.value
    again = loop.run_until_complete(back_office.assign_role(world.learner.email, "learner", world.channel_a.name))
    assert again.code == ResultCode.ROLE_ALREADY_ASSIGNED
    unbind()
