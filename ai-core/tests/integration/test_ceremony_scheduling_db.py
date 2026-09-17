"""Database-backed proof of the scheduling flow. Runs only with SPRINTFLOW_INTEGRATION_DB=1.

Each test seeds its own people and channels (prefix ``it-sched-``), drives the
service and the tools, inspects the rows, and deletes what it made. The tools
are driven inside a minimal LangGraph ``StateGraph`` with a ``MemorySaver`` so
``interrupt()`` and ``Command(resume=...)`` behave as they do in production,
without a model.
"""

import asyncio
import os
import time as time_module
from datetime import (
    UTC,
    datetime,
    time,
    timedelta,
)
from types import MappingProxyType
from typing import (
    Any,
    Awaitable,
    Callable,
    TypedDict,
)
from zoneinfo import ZoneInfo

import pytest
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import (
    END,
    START,
    StateGraph,
)
from langgraph.types import Command
from sqlalchemy import text

from app.core.config import settings
from app.core.langgraph.graph import (
    interrupt_question,
    resume_value,
)
from app.core.langgraph.tools.ceremonies import (
    amend_ceremony,
    list_ceremonies,
    schedule_ceremony,
)
from app.core.requester import (
    RequesterContext,
    current_requester,
)
from app.models.enums import (
    CeremonyStatus,
    CeremonyTypeKey,
    RoleKey,
)
from app.services import ceremony_scheduling as scheduling
from app.services.authorisation import (
    REFUSAL_MESSAGE,
    AuthorisationRefused,
    ValidationFailed,
)
from app.services.ceremony_scheduling import (
    PAST_CEREMONY_POLICY,
    ScheduleProposal,
    SchedulingProblem,
)
from app.services.database import database_service
from app.services.domain import ceremonies as ceremony_repo
from app.services.domain import channels as channel_repo
from app.services.domain import identity as identity_repo

pytestmark = pytest.mark.skipif(
    not os.getenv("SPRINTFLOW_INTEGRATION_DB"), reason="needs a real database (SPRINTFLOW_INTEGRATION_DB=1)"
)

BERLIN = ZoneInfo("Europe/Berlin")


class ToolState(TypedDict, total=False):
    """State of the one-node harness graph."""

    tool: str
    args: dict[str, Any]
    result: str


_TOOLS = {"schedule_ceremony": schedule_ceremony, "amend_ceremony": amend_ceremony, "list_ceremonies": list_ceremonies}


async def _run_tool(state: ToolState) -> ToolState:
    tool = _TOOLS[state["tool"]]
    return {"result": str(await tool.ainvoke(state["args"]))}


def build_harness():
    graph = StateGraph(ToolState)
    graph.add_node("run", _run_tool)
    graph.add_edge(START, "run")
    graph.add_edge("run", END)
    return graph.compile(checkpointer=MemorySaver())


class Fixture:
    """People and channels for one test, with cleanup."""

    def __init__(self) -> None:
        self.stamp = f"{int(time_module.time() * 1000)}"
        self.prefix = f"it-sched-{self.stamp}"
        self.user_ids: list[int] = []
        self.channel_ids: list[int] = []

    async def person(self, handle: str, *, superadmin: bool = False, zone: str | None = "Europe/Berlin"):
        user = await identity_repo.upsert_mattermost_user(
            mattermost_user_id=f"{self.prefix}-{handle}",
            username=f"{self.prefix}-{handle}",
            email=f"{self.prefix}-{handle}@example.test",
            display_name=handle,
            timezone=zone,
            is_superadmin=superadmin,
        )
        assert user.id is not None
        self.user_ids.append(user.id)
        return user

    async def channel(self, suffix: str):
        channel = await channel_repo.create_channel(f"{self.prefix}-{suffix}")
        assert channel.id is not None
        self.channel_ids.append(channel.id)
        return channel

    async def member(self, user_id: int, channel_id: int, role: RoleKey) -> None:
        role_row = await channel_repo.get_role_by_key(role)
        assert role_row is not None and role_row.id is not None
        await channel_repo.upsert_membership(
            user_id=user_id, channel_id=channel_id, role_id=role_row.id, assigned_by_id=None
        )

    @staticmethod
    def requester(user, *, roles: dict[int, str] | None = None) -> RequesterContext:
        return RequesterContext(
            mattermost_user_id=user.mattermost_user_id,
            username=user.username,
            email=user.email,
            channel_id="dm",
            channel_type="D",
            user_id=user.id,
            is_superadmin=user.is_superadmin,
            timezone=user.timezone,
            channel_roles=MappingProxyType(roles or {}),
        )

    async def ceremony_count(self, channel_id: int) -> int:
        async with database_service.session() as s:
            result = await s.exec(
                text("SELECT count(*) FROM ceremonies WHERE channel_id = :c"), params={"c": channel_id}
            )
            return int(result.scalar_one())

    async def cleanup(self) -> None:
        async with database_service.session() as s:
            if self.channel_ids:
                params = {"ids": list(self.channel_ids)}
                await s.exec(
                    text(
                        "DELETE FROM ceremony_amendments WHERE ceremony_id IN "
                        "(SELECT id FROM ceremonies WHERE channel_id = ANY(:ids))"
                    ),
                    params=params,
                )
                await s.exec(text("DELETE FROM ceremonies WHERE channel_id = ANY(:ids)"), params=params)
                await s.exec(text("DELETE FROM sprints WHERE channel_id = ANY(:ids)"), params=params)
                await s.exec(text("DELETE FROM channel_memberships WHERE channel_id = ANY(:ids)"), params=params)
                await s.exec(text("DELETE FROM channels WHERE id = ANY(:ids)"), params=params)
            if self.user_ids:
                await s.exec(text("DELETE FROM users WHERE id = ANY(:ids)"), params={"ids": list(self.user_ids)})
            await s.commit()


def tomorrow_at_14_berlin_in_utc() -> datetime:
    local_now = datetime.now(BERLIN)
    return datetime.combine(local_now.date() + timedelta(days=1), time(14, 0), tzinfo=BERLIN).astimezone(UTC)


def run(coroutine_factory: Callable[[Fixture], Awaitable[None]]) -> None:
    """Run one scenario on a fresh event loop, always cleaning up and disposing the engine."""

    async def scenario() -> None:
        fixture = Fixture()
        try:
            await coroutine_factory(fixture)
        finally:
            current_requester.set(None)
            await fixture.cleanup()
            await database_service.engine.dispose()

    asyncio.run(scenario())


def pending_value(paused):
    """The raw value of the interrupt the harness is paused on, or None."""
    interrupts = paused.get("__interrupt__") or []
    return interrupts[0].value if interrupts else None


async def answer(harness, config, paused, text):
    """Resume exactly as LangGraphAgent.get_response does: the payload is echoed with the reply."""
    return await harness.ainvoke(Command(resume=resume_value(text, pending_value(paused))), config)


# --- authorisation --------------------------------------------------------------------


def test_unauthorised_requesters_are_refused_and_nothing_is_created():
    async def scenario(fx: Fixture) -> None:
        channel = await fx.channel("A")
        learner = await fx.person("learner")
        outsider = await fx.person("outsider")
        unsynced = RequesterContext(mattermost_user_id=f"{fx.prefix}-ghost", timezone="Europe/Berlin")
        assert channel.id and learner.id
        await fx.member(learner.id, channel.id, RoleKey.LEARNER)

        for who in (fx.requester(learner, roles={channel.id: "learner"}), fx.requester(outsider), unsynced):
            with pytest.raises(AuthorisationRefused) as excinfo:
                await scheduling.prepare_schedule(
                    channel=channel.name, ceremony_type="planning", time_expression="tomorrow at 2pm", requester=who
                )
            assert str(excinfo.value) == REFUSAL_MESSAGE
        assert await fx.ceremony_count(channel.id) == 0

        # Through the tool, with the ContextVar bound: same refusal, same silence.
        current_requester.set(fx.requester(learner, roles={channel.id: "learner"}))
        result = await schedule_ceremony.ainvoke(
            {"channel": channel.name, "ceremony_type": "planning", "time_expression": "tomorrow at 2pm"}
        )
        assert result == f"[AUTHORISATION_REFUSED] {REFUSAL_MESSAGE}"
        assert await fx.ceremony_count(channel.id) == 0

    run(scenario)


def test_authority_is_channel_scoped():
    async def scenario(fx: Fixture) -> None:
        channel_a = await fx.channel("A")
        channel_b = await fx.channel("B")
        lead = await fx.person("lead")
        assert channel_a.id and channel_b.id and lead.id
        await fx.member(lead.id, channel_a.id, RoleKey.SCRUM_MASTER)
        who = fx.requester(lead, roles={channel_a.id: "scrum_master"})

        with pytest.raises(AuthorisationRefused):
            await scheduling.prepare_schedule(
                channel=channel_b.name, ceremony_type="retro", time_expression="tomorrow at 2pm", requester=who
            )
        proposal = await scheduling.prepare_schedule(
            channel=channel_a.name, ceremony_type="retro", time_expression="tomorrow at 2pm", requester=who
        )
        assert isinstance(proposal, ScheduleProposal)
        assert proposal.organizer_id == lead.id
        assert await fx.ceremony_count(channel_a.id) == 0  # preparing writes nothing

    run(scenario)


# --- interpretation and confirmation ------------------------------------------------


def test_ambiguous_time_is_a_question_and_creates_nothing():
    async def scenario(fx: Fixture) -> None:
        channel = await fx.channel("A")
        lead = await fx.person("lead")
        assert channel.id and lead.id
        await fx.member(lead.id, channel.id, RoleKey.TECH_LEAD)
        who = fx.requester(lead, roles={channel.id: "tech_lead"})

        outcome = await scheduling.prepare_schedule(
            channel=channel.name, ceremony_type="retro", time_expression="tomorrow at 2", requester=who
        )
        assert isinstance(outcome, SchedulingProblem) and outcome.kind == "clarification"
        assert "2 in the afternoon or 2 in the morning" in outcome.message

        current_requester.set(who)
        result = await schedule_ceremony.ainvoke(
            {"channel": channel.name, "ceremony_type": "retro", "time_expression": "tomorrow at 2"}
        )
        assert result.startswith("[TIME_CLARIFICATION_REQUIRED] Did you mean 2 in the afternoon")
        assert await fx.ceremony_count(channel.id) == 0

        no_zone = fx.requester(await fx.person("nozone", zone=None))
        await fx.member(no_zone.user_id or 0, channel.id, RoleKey.SCRUM_MASTER)
        previous_default = settings.SCHEDULING_DEFAULT_TIMEZONE
        settings.SCHEDULING_DEFAULT_TIMEZONE = ""
        try:
            outcome = await scheduling.prepare_schedule(
                channel=channel.name, ceremony_type="retro", time_expression="tomorrow at 2pm", requester=no_zone
            )
        finally:
            settings.SCHEDULING_DEFAULT_TIMEZONE = previous_default
        assert isinstance(outcome, SchedulingProblem) and outcome.status == "no_timezone"
        assert await fx.ceremony_count(channel.id) == 0

    run(scenario)


def test_confirmation_gates_persistence_and_stores_the_exact_instant():
    async def scenario(fx: Fixture) -> None:
        channel = await fx.channel("A")
        lead = await fx.person("lead")
        assert channel.id and lead.id
        await fx.member(lead.id, channel.id, RoleKey.SCRUM_MASTER)
        current_requester.set(fx.requester(lead, roles={channel.id: "scrum_master"}))
        harness = build_harness()
        expected = tomorrow_at_14_berlin_in_utc()
        args = {
            "channel": channel.name,
            "ceremony_type": "sprint planning",
            "time_expression": "tomorrow at 2pm",
            "agenda": "Plan the sprint",
        }

        # Declined: the question named the instant in both zones, and nothing was stored.
        config = {"configurable": {"thread_id": f"{fx.prefix}-no"}}
        paused = await harness.ainvoke({"tool": "schedule_ceremony", "args": args}, config)
        question = interrupt_question(pending_value(paused))
        assert expected.strftime("%Y-%m-%d %H:%M UTC") in question
        assert "(Europe/Berlin)" in question and "Sprint Planning" in question and channel.name in question
        assert await fx.ceremony_count(channel.id) == 0
        declined = await answer(harness, config, paused, "no")
        assert declined["result"] == "[CONFIRMATION_DECLINED] Nothing was scheduled."
        assert await fx.ceremony_count(channel.id) == 0

        # Unclear reply: also treated as no.
        config = {"configurable": {"thread_id": f"{fx.prefix}-unclear"}}
        await harness.ainvoke({"tool": "schedule_ceremony", "args": args}, config)
        unclear = await answer(harness, config, paused, "maybe later")
        assert unclear["result"].startswith("[CONFIRMATION_DECLINED] Your reply was not a clear yes")
        assert await fx.ceremony_count(channel.id) == 0

        # Confirmed: exactly one row, at exactly the confirmed instant, reading back equal.
        config = {"configurable": {"thread_id": f"{fx.prefix}-yes"}}
        await harness.ainvoke({"tool": "schedule_ceremony", "args": args}, config)
        assert await fx.ceremony_count(channel.id) == 0
        confirmed = await answer(harness, config, paused, "yes")
        assert confirmed["result"].startswith("[CEREMONY_SCHEDULED] Scheduled ceremony #")
        assert await fx.ceremony_count(channel.id) == 1
        rows = await ceremony_repo.list_ceremonies(channel.id)
        stored = rows[0]
        assert stored.scheduled_at == expected
        assert stored.scheduled_at.utcoffset() == timedelta(0)
        assert stored.organizer_id == lead.id
        assert stored.time_zone == "Europe/Berlin" and stored.time_expression == "tomorrow at 2pm"
        assert stored.duration_minutes == 90 and stored.agenda == "Plan the sprint"
        assert stored.status == CeremonyStatus.SCHEDULED.value
        planning = await ceremony_repo.get_ceremony_type_by_key(CeremonyTypeKey.SPRINT_PLANNING)
        assert planning and stored.ceremony_type_id == planning.id

        # Repeating the same request does not create a second identical ceremony.
        repeat = await scheduling.prepare_schedule(
            channel=channel.name, ceremony_type="planning", time_expression="tomorrow at 2pm", conflict_policy="warn"
        )
        assert isinstance(repeat, SchedulingProblem) and "already exists" in repeat.message
        assert await fx.ceremony_count(channel.id) == 1

    run(scenario)


# --- conflicts -----------------------------------------------------------------------


def test_conflict_policy_refuses_by_default_and_warns_when_configured():
    async def scenario(fx: Fixture) -> None:
        channel = await fx.channel("A")
        lead = await fx.person("lead")
        assert channel.id and lead.id
        await fx.member(lead.id, channel.id, RoleKey.SCRUM_MASTER)
        who = fx.requester(lead, roles={channel.id: "scrum_master"})

        first = await scheduling.prepare_schedule(
            channel=channel.name, ceremony_type="planning", time_expression="tomorrow at 2pm", requester=who
        )
        assert isinstance(first, ScheduleProposal)
        stored = await scheduling.commit_schedule(first, requester=who)
        assert not isinstance(stored, SchedulingProblem)
        assert await fx.ceremony_count(channel.id) == 1

        # Planning lasts 90 minutes: a standup at 15:00 overlaps it; one at 15:30 does not.
        clash = await scheduling.prepare_schedule(
            channel=channel.name,
            ceremony_type="standup",
            time_expression="tomorrow at 3pm",
            requester=who,
            conflict_policy="refuse",
        )
        assert isinstance(clash, SchedulingProblem) and clash.kind == "conflict"
        assert f"#{stored.id} Sprint Planning" in clash.message
        assert await fx.ceremony_count(channel.id) == 1

        clear = await scheduling.prepare_schedule(
            channel=channel.name,
            ceremony_type="standup",
            time_expression="tomorrow at 15:30",
            requester=who,
            conflict_policy="refuse",
        )
        assert isinstance(clear, ScheduleProposal) and clear.conflict_warning is None

        warned = await scheduling.prepare_schedule(
            channel=channel.name,
            ceremony_type="standup",
            time_expression="tomorrow at 3pm",
            requester=who,
            conflict_policy="warn",
        )
        assert isinstance(warned, ScheduleProposal)
        assert warned.conflict_warning and warned.conflict_warning.startswith("Warning: this overlaps")
        assert warned.confirmation_question().startswith("Warning:")
        committed = await scheduling.commit_schedule(warned, requester=who, conflict_policy="warn")
        assert not isinstance(committed, SchedulingProblem)
        assert await fx.ceremony_count(channel.id) == 2

        # Commit re-checks: a proposal prepared under "warn" cannot be committed under "refuse".
        again = await scheduling.prepare_schedule(
            channel=channel.name,
            ceremony_type="review",
            time_expression="tomorrow at 3pm",
            requester=who,
            conflict_policy="warn",
        )
        assert isinstance(again, ScheduleProposal)
        blocked = await scheduling.commit_schedule(again, requester=who, conflict_policy="refuse")
        assert isinstance(blocked, SchedulingProblem)
        assert await fx.ceremony_count(channel.id) == 2

    run(scenario)


# --- amendments -----------------------------------------------------------------------


def test_amendments_are_confirmed_and_traced():
    async def scenario(fx: Fixture) -> None:
        channel = await fx.channel("A")
        lead = await fx.person("lead")
        assert channel.id and lead.id
        await fx.member(lead.id, channel.id, RoleKey.TECH_LEAD)
        who = fx.requester(lead, roles={channel.id: "tech_lead"})
        current_requester.set(who)
        proposal = await scheduling.prepare_schedule(
            channel=channel.name, ceremony_type="retro", time_expression="tomorrow at 2pm", requester=who
        )
        assert isinstance(proposal, ScheduleProposal)
        ceremony = await scheduling.commit_schedule(proposal, requester=who)
        assert not isinstance(ceremony, SchedulingProblem) and ceremony.id
        harness = build_harness()

        # Time change through the tool: question, "no" leaves it, "yes" moves it and writes the trail.
        args = {"ceremony_id": ceremony.id, "new_time_expression": "tomorrow at 4pm", "reason": "clash with lunch"}
        config = {"configurable": {"thread_id": f"{fx.prefix}-amend-no"}}
        paused = await harness.ainvoke({"tool": "amend_ceremony", "args": args}, config)
        question = interrupt_question(pending_value(paused))
        assert "move it from" in question and "UTC" in question
        declined = await answer(harness, config, paused, "no")
        assert declined["result"] == "[CONFIRMATION_DECLINED] Nothing was changed."
        unchanged = await ceremony_repo.get_ceremony(ceremony.id)
        assert unchanged and unchanged.scheduled_at == ceremony.scheduled_at
        assert await ceremony_repo.list_amendments(ceremony.id) == []

        config = {"configurable": {"thread_id": f"{fx.prefix}-amend-yes"}}
        await harness.ainvoke({"tool": "amend_ceremony", "args": args}, config)
        done = await answer(harness, config, paused, "yes")
        assert done["result"].startswith("[CEREMONY_AMENDED] Done: amend ceremony #")
        moved = await ceremony_repo.get_ceremony(ceremony.id)
        assert moved and moved.scheduled_at == ceremony.scheduled_at + timedelta(hours=2)
        trail = await ceremony_repo.list_amendments(ceremony.id)
        fields = {row.field for row in trail}
        assert {"scheduled_at", "time_expression"} <= fields
        assert all(row.amended_by_id == lead.id and row.reason == "clash with lunch" for row in trail)
        time_row = next(row for row in trail if row.field == "scheduled_at")
        assert time_row.old_value == ceremony.scheduled_at.isoformat()
        assert time_row.new_value == moved.scheduled_at.isoformat()

        # Agenda-only: no confirmation, one more trail row.
        result = await amend_ceremony.ainvoke({"ceremony_id": ceremony.id, "new_agenda": "What went well"})
        assert result.startswith("[CEREMONY_AMENDED]")
        trail = await ceremony_repo.list_amendments(ceremony.id)
        assert trail[-1].field == "agenda" and trail[-1].new_value == "What went well"

        # Ambiguous new time: question, nothing changes.
        result = await amend_ceremony.ainvoke({"ceremony_id": ceremony.id, "new_time_expression": "friday at 3"})
        assert result.startswith("[TIME_CLARIFICATION_REQUIRED]")
        assert len(await ceremony_repo.list_amendments(ceremony.id)) == len(trail)

        # Cancel: confirmed, status flips, trail records it; a second cancel changes nothing.
        config = {"configurable": {"thread_id": f"{fx.prefix}-cancel"}}
        paused = await harness.ainvoke(
            {"tool": "amend_ceremony", "args": {"ceremony_id": ceremony.id, "cancel": True}}, config
        )
        assert "cancel ceremony #" in interrupt_question(pending_value(paused))
        done = await answer(harness, config, paused, "yes")
        assert done["result"].startswith("[CEREMONY_CANCELLED]")
        cancelled = await ceremony_repo.get_ceremony(ceremony.id)
        assert cancelled and cancelled.status == CeremonyStatus.CANCELLED.value
        trail = await ceremony_repo.list_amendments(ceremony.id)
        assert trail[-1].field == "status" and trail[-1].new_value == "cancelled"
        result = await amend_ceremony.ainvoke({"ceremony_id": ceremony.id, "cancel": True})
        assert result.startswith("[CEREMONY_CONFLICT]") and "already cancelled" in result
        assert len(await ceremony_repo.list_amendments(ceremony.id)) == len(trail)

        # A learner may not amend.
        learner = await fx.person("learner")
        assert learner.id
        await fx.member(learner.id, channel.id, RoleKey.LEARNER)
        with pytest.raises(AuthorisationRefused):
            await scheduling.prepare_amendment(
                ceremony_id=ceremony.id, new_agenda="hijack", requester=fx.requester(learner)
            )

    run(scenario)


def test_past_ceremony_policy():
    async def scenario(fx: Fixture) -> None:
        channel = await fx.channel("A")
        lead = await fx.person("lead")
        planning = await ceremony_repo.get_ceremony_type_by_key(CeremonyTypeKey.SPRINT_PLANNING)
        assert channel.id and lead.id and planning and planning.id
        await fx.member(lead.id, channel.id, RoleKey.SCRUM_MASTER)
        who = fx.requester(lead, roles={channel.id: "scrum_master"})
        past = await ceremony_repo.create_ceremony(
            channel_id=channel.id,
            ceremony_type_id=planning.id,
            organizer_id=lead.id,
            scheduled_at=datetime.now(UTC) - timedelta(days=1),
            duration_minutes=60,
        )
        assert past.id

        with pytest.raises(ValidationFailed) as moved:
            await scheduling.prepare_amendment(
                ceremony_id=past.id, new_time_expression="tomorrow at 2pm", requester=who
            )
        assert str(moved.value) == PAST_CEREMONY_POLICY
        with pytest.raises(ValidationFailed) as cancelled:
            await scheduling.prepare_amendment(ceremony_id=past.id, cancel=True, requester=who)
        assert str(cancelled.value) == PAST_CEREMONY_POLICY

        notes = await scheduling.prepare_amendment(ceremony_id=past.id, new_agenda="Outcome: shipped", requester=who)
        assert not isinstance(notes, SchedulingProblem) and notes.requires_confirmation is False
        updated, trail = await scheduling.commit_amendment(notes, requester=who)
        assert updated.agenda == "Outcome: shipped" and updated.status == CeremonyStatus.SCHEDULED.value
        assert [row.field for row in trail] == ["agenda"]
        kept = await ceremony_repo.get_ceremony(past.id)
        assert kept and kept.scheduled_at == past.scheduled_at

    run(scenario)


# --- reads ----------------------------------------------------------------------------


def test_any_member_can_read_and_non_members_cannot():
    async def scenario(fx: Fixture) -> None:
        channel = await fx.channel("A")
        lead = await fx.person("lead")
        learner = await fx.person("learner", zone="Africa/Cairo")
        outsider = await fx.person("outsider")
        admin = await fx.person("admin", superadmin=True, zone=None)
        assert channel.id and lead.id and learner.id
        await fx.member(lead.id, channel.id, RoleKey.SCRUM_MASTER)
        await fx.member(learner.id, channel.id, RoleKey.LEARNER)
        lead_ctx = fx.requester(lead, roles={channel.id: "scrum_master"})
        proposal = await scheduling.prepare_schedule(
            channel=channel.name,
            ceremony_type="q&a",
            time_expression="tomorrow at 2pm",
            agenda="Ask anything",
            requester=lead_ctx,
        )
        assert isinstance(proposal, ScheduleProposal)
        stored = await scheduling.commit_schedule(proposal, requester=lead_ctx)
        assert not isinstance(stored, SchedulingProblem)

        # Learner reads, in their own zone (Cairo, UTC+3 in summer) and in UTC.
        current_requester.set(fx.requester(learner, roles={channel.id: "learner"}))
        result = await list_ceremonies.ainvoke({"channel": channel.name})
        assert result.startswith("[OK]")
        assert f"#{stored.id} Open Q&A" in result
        assert "(Africa/Cairo)" in result and proposal.utc_display in result
        assert f"organiser @{lead.username}" in result and "agenda: Ask anything" in result

        # Outsider: refused. Unsynced: refused.
        current_requester.set(fx.requester(outsider))
        result = await list_ceremonies.ainvoke({"channel": channel.name})
        assert result == f"[AUTHORISATION_REFUSED] {REFUSAL_MESSAGE}"
        with pytest.raises(AuthorisationRefused):
            await scheduling.list_calendar(
                channel=channel.name, requester=RequesterContext(mattermost_user_id=f"{fx.prefix}-ghost")
            )

        # Superadmin without membership and without a zone: allowed, UTC only.
        view = await scheduling.list_calendar(channel=channel.name, requester=fx.requester(admin))
        assert len(view.entries) == 1 and view.zone in (None, settings.SCHEDULING_DEFAULT_TIMEZONE or None)

        # Cancelled and past rows are hidden unless asked for.
        cancel = await scheduling.prepare_amendment(ceremony_id=stored.id or 0, cancel=True, requester=lead_ctx)
        assert not isinstance(cancel, SchedulingProblem)
        await scheduling.commit_amendment(cancel, requester=lead_ctx)
        hidden = await scheduling.list_calendar(channel=channel.name, requester=lead_ctx)
        assert hidden.entries == ()
        shown = await scheduling.list_calendar(channel=channel.name, include_cancelled=True, requester=lead_ctx)
        assert len(shown.entries) == 1 and shown.entries[0].ceremony.status == "cancelled"

    run(scenario)


def test_validation_failures_are_not_refusals():
    async def scenario(fx: Fixture) -> None:
        channel = await fx.channel("A")
        lead = await fx.person("lead")
        assert channel.id and lead.id
        await fx.member(lead.id, channel.id, RoleKey.SCRUM_MASTER)
        who = fx.requester(lead, roles={channel.id: "scrum_master"})
        current_requester.set(who)

        result = await schedule_ceremony.ainvoke(
            {"channel": channel.name, "ceremony_type": "town hall", "time_expression": "tomorrow at 2pm"}
        )
        assert result.startswith("[VALIDATION_ERROR] 'town hall' is not a ceremony type I know")
        assert "Daily Standup, Sprint Planning, Sprint Review, Retrospective, Open Q&A" in result

        result = await schedule_ceremony.ainvoke(
            {
                "channel": channel.name,
                "ceremony_type": "retro",
                "time_expression": "tomorrow at 2pm",
                "sprint_name": "Sprint 99",
            }
        )
        assert result.startswith("[VALIDATION_ERROR]") and "Sprint 99" in result

        result = await schedule_ceremony.ainvoke(
            {"channel": f"{fx.prefix}-nowhere", "ceremony_type": "retro", "time_expression": "tomorrow at 2pm"}
        )
        assert result.startswith("[VALIDATION_ERROR] I do not know a channel")

        await channel_repo.set_channel_active(channel.id, False)
        result = await schedule_ceremony.ainvoke(
            {"channel": channel.name, "ceremony_type": "retro", "time_expression": "tomorrow at 2pm"}
        )
        assert result.startswith("[VALIDATION_ERROR]") and "inactive" in result
        assert await fx.ceremony_count(channel.id) == 0

    run(scenario)
