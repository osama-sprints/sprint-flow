"""In-container authorisation probe: one PASS/FAIL line per assertion, non-zero exit on failure.

Driven by the host-side ``scripts/verify_authorisation.py`` (which pipes this
file over stdin into the ai-core container), or run directly against any
database that has the domain schema:

    docker compose exec -T ai-core /app/.venv/bin/python - < scripts/_authorisation_probe.py
    (cd ai-core && PYTHONPATH=. .venv/bin/python ../scripts/_authorisation_probe.py)

It seeds test identities straight into ``users`` (fake Mattermost ids prefixed
``verify-auth-``), binds ``current_requester`` exactly as the conversation
layer does, and drives both the SERVICE functions and the TOOL wrappers. Every
refusal is paired with a row-count/row-content snapshot proving nothing was
written. Mattermost is never called: the two profile lookups ``resolve_person``
needs are replaced with an in-memory fake for the seeded identities. All rows
the probe creates are deleted at the end, even when an assertion fails.
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
from app.services.domain import cohorts as cohort_repo  # noqa: E402
from app.services.domain import identity as identity_repo  # noqa: E402
from app.services.domain import sprints as sprint_repo  # noqa: E402
from app.services.mattermost import mattermost_client  # noqa: E402

PREFIX = "verify-auth-"
STAMP = secrets.token_hex(3)
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
# Helpers
# ---------------------------------------------------------------------------


def bind(user: Any, **overrides: Any) -> None:
    """Bind a requester the way the conversation layer does (from the stored row)."""
    fields: dict[str, Any] = {
        "mattermost_user_id": user.mattermost_user_id,
        "username": user.username,
        "email": user.email,
        "channel_type": "D",
        "user_id": user.id,
        "is_superadmin": user.is_superadmin,
        "cohort_roles": MappingProxyType({}),
    }
    fields.update(overrides)
    current_requester.set(RequesterContext(**fields))


def unbind() -> None:
    """Clear the requester, as the conversation layer does after every turn."""
    current_requester.set(None)


async def snapshot() -> tuple[Any, ...]:
    """Everything a refused action could have touched, as comparable values."""
    async with database_service.engine.connect() as conn:
        cohorts = (await conn.execute(text("SELECT count(*) FROM cohorts"))).scalar_one()
        sprints = (await conn.execute(text("SELECT count(*) FROM sprints"))).scalar_one()
        memberships = (
            await conn.execute(
                text("SELECT user_id, cohort_id, role_id, status FROM cohort_memberships ORDER BY user_id, cohort_id")
            )
        ).all()
        steps = (await conn.execute(text("SELECT count(*) FROM onboarding_steps"))).scalar_one()
    return cohorts, sprints, tuple(tuple(row) for row in memberships), steps


async def cleanup() -> None:
    """Delete every row this (or an earlier, crashed) probe run created, in dependency order."""
    async with database_service.engine.begin() as conn:
        await conn.execute(
            text(
                "DELETE FROM onboarding_steps WHERE user_id IN (SELECT id FROM users WHERE mattermost_user_id LIKE :p)"
            ),
            {"p": f"{PREFIX}%"},
        )
        await conn.execute(
            text("DELETE FROM sprints WHERE cohort_id IN (SELECT id FROM cohorts WHERE lower(name) LIKE :p)"),
            {"p": f"{PREFIX}%"},
        )
        await conn.execute(
            text(
                "DELETE FROM cohort_memberships WHERE cohort_id IN "
                "(SELECT id FROM cohorts WHERE lower(name) LIKE :p) "
                "OR user_id IN (SELECT id FROM users WHERE mattermost_user_id LIKE :p)"
            ),
            {"p": f"{PREFIX}%"},
        )
        await conn.execute(
            text(
                "UPDATE cohorts SET created_by_id = NULL WHERE created_by_id IN "
                "(SELECT id FROM users WHERE mattermost_user_id LIKE :p)"
            ),
            {"p": f"{PREFIX}%"},
        )
        await conn.execute(text("DELETE FROM cohorts WHERE lower(name) LIKE :p"), {"p": f"{PREFIX}%"})
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
    name_a = f"Verify-Auth-A-{STAMP}"
    name_b = f"Verify-Auth-B-{STAMP}"

    print("--- tool boundary")
    tool_names = {tool.name for tool in back_office_tools.TOOLS}
    check(
        "five tools with the contract names",
        tool_names == {"create_cohort", "assign_role", "open_sprint", "list_cohorts", "list_cohort_members"},
        str(sorted(tool_names)),
    )
    forbidden = ("requester", "user_id", "mattermost_user", "superadmin", "identity", "role_key")
    leaks = [
        f"{tool.name}.{arg}"
        for tool in back_office_tools.TOOLS
        for arg in tool.args
        if any(f in arg for f in forbidden)
    ]
    check("no tool argument carries requester identity", not leaks, str(leaks))

    print("--- nobody bound (a tool invoked outside a turn)")
    unbind()
    await expect_tool_refusal("unbound: create_cohort", back_office_tools.create_cohort, {"name": name_a})
    await expect_tool_refusal("unbound: list_cohorts", back_office_tools.list_cohorts, {})

    print("--- superadmin: authorised platform actions")
    bind(superadmin)
    before = await snapshot()
    created = await back_office.create_cohort(name_a)
    check("superadmin creates cohort A", created.code == ResultCode.COHORT_CREATED, created.message)
    check("cohort A has an integer id", isinstance(created.cohort.id, int))
    again = await back_office.create_cohort(name_a.lower())
    check(
        "creating A again (different case) is a no-op",
        again.code == ResultCode.COHORT_ALREADY_EXISTS and again.cohort.id == created.cohort.id,
        again.message,
    )
    after = await snapshot()
    check("exactly one cohort row was added by the two calls", after[0] == before[0] + 1)
    tool_again = await back_office_tools.create_cohort.ainvoke({"name": name_a})
    check(
        "create_cohort tool reports COHORT_ALREADY_EXISTS",
        result_code_of(tool_again) == ResultCode.COHORT_ALREADY_EXISTS,
        tool_again,
    )
    check("cohort count unchanged after the tool repeat", (await snapshot())[0] == after[0])
    linked = await back_office.create_cohort(name_b, mattermost_team="abcdefghijklmnopqrstuvwxyz")
    check(
        "cohort B created with an id-shaped Mattermost team stored",
        linked.code == ResultCode.COHORT_CREATED and linked.cohort.mattermost_team_id == "abcdefghijklmnopqrstuvwxyz",
        linked.message,
    )
    cohort_a, cohort_b = created.cohort, linked.cohort
    assert cohort_a.id is not None and cohort_b.id is not None

    sm_assign = await back_office.assign_role(f"@{scrum_master.username}", "Scrum Master", name_a)
    check("superadmin assigns scrum master in A", sm_assign.code == ResultCode.ROLE_ASSIGNED, sm_assign.message)
    learner_assign = await back_office.assign_role(learner.email or "", "learner", str(cohort_a.id))
    check(
        "superadmin assigns learner in A by email and cohort id",
        learner_assign.code == ResultCode.ROLE_ASSIGNED,
        learner_assign.message,
    )
    learner_in_b = await back_office.assign_role(f"@{learner.username}", "student", name_b)
    check("superadmin assigns learner in B via alias 'student'", learner_in_b.code == ResultCode.ROLE_ASSIGNED)

    print("--- learner: refused on every mutation, tables unchanged")
    bind(learner)
    await expect_refusal("learner: create_cohort (service)", back_office.create_cohort(f"{PREFIX}rogue-{STAMP}"))
    await expect_refusal(
        "learner: promote self to scrum master in A (service)",
        back_office.assign_role(f"@{learner.username}", "scrum master", name_a),
    )
    await expect_refusal("learner: open_sprint in A (service)", back_office.open_sprint(name_a, "Sprint 1"))
    await expect_tool_refusal(
        "learner: create_cohort (tool)", back_office_tools.create_cohort, {"name": f"{PREFIX}rogue-{STAMP}"}
    )
    await expect_tool_refusal(
        "learner: assign_role (tool)",
        back_office_tools.assign_role,
        {"person": f"@{learner.username}", "role": "scrum master", "cohort": name_a},
    )
    await expect_tool_refusal(
        "learner: open_sprint (tool)", back_office_tools.open_sprint, {"cohort": name_a, "sprint_name": "Sprint 1"}
    )
    still = await cohort_repo.get_role_for_user_in_cohort(learner.id, cohort_a.id)
    check("learner is still a learner in A afterwards", bool(still) and still.key == RoleKey.LEARNER)
    await expect_tool_refusal(
        "learner: unknown role is refused, not validated (authorisation first)",
        back_office_tools.assign_role,
        {"person": f"@{learner.username}", "role": "emperor", "cohort": name_a},
    )

    print("--- forged context hints do not grant authority (stored data decides)")
    bind(learner, is_superadmin=True, cohort_roles=MappingProxyType({cohort_a.id: "scrum_master"}))
    await expect_tool_refusal(
        "learner with forged is_superadmin: create_cohort",
        back_office_tools.create_cohort,
        {"name": f"{PREFIX}forged-{STAMP}"},
    )
    await expect_tool_refusal(
        "learner with forged cohort_roles: assign_role in A",
        back_office_tools.assign_role,
        {"person": f"@{target.username}", "role": "learner", "cohort": name_a},
    )

    print("--- scrum master of A: cohort-scoped authority")
    bind(scrum_master)
    await expect_tool_refusal(
        "scrum master of A: create_cohort (platform-level)",
        back_office_tools.create_cohort,
        {"name": f"{PREFIX}sm-{STAMP}"},
    )
    assigned = await back_office_tools.assign_role.ainvoke(
        {"person": f"@{target.username}", "role": "learner", "cohort": name_a}
    )
    check("scrum master assigns a learner in A", result_code_of(assigned) == ResultCode.ROLE_ASSIGNED, assigned)
    await expect_tool_refusal(
        "scrum master of A: assign_role in B",
        back_office_tools.assign_role,
        {"person": f"@{target.username}", "role": "learner", "cohort": name_b},
    )
    await expect_tool_refusal(
        "scrum master of A: open_sprint in B",
        back_office_tools.open_sprint,
        {"cohort": name_b, "sprint_name": "Sprint 1"},
    )
    await expect_tool_refusal(
        "scrum master of A: list_cohort_members of B",
        back_office_tools.list_cohort_members,
        {"cohort": name_b},
    )

    print("--- idempotency")
    before = await snapshot()
    same = await back_office.assign_role(f"@{target.username}", "learner", name_a)
    check("same role twice -> ROLE_ALREADY_ASSIGNED", same.code == ResultCode.ROLE_ALREADY_ASSIGNED, same.message)
    check("membership rows unchanged", before == await snapshot())
    changed = await back_office.assign_role(f"@{target.username}", "tech lead", name_a)
    check(
        "different role -> ROLE_CHANGED naming the previous role",
        changed.code == ResultCode.ROLE_CHANGED
        and changed.previous_role is not None
        and changed.previous_role.key == RoleKey.LEARNER
        and "Learner" in changed.message,
        changed.message,
    )
    check("still one membership row per (user, cohort)", (await snapshot())[2].__len__() == len(before[2]))
    before = await snapshot()
    opened = await back_office.open_sprint(name_a, "Sprint 1", "2030-01-06", "2030-01-17")
    check("scrum master opens Sprint 1 in A", opened.code == ResultCode.SPRINT_OPENED, opened.message)
    check(
        "sprint stored as active with opened_by",
        opened.sprint.status == "active" and opened.sprint.opened_by_id == scrum_master.id,
    )
    reopened = await back_office_tools.open_sprint.ainvoke({"cohort": name_a, "sprint_name": "sprint 1"})
    check(
        "opening Sprint 1 again -> SPRINT_ALREADY_OPEN",
        result_code_of(reopened) == ResultCode.SPRINT_ALREADY_OPEN,
        reopened,
    )
    check("exactly one sprint row was added", (await snapshot())[1] == before[1] + 1)

    print("--- validation failures are distinct from refusals")
    unknown_role = await back_office_tools.assign_role.ainvoke(
        {"person": f"@{target.username}", "role": "emperor", "cohort": name_a}
    )
    check(
        "unknown role -> VALIDATION_ERROR naming the known roles",
        result_code_of(unknown_role) == ResultCode.VALIDATION_ERROR and "Known roles" in unknown_role,
        unknown_role,
    )
    unknown_cohort = await back_office_tools.open_sprint.ainvoke(
        {"cohort": f"{PREFIX}nope-{STAMP}", "sprint_name": "S"}
    )
    check(
        "unknown cohort -> VALIDATION_ERROR",
        result_code_of(unknown_cohort) == ResultCode.VALIDATION_ERROR,
        unknown_cohort,
    )
    bad_dates = await back_office_tools.open_sprint.ainvoke(
        {"cohort": name_a, "sprint_name": "Sprint 2", "start_date": "2030-03-10", "end_date": "2030-03-01"}
    )
    check("end before start -> VALIDATION_ERROR", result_code_of(bad_dates) == ResultCode.VALIDATION_ERROR, bad_dates)
    overlap = await back_office_tools.open_sprint.ainvoke(
        {"cohort": name_a, "sprint_name": "Sprint 2", "start_date": "2030-01-10", "end_date": "2030-01-20"}
    )
    check(
        "overlapping sprint -> VALIDATION_ERROR naming the clash",
        result_code_of(overlap) == ResultCode.VALIDATION_ERROR and "Sprint 1" in overlap,
        overlap,
    )
    unknown_person = await back_office_tools.assign_role.ainvoke(
        {"person": "@nobody-here", "role": "learner", "cohort": name_a}
    )
    check(
        "unknown person -> VALIDATION_ERROR",
        result_code_of(unknown_person) == ResultCode.VALIDATION_ERROR,
        unknown_person,
    )
    check("validation and refusal codes differ", ResultCode.VALIDATION_ERROR != ResultCode.AUTHORISATION_REFUSED)
    try:
        await back_office.assign_role(f"@{target.username}", "emperor", name_a)
        distinct = False
    except ValidationFailed:
        distinct = True
    except AuthorisationRefused:
        distinct = False
    check("service raises ValidationFailed (not AuthorisationRefused) for an unknown role", distinct)
    check("no sprint was created by the failed attempts", (await snapshot())[1] == before[1] + 1)

    print("--- scoped reads")
    bind(learner)
    mine = await back_office.list_cohorts()
    check(
        "learner lists only their own cohorts (A and B)",
        {c.id for c, _ in mine.entries} == {cohort_a.id, cohort_b.id} and all(r is not None for _, r in mine.entries),
        mine.message,
    )
    members = await back_office_tools.list_cohort_members.ainvoke({"cohort": name_a})
    check(
        "learner in A can list A's members",
        result_code_of(members) == ResultCode.OK and scrum_master.username in members,
        members,
    )
    bind(superadmin)
    everything = await back_office.list_cohorts()
    check(
        "superadmin lists every cohort",
        everything.is_superadmin and {cohort_a.id, cohort_b.id} <= {c.id for c, _ in everything.entries},
    )

    print("--- kill switch")
    await cohort_repo.set_cohort_active(cohort_b.id, False)
    inactive = await back_office_tools.open_sprint.ainvoke({"cohort": name_b, "sprint_name": "Sprint 1"})
    check(
        "inactive cohort -> VALIDATION_ERROR for an authorised requester",
        result_code_of(inactive) == ResultCode.VALIDATION_ERROR and "inactive" in inactive,
        inactive,
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
# Command mode, used by the host verifier's cohort-scoped live case
# ---------------------------------------------------------------------------


async def command_setup_scoped(mattermost_user_id: str, username: str, email: str, stamp: str) -> dict[str, Any]:
    """Give a REAL Mattermost account tech-lead authority in cohort A only (cohort names carry the probe prefix)."""
    user = await identity_repo.upsert_mattermost_user(
        mattermost_user_id=mattermost_user_id,
        username=username,
        email=email.lower(),
        display_name=username,
        timezone="UTC",
        is_superadmin=False,
    )
    assert user.id is not None
    name_a, name_b = f"{PREFIX}scoped-a-{stamp}", f"{PREFIX}scoped-b-{stamp}"
    cohort_a = await cohort_repo.get_cohort_by_name(name_a) or await cohort_repo.create_cohort(name_a)
    cohort_b = await cohort_repo.get_cohort_by_name(name_b) or await cohort_repo.create_cohort(name_b)
    lead = await cohort_repo.get_role_by_key(RoleKey.TECH_LEAD)
    assert lead is not None and lead.id is not None and cohort_a.id is not None and cohort_b.id is not None
    await cohort_repo.upsert_membership(user_id=user.id, cohort_id=cohort_a.id, role_id=lead.id, assigned_by_id=None)
    return {"user_id": user.id, "cohort_a": cohort_a.name, "cohort_b": cohort_b.name}


async def command_check_scoped(stamp: str) -> dict[str, Any]:
    """Count sprints in the scoped cohorts."""
    result: dict[str, Any] = {}
    for label in ("a", "b"):
        cohort = await cohort_repo.get_cohort_by_name(f"{PREFIX}scoped-{label}-{stamp}")
        result[f"sprints_{label}"] = len(await sprint_repo.list_sprints(cohort.id)) if cohort and cohort.id else None
    return result


async def command_cleanup_scoped(mattermost_user_id: str) -> dict[str, Any]:
    """Remove the scoped cohorts (prefix cleanup) and the real account's rows."""
    await cleanup()
    async with database_service.engine.begin() as conn:
        await conn.execute(
            text("DELETE FROM onboarding_steps WHERE user_id IN (SELECT id FROM users WHERE mattermost_user_id = :m)"),
            {"m": mattermost_user_id},
        )
        await conn.execute(
            text(
                "DELETE FROM cohort_memberships WHERE user_id IN (SELECT id FROM users WHERE mattermost_user_id = :m)"
            ),
            {"m": mattermost_user_id},
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
            print(json.dumps(await command_cleanup_scoped(argv[1])))
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
    print("SprintFlow authorisation & back office — in-container probe")
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
