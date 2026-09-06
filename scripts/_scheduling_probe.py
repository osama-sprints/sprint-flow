"""In-container scheduling probe, driven by ``scripts/verify_scheduling.py`` over stdin.

Modes (``argv[1]``):

- ``checks``            — seed people/cohort, exercise the service and the tools (with a
                          real LangGraph ``interrupt()`` / ``Command(resume=...)``), print one
                          PASS/FAIL line per assertion, clean up, exit non-zero on failure.
- ``setup <stamp> <mm_user_id> <username> <email>``
                        — create cohort ``Verify-Sched-<stamp>`` and give that Mattermost
                          account a scrum_master membership; print JSON.
- ``inspect <cohort>``  — print JSON with every ceremony of the cohort (id, scheduled_at UTC,
                          status, type key).
- ``cleanup <stamp>``   — delete everything the setup and the conversation created.

Run in the stack:  docker compose exec -T ai-core /app/.venv/bin/python - checks < scripts/_scheduling_probe.py
Run on a dev DB:   cd ai-core && .venv/bin/python ../scripts/_scheduling_probe.py checks
"""

import asyncio
import json
import os
import sys
from datetime import (
    UTC,
    datetime,
    time,
    timedelta,
)
from types import MappingProxyType
from typing import (
    Any,
    TypedDict,
)
from zoneinfo import ZoneInfo

sys.path.insert(0, "/app")
sys.path.insert(0, os.getcwd())

from langgraph.checkpoint.memory import MemorySaver  # noqa: E402
from langgraph.graph import (  # noqa: E402
    END,
    START,
    StateGraph,
)
from langgraph.types import Command  # noqa: E402
from sqlalchemy import text  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.core.langgraph.graph import (  # noqa: E402
    interrupt_question,
    resume_value,
)
from app.core.langgraph.tools.ceremonies import (  # noqa: E402
    amend_ceremony,
    list_ceremonies,
    schedule_ceremony,
)
from app.core.requester import (  # noqa: E402
    RequesterContext,
    current_requester,
)
from app.models.enums import (  # noqa: E402
    CeremonyTypeKey,
    RoleKey,
)
from app.services import ceremony_scheduling as scheduling  # noqa: E402
from app.services.authorisation import (  # noqa: E402
    REFUSAL_MESSAGE,
    AuthorisationRefused,
    ValidationFailed,
)
from app.services.ceremony_scheduling import (  # noqa: E402
    PAST_CEREMONY_POLICY,
    ScheduleProposal,
    SchedulingProblem,
)
from app.services.database import database_service  # noqa: E402
from app.services.domain import ceremonies as ceremony_repo  # noqa: E402
from app.services.domain import cohorts as cohort_repo  # noqa: E402
from app.services.domain import identity as identity_repo  # noqa: E402

PREFIX = "verify-sched-"
BERLIN = ZoneInfo("Europe/Berlin")
results: list[bool] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    """Record and print one assertion."""
    results.append(ok)
    suffix = f"  ({detail[:160]})" if detail and not ok else ""
    print(f"  {label:70} {'PASS' if ok else 'FAIL'}{suffix}")


class ToolState(TypedDict, total=False):
    """State of the one-node harness graph."""

    tool: str
    args: dict[str, Any]
    result: str


_TOOLS = {"schedule_ceremony": schedule_ceremony, "amend_ceremony": amend_ceremony, "list_ceremonies": list_ceremonies}


async def _run_tool(state: ToolState) -> ToolState:
    return {"result": str(await _TOOLS[state["tool"]].ainvoke(state["args"]))}


def build_harness():
    """A one-node graph that runs a tool under a checkpointer, so ``interrupt()`` can be resumed."""
    graph = StateGraph(ToolState)
    graph.add_node("run", _run_tool)
    graph.add_edge(START, "run")
    graph.add_edge("run", END)
    return graph.compile(checkpointer=MemorySaver())


def pending_value(paused: dict[str, Any]) -> Any:
    """The raw value of the interrupt the harness is paused on, or None."""
    interrupts = paused.get("__interrupt__") or []
    return interrupts[0].value if interrupts else None


async def answer(harness: Any, config: dict, paused: dict[str, Any], text: str) -> dict[str, Any]:
    """Resume a paused tool exactly as ``LangGraphAgent.get_response`` does.

    The conversation layer echoes a structured interrupt's payload back with the
    person's words (``graph.resume_value``), which is what lets a confirming tool
    verify it is committing the instant that was actually shown. Resuming with a
    bare string here would test a path the product never takes.

    Args:
        harness: The compiled one-node graph.
        config: Its thread config.
        paused: The state returned when the tool paused.
        text: What the person replies.

    Returns:
        dict: The state after the resume.
    """
    return await harness.ainvoke(Command(resume=resume_value(text, pending_value(paused))), config)


def requester_for(user, roles: dict[int, str] | None = None) -> RequesterContext:
    """Build the context the conversation layer would bind for this stored user."""
    return RequesterContext(
        mattermost_user_id=user.mattermost_user_id,
        username=user.username,
        email=user.email,
        channel_id="dm",
        channel_type="D",
        user_id=user.id,
        is_superadmin=user.is_superadmin,
        timezone=user.timezone,
        cohort_roles=MappingProxyType(roles or {}),
    )


async def count_ceremonies(cohort_id: int) -> int:
    """Count a cohort's ceremonies straight from the table (no service in the loop)."""
    async with database_service.session() as s:
        result = await s.exec(text("SELECT count(*) FROM ceremonies WHERE cohort_id = :c"), params={"c": cohort_id})
        return int(result.scalar_one())


async def delete_cohorts(cohort_ids: list[int], user_ids: list[int]) -> None:
    """Delete probe rows in dependency order."""
    async with database_service.session() as s:
        if cohort_ids:
            params = {"ids": list(cohort_ids)}
            await s.exec(
                text(
                    "DELETE FROM ceremony_amendments WHERE ceremony_id IN "
                    "(SELECT id FROM ceremonies WHERE cohort_id = ANY(:ids))"
                ),
                params=params,
            )
            await s.exec(text("DELETE FROM ceremonies WHERE cohort_id = ANY(:ids)"), params=params)
            await s.exec(text("DELETE FROM sprints WHERE cohort_id = ANY(:ids)"), params=params)
            await s.exec(text("DELETE FROM onboarding_steps WHERE cohort_id = ANY(:ids)"), params=params)
            await s.exec(text("DELETE FROM cohort_memberships WHERE cohort_id = ANY(:ids)"), params=params)
            await s.exec(text("DELETE FROM cohorts WHERE id = ANY(:ids)"), params=params)
        if user_ids:
            params = {"ids": list(user_ids)}
            await s.exec(text("DELETE FROM cohort_memberships WHERE user_id = ANY(:ids)"), params=params)
            await s.exec(text("DELETE FROM onboarding_steps WHERE user_id = ANY(:ids)"), params=params)
            await s.exec(text("DELETE FROM users WHERE id = ANY(:ids)"), params=params)
        await s.commit()


async def checks() -> None:
    """Exercise every path against the live database, then clean up."""
    stamp = str(int(datetime.now(UTC).timestamp()))
    user_ids: list[int] = []
    cohort_ids: list[int] = []

    async def person(handle: str, *, zone: str | None = "Europe/Berlin"):
        user = await identity_repo.upsert_mattermost_user(
            mattermost_user_id=f"{PREFIX}{stamp}-{handle}",
            username=f"{PREFIX}{stamp}-{handle}",
            email=f"{PREFIX}{stamp}-{handle}@example.test",
            display_name=handle,
            timezone=zone,
            is_superadmin=False,
        )
        assert user.id is not None
        user_ids.append(user.id)
        return user

    try:
        cohort = await cohort_repo.create_cohort(f"{PREFIX}{stamp}")
        other = await cohort_repo.create_cohort(f"{PREFIX}{stamp}-other")
        assert cohort.id and other.id
        cohort_ids += [cohort.id, other.id]
        lead, learner, outsider = await person("lead"), await person("learner"), await person("outsider")
        scrum_master = await cohort_repo.get_role_by_key(RoleKey.SCRUM_MASTER)
        learner_role = await cohort_repo.get_role_by_key(RoleKey.LEARNER)
        assert scrum_master and scrum_master.id and learner_role and learner_role.id and lead.id and learner.id
        await cohort_repo.upsert_membership(
            user_id=lead.id, cohort_id=cohort.id, role_id=scrum_master.id, assigned_by_id=None
        )
        await cohort_repo.upsert_membership(
            user_id=learner.id, cohort_id=cohort.id, role_id=learner_role.id, assigned_by_id=None
        )
        lead_ctx = requester_for(lead, {cohort.id: "scrum_master"})
        learner_ctx = requester_for(learner, {cohort.id: "learner"})
        harness = build_harness()
        expected = datetime.combine(datetime.now(BERLIN).date() + timedelta(days=1), time(14, 0), tzinfo=BERLIN)
        expected = expected.astimezone(UTC)
        base_args = {"cohort": cohort.name, "ceremony_type": "sprint planning", "time_expression": "tomorrow at 2pm"}

        # 1. Ambiguous time -> clarification, zero rows.
        current_requester.set(lead_ctx)
        result = await schedule_ceremony.ainvoke({**base_args, "time_expression": "tomorrow at 2"})
        check(
            "ambiguous 'tomorrow at 2' -> [TIME_CLARIFICATION_REQUIRED] question",
            result.startswith("[TIME_CLARIFICATION_REQUIRED] Did you mean 2 in the afternoon"),
            result,
        )
        check("ambiguous time created zero rows", await count_ceremonies(cohort.id) == 0)

        # 2. Unauthorised (learner) -> refused, zero rows. Outsider too.
        current_requester.set(learner_ctx)
        result = await schedule_ceremony.ainvoke(base_args)
        check(
            "learner -> [AUTHORISATION_REFUSED] fixed sentence",
            result == f"[AUTHORISATION_REFUSED] {REFUSAL_MESSAGE}",
            result,
        )
        current_requester.set(requester_for(outsider))
        result = await schedule_ceremony.ainvoke(base_args)
        check("non-member -> [AUTHORISATION_REFUSED]", result.startswith("[AUTHORISATION_REFUSED]"), result)
        current_requester.set(lead_ctx)
        result = await schedule_ceremony.ainvoke({**base_args, "cohort": other.name})
        check(
            "scrum master of another cohort -> refused (cohort-scoped)",
            result.startswith("[AUTHORISATION_REFUSED]"),
            result,
        )
        check("unauthorised attempts created zero rows", await count_ceremonies(cohort.id) == 0)

        # 3. Confirm "no" -> zero rows.
        config = {"configurable": {"thread_id": f"{stamp}-no"}}
        paused = await harness.ainvoke({"tool": "schedule_ceremony", "args": base_args}, config)
        question = interrupt_question(pending_value(paused)) if pending_value(paused) is not None else ""
        check(
            "tool pauses with interrupt() before writing",
            pending_value(paused) is not None,
            json.dumps(paused, default=str)[:200],
        )
        check(
            "the interrupt carries the instant being confirmed, not just text",
            isinstance(pending_value(paused), dict) and "scheduled_at" in pending_value(paused),
            str(pending_value(paused))[:160],
        )
        check(
            "confirmation names the instant in the person's zone and in UTC",
            "(Europe/Berlin)" in question and expected.strftime("%Y-%m-%d %H:%M UTC") in question,
            question,
        )
        check("nothing stored while waiting for confirmation", await count_ceremonies(cohort.id) == 0)
        declined = await answer(harness, config, paused, "no")
        check(
            "reply 'no' -> [CONFIRMATION_DECLINED]",
            declined.get("result", "").startswith("[CONFIRMATION_DECLINED]"),
            str(declined),
        )
        check("reply 'no' left zero rows", await count_ceremonies(cohort.id) == 0)

        # 4. Confirm "yes" -> one row at the intended UTC instant.
        config = {"configurable": {"thread_id": f"{stamp}-yes"}}
        paused = await harness.ainvoke({"tool": "schedule_ceremony", "args": base_args}, config)
        confirmed = await answer(harness, config, paused, "yes")
        check(
            "reply 'yes' -> [CEREMONY_SCHEDULED]",
            confirmed.get("result", "").startswith("[CEREMONY_SCHEDULED]"),
            str(confirmed),
        )
        rows = await ceremony_repo.list_ceremonies(cohort.id)
        check("exactly one ceremony exists", len(rows) == 1 and await count_ceremonies(cohort.id) == 1)
        stored = rows[0] if rows else None
        check(
            "stored scheduled_at equals the confirmed UTC instant (tz-aware)",
            bool(stored) and stored.scheduled_at == expected and stored.scheduled_at.utcoffset() == timedelta(0),
            str(stored.scheduled_at) if stored else "no row",
        )
        check("organiser is the stored requester, not an argument", bool(stored) and stored.organizer_id == lead.id)
        check(
            "row carries type id, duration, zone and the original words",
            bool(stored)
            and stored.duration_minutes == 90
            and stored.time_zone == "Europe/Berlin"
            and stored.time_expression == "tomorrow at 2pm",
        )

        # 5. Conflict -> refused, still one row.
        result = await schedule_ceremony.ainvoke(
            {**base_args, "ceremony_type": "standup", "time_expression": "tomorrow at 3pm"}
        )
        check(
            "overlapping standup -> [CEREMONY_CONFLICT] naming the clash",
            result.startswith("[CEREMONY_CONFLICT]") and "Sprint Planning" in result,
            result,
        )
        check("conflict created zero new rows", await count_ceremonies(cohort.id) == 1)
        repeat = await scheduling.prepare_schedule(
            cohort=cohort.name, ceremony_type="planning", time_expression="tomorrow at 2pm", conflict_policy="warn"
        )
        check("exact repeat refused even under 'warn' (no duplicate state)", isinstance(repeat, SchedulingProblem))

        # 6. Amend time -> confirmed -> amendment trail row.
        assert stored and stored.id
        config = {"configurable": {"thread_id": f"{stamp}-amend"}}
        paused = await harness.ainvoke(
            {
                "tool": "amend_ceremony",
                "args": {"ceremony_id": stored.id, "new_time_expression": "tomorrow at 4pm", "reason": "probe"},
            },
            config,
        )
        check(
            "amend time pauses for confirmation with both zones",
            pending_value(paused) is not None and "UTC" in interrupt_question(pending_value(paused)),
        )
        done = await answer(harness, config, paused, "yes")
        check(
            "amend confirmed -> [CEREMONY_AMENDED]", done.get("result", "").startswith("[CEREMONY_AMENDED]"), str(done)
        )
        moved = await ceremony_repo.get_ceremony(stored.id)
        trail = await ceremony_repo.list_amendments(stored.id)
        check("scheduled_at moved by two hours", bool(moved) and moved.scheduled_at == expected + timedelta(hours=2))
        check(
            "ceremony_amendments holds the scheduled_at change with old/new values",
            any(
                r.field == "scheduled_at" and r.old_value == expected.isoformat() and r.amended_by_id == lead.id
                for r in trail
            ),
            str([(r.field, r.old_value, r.new_value) for r in trail]),
        )

        # 7. Past ceremony cannot be moved or cancelled; agenda may change.
        planning = await ceremony_repo.get_ceremony_type_by_key(CeremonyTypeKey.SPRINT_PLANNING)
        assert planning and planning.id
        past = await ceremony_repo.create_ceremony(
            cohort_id=cohort.id,
            ceremony_type_id=planning.id,
            organizer_id=lead.id,
            scheduled_at=datetime.now(UTC) - timedelta(days=1),
            duration_minutes=60,
        )
        assert past.id
        result = await amend_ceremony.ainvoke({"ceremony_id": past.id, "new_time_expression": "tomorrow at 2pm"})
        check(
            "past ceremony: time change -> [VALIDATION_ERROR] policy",
            result == f"[VALIDATION_ERROR] {PAST_CEREMONY_POLICY}",
            result,
        )
        result = await amend_ceremony.ainvoke({"ceremony_id": past.id, "cancel": True})
        check("past ceremony: cancel -> [VALIDATION_ERROR] policy", result.startswith("[VALIDATION_ERROR]"), result)
        result = await amend_ceremony.ainvoke({"ceremony_id": past.id, "new_agenda": "Outcome recorded"})
        kept = await ceremony_repo.get_ceremony(past.id)
        check(
            "past ceremony: agenda change allowed, time untouched",
            result.startswith("[CEREMONY_AMENDED]")
            and bool(kept)
            and kept.agenda == "Outcome recorded"
            and kept.scheduled_at == past.scheduled_at,
            result,
        )

        # 8. Reads: member OK, non-member refused.
        current_requester.set(learner_ctx)
        result = await list_ceremonies.ainvoke({"cohort": cohort.name})
        check(
            "learner (member) reads the calendar",
            result.startswith("[OK]") and f"#{stored.id} Sprint Planning" in result,
            result,
        )
        check("calendar shows local time and UTC", "(Europe/Berlin)" in result and "UTC" in result, result)
        current_requester.set(requester_for(outsider))
        result = await list_ceremonies.ainvoke({"cohort": cohort.name})
        check(
            "non-member read -> [AUTHORISATION_REFUSED]",
            result == f"[AUTHORISATION_REFUSED] {REFUSAL_MESSAGE}",
            result,
        )

        # 9. Service-level exceptions keep refusal and validation distinct.
        try:
            await scheduling.prepare_schedule(
                cohort=cohort.name, ceremony_type="town hall", time_expression="tomorrow at 2pm", requester=lead_ctx
            )
            check("unknown ceremony type -> ValidationFailed listing the five", False, "no exception")
        except ValidationFailed as exc:
            check("unknown ceremony type -> ValidationFailed listing the five", "Open Q&A" in str(exc), str(exc))
        try:
            await scheduling.prepare_schedule(
                cohort=cohort.name, ceremony_type="retro", time_expression="tomorrow at 2pm", requester=learner_ctx
            )
            check("service refuses learner with AuthorisationRefused", False, "no exception")
        except AuthorisationRefused as exc:
            check("service refuses learner with AuthorisationRefused", str(exc) == REFUSAL_MESSAGE)
        proposal = await scheduling.prepare_schedule(
            cohort=cohort.name, ceremony_type="retro", time_expression="tomorrow at 2pm", requester=lead_ctx
        )
        check(
            "prepare_schedule writes nothing (still 2 rows)",
            isinstance(proposal, ScheduleProposal) and await count_ceremonies(cohort.id) == 2,
        )
    finally:
        current_requester.set(None)
        await delete_cohorts(cohort_ids, user_ids)
        check("probe rows cleaned up", True)


async def setup(stamp: str, mattermost_user_id: str, username: str, email: str) -> dict[str, Any]:
    """Create the cohort the conversation will target and make the admin its scrum master."""
    user = await identity_repo.upsert_mattermost_user(
        mattermost_user_id=mattermost_user_id,
        username=username,
        email=email.lower(),
        display_name=username,
        timezone=None,
        is_superadmin=email.lower() in settings.ADMIN_EMAILS,
    )
    name = f"Verify-Sched-{stamp}"
    cohort = await cohort_repo.get_cohort_by_name(name) or await cohort_repo.create_cohort(name)
    role = await cohort_repo.get_role_by_key(RoleKey.SCRUM_MASTER)
    assert user.id and cohort.id and role and role.id
    await cohort_repo.upsert_membership(user_id=user.id, cohort_id=cohort.id, role_id=role.id, assigned_by_id=user.id)
    return {
        "cohort_id": cohort.id,
        "cohort_name": cohort.name,
        "user_id": user.id,
        "is_superadmin": user.is_superadmin,
    }


async def inspect(cohort_reference: str) -> dict[str, Any]:
    """Report every ceremony of a cohort, in UTC."""
    cohort = await cohort_repo.resolve_cohort(cohort_reference)
    if cohort is None or cohort.id is None:
        return {"cohort": None, "ceremonies": []}
    rows = await ceremony_repo.list_ceremonies(cohort.id, include_past=True, include_cancelled=True)
    types = {row.id: row.key for row in await ceremony_repo.list_ceremony_types()}
    ceremonies = []
    for row in rows:
        assert row.id is not None
        amendments = await ceremony_repo.list_amendments(row.id)
        ceremonies.append(
            {
                "id": row.id,
                "scheduled_at_utc": row.scheduled_at.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC"),
                "status": row.status,
                "type": types.get(row.ceremony_type_id),
                "duration_minutes": row.duration_minutes,
                "organizer_id": row.organizer_id,
                "time_zone": row.time_zone,
                "time_expression": row.time_expression,
                "agenda": row.agenda,
                "amendments": [
                    {"field": a.field, "old": a.old_value, "new": a.new_value, "by": a.amended_by_id}
                    for a in amendments
                ],
            }
        )
    return {"cohort": cohort.name, "ceremonies": ceremonies}


async def cleanup(stamp: str) -> dict[str, Any]:
    """Delete the cohort created by ``setup`` and everything hanging off it."""
    cohort = await cohort_repo.get_cohort_by_name(f"Verify-Sched-{stamp}")
    if cohort is None or cohort.id is None:
        return {"deleted": False}
    await delete_cohorts([cohort.id], [])
    return {"deleted": True, "cohort_id": cohort.id}


async def main(argv: list[str]) -> int:
    mode = argv[0] if argv else "checks"
    try:
        if mode == "checks":
            print("=" * 84)
            print("SprintFlow ceremony scheduling — in-container probe")
            print("=" * 84)
            await checks()
            print("=" * 84)
            print(f"{sum(results)}/{len(results)} checks passed")
            print("SCHEDULING PROBE OK" if all(results) else "SCHEDULING PROBE FAILED")
            return 0 if all(results) else 1
        if mode == "setup":
            print(json.dumps(await setup(argv[1], argv[2], argv[3], argv[4])))
            return 0
        if mode == "inspect":
            print(json.dumps(await inspect(argv[1])))
            return 0
        if mode == "cleanup":
            print(json.dumps(await cleanup(argv[1])))
            return 0
        print(f"unknown mode {mode!r}", file=sys.stderr)
        return 2
    finally:
        await database_service.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:])))
