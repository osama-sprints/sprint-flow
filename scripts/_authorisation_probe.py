"""In-container authorisation probe: one PASS/FAIL line per assertion, non-zero exit on failure.

Driven by the host-side ``scripts/verify_authorisation.py`` (which pipes this
file over stdin into the ai-core container), or run directly against any
database that has the domain schema:

    docker compose exec -T ai-core /app/.venv/bin/python - < scripts/_authorisation_probe.py
    (cd ai-core && PYTHONPATH=. .venv/bin/python ../scripts/_authorisation_probe.py)

It seeds test identities straight into ``users`` (fake Mattermost ids prefixed
``verify-auth-``), binds ``current_requester`` exactly as the conversation
layer does — including the channel the turn arrived in — and drives both the
SERVICE functions and the TOOL wrappers. Every refusal is paired with a
row-count/row-content snapshot proving nothing was written. Mattermost is
never called: the two profile lookups ``resolve_person`` needs are replaced
with an in-memory fake for the seeded identities.

This probe targets the post-refactor **channels** architecture: a channel is a
plain Mattermost channel id (no local ``channels`` table), roles live in
``channel_roles``, authority is decided from stored rows per channel, and the
back-office tools derive the channel from ``current_requester`` rather than
taking a ``cohort`` argument. All rows the probe creates carry the
``verify-auth-`` prefix and are deleted at the end, even when an assertion
fails.
"""

import asyncio
import json
import os
import secrets
import sys
from types import MappingProxyType
from typing import Any

for _candidate in ("/app", os.path.join(os.getcwd(), "ai-core"), os.getcwd()):
    if os.path.isdir(os.path.join(_candidate, "app")):
        sys.path.insert(0, _candidate)
        break

from sqlalchemy import text  # noqa: E402

from app.core.langgraph.tools import back_office as back_office_tools  # noqa: E402
from app.core.langgraph.tools.results import (  # noqa: E402
    ResultCode,
    result_code_of,
)
from app.core.requester import (  # noqa: E402
    RequesterContext,
    current_requester,
)
from app.models.enums import RoleKey  # noqa: E402
from app.services import back_office  # noqa: E402
from app.services.authorisation import (  # noqa: E402
    REFUSAL_MESSAGE,
    AuthorisationRefused,
    ValidationFailed,
)
from app.services.database import database_service  # noqa: E402
from app.services.domain import channels as channel_repo  # noqa: E402
from app.services.domain import identity as identity_repo  # noqa: E402
from app.services.mattermost import mattermost_client  # noqa: E402

PREFIX = "verify-auth-"
STAMP = secrets.token_hex(3)
TEAM_ID = f"{PREFIX}team-{STAMP}"
CHANNEL_A = f"{PREFIX}channel-a-{STAMP}"
CHANNEL_B = f"{PREFIX}channel-b-{STAMP}"
REFUSED = f"[{ResultCode.AUTHORISATION_REFUSED}] {REFUSAL_MESSAGE}"

results: list[bool] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    """Record and print one assertion."""
    results.append(ok)
    suffix = f"  ({detail})" if detail and not ok else ""
    print(f"  {label:70} {'PASS' if ok else 'FAIL'}{suffix}", flush=True)


# ---------------------------------------------------------------------------
# Fake Mattermost directory for the seeded identities (resolve_person offline)
# ---------------------------------------------------------------------------

PROFILES: dict[str, dict[str, Any]] = {}


def fake_profile(mattermost_user_id: str, username: str, email: str) -> dict[str, Any]:
    """Register and return a minimal Mattermost profile."""
    profile = {"id": mattermost_user_id, "username": username, "email": email}
    PROFILES[username.lower()] = profile
    PROFILES[email.lower()] = profile
    return profile


async def fake_get_user_by_username(username: str) -> dict[str, Any] | None:
    """Stand-in for the Mattermost REST lookup."""
    return PROFILES.get(username.strip().lstrip("@").lower())


async def fake_get_user_by_email(email: str) -> dict[str, Any] | None:
    """Stand-in for the Mattermost REST lookup."""
    return PROFILES.get(email.strip().lower())


# ---------------------------------------------------------------------------
# Helpers — channels architecture
# ---------------------------------------------------------------------------


def bind(user: Any, channel_id: str, **overrides: Any) -> None:
    """Bind a requester the way the conversation layer does (channel included)."""
    fields: dict[str, Any] = {
        "mattermost_user_id": user.mattermost_user_id,
        "username": user.username,
        "email": user.email,
        "channel_id": channel_id,
        "team_id": TEAM_ID,
        "channel_type": "O",
        "user_id": user.id,
        "is_superadmin": user.is_superadmin,
        "channel_roles": MappingProxyType({}),
    }
    fields.update(overrides)
    current_requester.set(RequesterContext(**fields))


def unbind() -> None:
    """Clear the requester, as the conversation layer does after every turn."""
    current_requester.set(None)


async def snapshot() -> tuple[Any, ...]:
    """Everything a refused action could have touched, limited to probe rows."""
    async with database_service.engine.connect() as conn:
        sprints = (
            await conn.execute(
                text("SELECT count(*) FROM sprints WHERE channel_id LIKE :p"), {"p": f"{PREFIX}%"}
            )
        ).scalar_one()
        roles = (
            await conn.execute(
                text(
                    "SELECT user_id, channel_id, role_id, status FROM channel_roles "
                    "WHERE channel_id LIKE :p "
                    "OR user_id IN (SELECT id FROM users WHERE mattermost_user_id LIKE :p) "
                    "ORDER BY user_id, channel_id"
                ),
                {"p": f"{PREFIX}%"},
            )
        ).all()
    return sprints, tuple(tuple(row) for row in roles)


async def cleanup() -> None:
    """Delete every row this (or an earlier, crashed) probe run created, in dependency order."""
    async with database_service.engine.begin() as conn:
        await conn.execute(
            text(
                "DELETE FROM sprints WHERE channel_id LIKE :p "
                "OR opened_by_id IN (SELECT id FROM users WHERE mattermost_user_id LIKE :p)"
            ),
            {"p": f"{PREFIX}%"},
        )
        await conn.execute(
            text(
                "DELETE FROM channel_roles WHERE channel_id LIKE :p "
                "OR user_id IN (SELECT id FROM users WHERE mattermost_user_id LIKE :p)"
            ),
            {"p": f"{PREFIX}%"},
        )
        await conn.execute(text("DELETE FROM users WHERE mattermost_user_id LIKE :p"), {"p": f"{PREFIX}%"})


async def seed_user(handle: str, *, is_superadmin: bool = False) -> Any:
    """Insert one test identity directly into ``users`` and register its fake profile."""
    mattermost_user_id = f"{PREFIX}{handle}-{STAMP}"
    username = f"{PREFIX}{handle}-{STAMP}"
    email = f"{username}@example.test"
    fake_profile(mattermost_user_id, username, email)
    return await identity_repo.upsert_mattermost_user(
        mattermost_user_id=mattermost_user_id,
        username=username,
        email=email,
        display_name=handle.title(),
        timezone="UTC",
        is_superadmin=is_superadmin,
    )


async def stored_role(user: Any, channel_id: str) -> RoleKey | None:
    """The requester's stored role in the channel, per the database (what authority uses)."""
    role = await channel_repo.get_role_for_user_in_channel(user.id, channel_id, active_only=True)
    return role.key if role else None


async def grant_role(user: Any, channel_id: str, role_key: RoleKey) -> None:
    """Give ``user`` a stored role in ``channel_id`` (what the seeded happy path uses)."""
    role = await channel_repo.get_role_by_key(role_key)
    assert role is not None and role.id is not None and user.id is not None
    await channel_repo.upsert_channel_role(
        user_id=user.id,
        team_id=TEAM_ID,
        channel_id=channel_id,
        role_id=role.id,
        assigned_by_id=None,
    )


async def expect_refusal(label: str, coro: Any) -> None:
    """Assert a SERVICE call raises the refusal exception with the fixed sentence and no side effects."""
    before = await snapshot()
    try:
        await coro
    except AuthorisationRefused as exc:
        check(f"{label}: refused (AuthorisationRefused)", str(exc) == REFUSAL_MESSAGE, str(exc))
    except Exception as exc:  # noqa: BLE001 - the probe must report, not crash
        check(f"{label}: refused (AuthorisationRefused)", False, f"{type(exc).__name__}: {exc}")
    else:
        check(f"{label}: refused (AuthorisationRefused)", False, "call succeeded")
    check(f"{label}: nothing was written", before == await snapshot())


async def expect_tool_refusal(label: str, tool: Any, args: dict[str, Any]) -> str:
    """Assert a TOOL call returns exactly the refusal result and changes nothing."""
    before = await snapshot()
    result = await tool.ainvoke(args)
    check(f"{label}: tool result is exactly the refusal", result == REFUSED, result)
    check(f"{label}: nothing was written", before == await snapshot())
    return result


# ---------------------------------------------------------------------------
# The scenario
# ---------------------------------------------------------------------------


async def scenario() -> None:
    """Run every assertion."""
    mattermost_client.get_user_by_username = fake_get_user_by_username  # type: ignore[method-assign]
    mattermost_client.get_user_by_email = fake_get_user_by_email  # type: ignore[method-assign]

    superadmin = await seed_user("super", is_superadmin=True)
    learner = await seed_user("learner")
    scrum_master = await seed_user("sm")
    target = await seed_user("target")

    print("--- tool boundary")
    tool_names = {tool.name for tool in back_office_tools.TOOLS}
    contract = {"assign_role", "open_sprint", "list_channel_roles_for_requester", "list_channel_members"}
    check(
        "the four channel-administration tools are exposed",
        contract <= tool_names,
        str(sorted(tool_names)),
    )
    forbidden = ("requester", "user_id", "mattermost_user", "superadmin", "identity")
    leaks = [
        f"{tool.name}.{arg}"
        for tool in back_office_tools.TOOLS
        if tool.name in contract
        for arg in tool.args
        if any(f in arg for f in forbidden)
    ]
    check("no tool argument carries requester identity or a channel", not leaks, str(leaks))

    print("--- nobody bound (a tool invoked outside a turn)")
    unbind()
    await expect_tool_refusal(
        "unbound: assign_role", back_office_tools.assign_role, {"person": f"@{PREFIX}x-{STAMP}", "role": "learner"}
    )
    await expect_tool_refusal("unbound: open_sprint", back_office_tools.open_sprint, {"sprint_name": "Sprint 1"})
    await expect_tool_refusal("unbound: list_channel_members", back_office_tools.list_channel_members, {})

    print("--- superadmin: authorised platform actions (channel context required)")
    bind(superadmin, CHANNEL_A)
    before = await snapshot()
    sm_assign = await back_office.assign_role(f"@{scrum_master.username}", "scrum master")
    check("superadmin assigns scrum master in A", sm_assign.code == ResultCode.ROLE_ASSIGNED, sm_assign.message)
    check(
        "scrum master's role is stored under channel A",
        await stored_role(scrum_master, CHANNEL_A) == RoleKey.SCRUM_MASTER,
    )
    learner_assign = await back_office.assign_role(learner.email or "", "learner")
    check("superadmin assigns learner in A by email", learner_assign.code == ResultCode.ROLE_ASSIGNED)
    check(
        "learner's role is stored under channel A",
        await stored_role(learner, CHANNEL_A) == RoleKey.LEARNER,
    )
    bind(superadmin, CHANNEL_B)
    learner_in_b = await back_office.assign_role(f"@{learner.username}", "learner")
    check("superadmin assigns learner in B", learner_in_b.code == ResultCode.ROLE_ASSIGNED)
    check("learner's role is stored under channel B", await stored_role(learner, CHANNEL_B) == RoleKey.LEARNER)
    bind(superadmin, CHANNEL_A)
    after_roles = await snapshot()
    check("exactly three channel_roles rows were added by the three assignments", len(after_roles[1]) == len(before[1]) + 3)
    opened = await back_office.open_sprint("Sprint 1", "2030-01-06", "2030-01-17")
    check("superadmin opens Sprint 1 in A", opened.code == ResultCode.SPRINT_OPENED, opened.message)
    check(
        "sprint stored as active with opened_by",
        opened.sprint.status == "active" and opened.sprint.opened_by_id == superadmin.id,
    )
    check("exactly one sprint row was added", (await snapshot())[0] == before[0] + 1)

    print("--- learner: refused on every mutation, tables unchanged")
    bind(learner, CHANNEL_A)
    await expect_refusal("learner: assign_role (service)", back_office.assign_role(f"@{learner.username}", "scrum master"))
    await expect_refusal("learner: open_sprint (service)", back_office.open_sprint("Sprint 1"))
    await expect_tool_refusal(
        "learner: assign_role (tool)",
        back_office_tools.assign_role,
        {"person": f"@{target.username}", "role": "scrum master"},
    )
    await expect_tool_refusal(
        "learner: open_sprint (tool)", back_office_tools.open_sprint, {"sprint_name": "Sprint 1"}
    )
    await expect_tool_refusal(
        "learner: unknown role is refused, not validated (authorisation first)",
        back_office_tools.assign_role,
        {"person": f"@{target.username}", "role": "emperor"},
    )
    check("learner is still a learner in A afterwards", await stored_role(learner, CHANNEL_A) == RoleKey.LEARNER)

    print("--- forged context hints do not grant authority (stored data decides)")
    bind(
        learner,
        CHANNEL_A,
        is_superadmin=True,
        channel_roles=MappingProxyType({CHANNEL_A: "scrum_master"}),
    )
    await expect_tool_refusal(
        "learner with forged is_superadmin: assign_role",
        back_office_tools.assign_role,
        {"person": f"@{target.username}", "role": "learner"},
    )
    await expect_tool_refusal(
        "learner with forged channel_roles: open_sprint",
        back_office_tools.open_sprint,
        {"sprint_name": "Sprint 1"},
    )

    print("--- scrum master of A: channel-scoped authority")
    bind(scrum_master, CHANNEL_A)
    assigned = await back_office_tools.assign_role.ainvoke(
        {"person": f"@{target.username}", "role": "learner"}
    )
    check("scrum master assigns a learner in A", result_code_of(assigned) == ResultCode.ROLE_ASSIGNED, assigned)
    check("target now holds learner in A", await stored_role(target, CHANNEL_A) == RoleKey.LEARNER)
    bind(scrum_master, CHANNEL_B)
    await expect_tool_refusal("scrum master of A: assign_role in B", back_office_tools.assign_role, {
        "person": f"@{target.username}", "role": "learner"
    })
    await expect_tool_refusal(
        "scrum master of A: open_sprint in B", back_office_tools.open_sprint, {"sprint_name": "Sprint 1"}
    )
    await expect_tool_refusal("scrum master of A: list B's members", back_office_tools.list_channel_members, {})

    print("--- idempotency")
    bind(scrum_master, CHANNEL_A)
    before = await snapshot()
    same = await back_office.assign_role(f"@{target.username}", "learner")
    check("same role twice -> ROLE_ALREADY_ASSIGNED", same.code == ResultCode.ROLE_ALREADY_ASSIGNED, same.message)
    check("channel_roles rows unchanged", before == await snapshot())
    changed = await back_office.assign_role(f"@{target.username}", "tech lead")
    check(
        "different role -> ROLE_CHANGED naming the previous role",
        changed.code == ResultCode.ROLE_CHANGED
        and changed.previous_role is not None
        and changed.previous_role.key == RoleKey.LEARNER
        and "Learner" in changed.message,
        changed.message,
    )
    check("still one channel_roles row per (user, channel)", len((await snapshot())[1]) == len(before[1]))
    reopened = await back_office_tools.open_sprint.ainvoke({"sprint_name": "sprint 1"})
    check(
        "opening Sprint 1 again -> SPRINT_ALREADY_OPEN",
        result_code_of(reopened) == ResultCode.SPRINT_ALREADY_OPEN,
        reopened,
    )
    check("exactly one sprint row still exists", (await snapshot())[0] == before[0])

    print("--- validation failures are distinct from refusals")
    bind(scrum_master, CHANNEL_A)
    unknown_role = await back_office_tools.assign_role.ainvoke(
        {"person": f"@{target.username}", "role": "emperor"}
    )
    check(
        "unknown role -> VALIDATION_ERROR naming the known roles",
        result_code_of(unknown_role) == ResultCode.VALIDATION_ERROR and "Known roles" in unknown_role,
        unknown_role,
    )
    bind(scrum_master, "")
    outside = await back_office_tools.assign_role.ainvoke(
        {"person": f"@{target.username}", "role": "learner"}
    )
    check(
        "no channel context -> VALIDATION_ERROR (not a refusal)",
        result_code_of(outside) == ResultCode.VALIDATION_ERROR and "outside of a channel context" in outside,
        outside,
    )
    bind(scrum_master, CHANNEL_A)
    unknown_person = await back_office_tools.assign_role.ainvoke({"person": "@nobody-here", "role": "learner"})
    check(
        "unknown person -> VALIDATION_ERROR",
        result_code_of(unknown_person) == ResultCode.VALIDATION_ERROR,
        unknown_person,
    )
    bad_dates = await back_office_tools.open_sprint.ainvoke(
        {"sprint_name": "Sprint 2", "start_date": "2030-03-10", "end_date": "2030-03-01"}
    )
    check("end before start -> VALIDATION_ERROR", result_code_of(bad_dates) == ResultCode.VALIDATION_ERROR, bad_dates)
    overlap = await back_office_tools.open_sprint.ainvoke(
        {"sprint_name": "Sprint 2", "start_date": "2030-01-10", "end_date": "2030-01-20"}
    )
    check(
        "overlapping sprint -> VALIDATION_ERROR naming the clash",
        result_code_of(overlap) == ResultCode.VALIDATION_ERROR and "Sprint 1" in overlap,
        overlap,
    )
    empty_name = await back_office_tools.open_sprint.ainvoke({"sprint_name": "   "})
    check("empty sprint name -> VALIDATION_ERROR", result_code_of(empty_name) == ResultCode.VALIDATION_ERROR, empty_name)
    check("validation and refusal codes differ", ResultCode.VALIDATION_ERROR != ResultCode.AUTHORISATION_REFUSED)
    try:
        await back_office.assign_role(f"@{target.username}", "emperor")
        distinct = False
    except ValidationFailed:
        distinct = True
    except AuthorisationRefused:
        distinct = False
    check("service raises ValidationFailed (not AuthorisationRefused) for an unknown role", distinct)
    check("no sprint was created by the failed attempts", (await snapshot())[0] == before[0])

    print("--- scoped reads")
    bind(learner, CHANNEL_A)
    mine = await back_office.list_channel_roles_for_requester()
    check(
        "learner lists only their own channels (A and B)",
        {channel_id for channel_id, _ in mine.entries} == {CHANNEL_A, CHANNEL_B} and not mine.is_superadmin,
        mine.message,
    )
    members = await back_office_tools.list_channel_members.ainvoke({})
    check(
        "learner in A can list A's members",
        result_code_of(members) == ResultCode.OK and scrum_master.username in members,
        members,
    )
    bind(superadmin, CHANNEL_A)
    everything = await back_office.list_channel_roles_for_requester()
    check(
        "superadmin's listing reports platform-wide authority",
        everything.is_superadmin and "superadmin" in everything.message.lower(),
        everything.message,
    )
    unbind()


async def main_async() -> None:
    """Run the scenario with guaranteed cleanup."""
    await cleanup()
    try:
        await scenario()
    finally:
        unbind()
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


# ---------------------------------------------------------------------------
# Command mode, used by the host verifier's channel-scoped live case
# ---------------------------------------------------------------------------


async def command_setup_scoped(mattermost_user_id: str, username: str, email: str, channel_id: str) -> dict[str, Any]:
    """Give a REAL Mattermost account tech-lead authority in ONE real channel (stored row)."""
    user = await identity_repo.upsert_mattermost_user(
        mattermost_user_id=mattermost_user_id,
        username=username,
        email=email.lower(),
        display_name=username,
        timezone="UTC",
        is_superadmin=False,
    )
    assert user.id is not None
    lead = await channel_repo.get_role_by_key(RoleKey.TECH_LEAD)
    assert lead is not None and lead.id is not None
    await channel_repo.upsert_channel_role(
        user_id=user.id,
        team_id="",
        channel_id=channel_id,
        role_id=lead.id,
        assigned_by_id=None,
    )
    return {"user_id": user.id}


async def command_check_scoped(channel_id: str) -> dict[str, Any]:
    """Count sprints in the given channel."""
    async with database_service.engine.connect() as conn:
        runs = (await conn.execute(text("SELECT count(*) FROM sprints WHERE channel_id = :c"), {"c": channel_id})).scalar_one()
    return {"sprints": runs}


async def command_cleanup_scoped(mattermost_user_id: str, channel_id: str) -> dict[str, Any]:
    """Remove the real account's rows and anything created under the live channel."""
    await cleanup()
    async with database_service.engine.begin() as conn:
        await conn.execute(text("DELETE FROM sprints WHERE channel_id = :c"), {"c": channel_id})
        await conn.execute(
            text(
                "DELETE FROM channel_roles WHERE user_id IN "
                "(SELECT id FROM users WHERE mattermost_user_id = :m) OR channel_id = :c"
            ),
            {"m": mattermost_user_id, "c": channel_id},
        )
        await conn.execute(text("DELETE FROM users WHERE mattermost_user_id = :m"), {"m": mattermost_user_id})
    return {"deleted": True}


async def command(argv: list[str]) -> int:
    """Run one command and print a single JSON line (the host verifier parses the last JSON line)."""
    try:
        if argv[0] == "setup-scoped":
            print(json.dumps(await command_setup_scoped(argv[1], argv[2], argv[3], argv[4])))
        elif argv[0] == "check-scoped":
            print(json.dumps(await command_check_scoped(argv[1])))
        elif argv[0] == "cleanup-scoped":
            print(json.dumps(await command_cleanup_scoped(argv[1], argv[2])))
        else:
            print(json.dumps({"error": f"unknown command {argv[0]}"}))
            return 2
        return 0
    finally:
        await database_service.close()


def main() -> int:
    """Run every check.

    Returns:
        int: 0 when all pass.
    """
    print("=" * 84)
    print("SprintFlow authorisation & back office — in-container probe (channels era)")
    print("=" * 84)
    try:
        asyncio.run(main_async())
    except Exception as exc:  # noqa: BLE001 - report, never hide, a crash
        check(f"probe crashed: {type(exc).__name__}", False, str(exc)[:300])
    print("=" * 84)
    passed = sum(results)
    print(f"{passed}/{len(results)} checks passed")
    print("AUTHORISATION PROBE OK" if all(results) else "AUTHORISATION PROBE FAILED")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(command(sys.argv[1:])) if len(sys.argv) > 1 else main())