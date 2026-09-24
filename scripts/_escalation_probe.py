"""In-container escalation probe: one PASS/FAIL line per assertion, non-zero exit on failure.

Driven by the host-side ``scripts/verify_escalation.py`` (which pipes this file
over stdin into the ai-core container), or run directly against any database
that has the domain schema:

    docker compose exec -T ai-core /app/.venv/bin/python - < scripts/_escalation_probe.py
    (cd ai-core && PYTHONPATH=. .venv/bin/python ../scripts/_escalation_probe.py)

Mattermost is never called for real: ``app.services.escalation.mattermost_client``
is swapped for an in-memory fake that records DMs instead of sending them and
can be told to fail on demand. All rows this probe creates carry the
``verify-esc-`` prefix (users and ``channel_roles`` rows) or live under ids
with that prefix (escalation tickets), and are deleted at the end, even when
an assertion fails.

This probe targets the post-refactor **channels** architecture: a channel is a
plain Mattermost channel id (there is no local ``channels`` table), roles live
in ``channel_roles``, and every sticky references ``channel_id``/``channel_roles``
rather than the legacy ``cohort_id``/``cohort_memberships``/``cohorts`` schema.
"""

import asyncio
import os
import re
import sys
import uuid
from typing import Any

for _candidate in ("/app", os.path.join(os.getcwd(), "ai-core"), os.getcwd()):
    if os.path.isdir(os.path.join(_candidate, "app")):
        sys.path.insert(0, _candidate)
        break

from sqlalchemy import text  # noqa: E402

from app.core.langgraph.tools import escalation as escalation_tools  # noqa: E402
from app.core.langgraph.tools import (  # noqa: E402
    LEARNER_SUPPORT_TOOLS,
    BACK_OFFICE_TOOLS,
)
from app.core.langgraph.tools.results import (  # noqa: E402
    ResultCode,
    result_code_of,
)
from app.core.requester import (  # noqa: E402
    RequesterContext,
    current_requester,
)
from app.models.enums import RoleKey  # noqa: E402
from app.services import escalation  # noqa: E402
from app.services.database import database_service  # noqa: E402
from app.services.domain import channels as channel_repo  # noqa: E402
from app.services.domain import escalations as escalation_repo  # noqa: E402
from app.services.domain import identity as identity_repo  # noqa: E402

PREFIX = "verify-esc-"
STAMP = uuid.uuid4().hex[:8]
TICKET_REF_RE = re.compile(r"^ESC-\d{6}$")

results: list[bool] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    """Record and print one assertion."""
    results.append(ok)
    suffix = f"  ({detail})" if detail and not ok else ""
    print(f"  {label:70} {'PASS' if ok else 'FAIL'}{suffix}", flush=True)


# ---------------------------------------------------------------------------
# Fake Mattermost — records DMs instead of sending them; can fail on demand
# ---------------------------------------------------------------------------


class FakeMattermost:
    """Stands in for ``mattermost_client`` inside ``app.services.escalation``.

    Mirrors the REAL client's failure contract (return None, never raise —
    see ``app/services/mattermost.py``), because ``open_escalation`` checks for
    ``None`` rather than catching an exception.
    """

    def __init__(self) -> None:
        self.dm_channels: dict[str, str] = {}  # mattermost_user_id -> channel id
        self.posts: list[dict[str, Any]] = []
        self.fail = False

    async def create_direct_channel(self, user_id: str) -> dict[str, Any] | None:
        if self.fail:
            return None
        channel_id = self.dm_channels.setdefault(user_id, f"dm-{user_id}")
        return {"id": channel_id}

    async def create_post(self, channel_id: str, message: str, root_id: str | None = None) -> dict[str, Any] | None:
        if self.fail:
            return None
        post = {"id": f"probe-post-{uuid.uuid4().hex[:8]}", "channel_id": channel_id, "message": message}
        self.posts.append(post)
        return post


# ---------------------------------------------------------------------------
# Helpers — channels architecture: a channel is just a Mattermost channel id
# ---------------------------------------------------------------------------


def channel_under_test(tag: str) -> str:
    """A unique fake Mattermost channel id (no local channel row is needed)."""
    return f"{PREFIX}channel-{tag}-{STAMP}"


def bind(mattermost_user_id: str, channel_id: str, *, thread_id: str = "") -> None:
    """Bind a requester the way the conversation layer does for one turn."""
    current_requester.set(
        RequesterContext(
            mattermost_user_id=mattermost_user_id,
            channel_id=channel_id,
            channel_type="O",
            learner_thread_id=thread_id,
        )
    )


def unbind() -> None:
    """Clear the requester, as the conversation layer does after every turn."""
    current_requester.set(None)


async def make_learner(tag: str) -> Any:
    """Seed a learner identity (escalation does not require any role)."""
    mattermost_user_id = f"{PREFIX}{tag}-{STAMP}"
    return await identity_repo.upsert_mattermost_user(
        mattermost_user_id=mattermost_user_id,
        username=f"{PREFIX}{tag}-{STAMP}",
        email=f"{PREFIX}{tag}-{STAMP}@example.test",
        display_name=tag.title(),
        timezone="UTC",
        is_superadmin=False,
    )


async def add_role(user: Any, channel_id: str, role_key: RoleKey) -> None:
    """Give ``user`` a role in a channel."""
    role = await channel_repo.get_role_by_key(role_key)
    assert role is not None and role.id is not None and user.id is not None
    await channel_repo.upsert_channel_role(
        user_id=user.id,
        team_id="",
        channel_id=channel_id,
        role_id=role.id,
        assigned_by_id=None,
    )


async def ticket_count() -> int:
    async with database_service.engine.connect() as conn:
        return (
            await conn.execute(text("SELECT count(*) FROM escalation_tickets WHERE ticket_ref LIKE 'ESC-%'"))
        ).scalar_one()


async def cleanup() -> None:
    """Delete every row this (or an earlier, crashed) run created, in dependency order.

    Targets the channels schema: escalation tickets by channel id or by their
    users, ``channel_roles`` by channel id or user, then users. There is no
    ``cohorts``/``cohort_memberships`` table any more.
    """
    async with database_service.engine.begin() as conn:
        await conn.execute(
            text(
                "DELETE FROM escalation_tickets WHERE channel_id LIKE :p "
                "OR learner_id IN (SELECT id FROM users WHERE mattermost_user_id LIKE :p) "
                "OR assigned_human_id IN (SELECT id FROM users WHERE mattermost_user_id LIKE :p)"
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


# ---------------------------------------------------------------------------
# The scenario
# ---------------------------------------------------------------------------


async def scenario() -> None:
    """Run every assertion."""
    fake = FakeMattermost()
    escalation.mattermost_client = fake  # type: ignore[assignment]

    print("--- tool contract")
    check(
        "escalate_to_human takes only 'question' (no ticket_type, no identity args)",
        set(escalation_tools.escalate_to_human.args) == {"question"},
        str(sorted(escalation_tools.escalate_to_human.args)),
    )
    check(
        "escalate_to_human is in the learner-support group",
        any(t.name == "escalate_to_human" for t in LEARNER_SUPPORT_TOOLS),
    )
    check(
        "escalate_to_human is NOT in the back-office group",
        not any(t.name == "escalate_to_human" for t in BACK_OFFICE_TOOLS),
    )

    print("--- unbound (invoked outside a turn)")
    unbind()
    unbound_result = await escalation_tools.escalate_to_human.ainvoke({"question": "hello?"})
    check(
        "unbound call is refused/errors, not a crash",
        result_code_of(unbound_result) in (ResultCode.AUTHORISATION_REFUSED, ResultCode.SYSTEM_ERROR),
        unbound_result,
    )

    print("--- happy path: channel has an ops support")
    channel_a = channel_under_test("a")
    channel_b = channel_under_test("b")
    lead_a = await make_learner("lead-a")
    lead_b = await make_learner("lead-b")
    learner = await make_learner("learner")
    await add_role(lead_a, channel_a, RoleKey.OPS_SUPPORT)
    await add_role(lead_b, channel_b, RoleKey.OPS_SUPPORT)

    before_tickets = await ticket_count()
    bind(learner.mattermost_user_id, channel_a, thread_id=f"{PREFIX}thread-1")
    result = await escalation_tools.escalate_to_human.ainvoke({"question": "How many late days do I have left?"})
    check("happy path: tool reports ESCALATION_OPENED", result_code_of(result) == ResultCode.ESCALATION_OPENED, result)
    check("happy path: exactly one ticket row was added", await ticket_count() == before_tickets + 1)
    ticket = await escalation_repo.get_escalation_ticket_by_human_thread(fake.posts[-1]["id"])
    assert ticket is not None
    check("happy path: ticket_ref matches ESC-###### ", bool(TICKET_REF_RE.match(ticket.ticket_ref)), ticket.ticket_ref)
    check("happy path: ticket routed to channel A's ops support", ticket.assigned_human_id == lead_a.id)
    check("happy path: ticket status is waiting_human", ticket.status == "waiting_human")
    check("happy path: ticket channel is A, not B", ticket.channel_id == channel_a)
    check(
        "happy path: learner_channel_id / learner_thread_id recorded",
        ticket.channel_id == channel_a and ticket.learner_thread_id == f"{PREFIX}thread-1",
    )
    check("happy path: question stored verbatim", ticket.question == "How many late days do I have left?")
    check(
        "happy path: exactly one DM was sent, to A's ops support's channel",
        len(fake.posts) == 1 and fake.dm_channels.get(lead_a.mattermost_user_id) == fake.posts[0]["channel_id"],
    )
    check(
        "happy path: the DM to the human contains the ticket ref and the question",
        ticket.ticket_ref in fake.posts[0]["message"] and "late days" in fake.posts[0]["message"],
    )
    check(
        "happy path: the learner-facing message never names the human",
        lead_a.username not in result and (lead_a.display_name or "") not in result,
        result,
    )
    unbind()

    print("--- cross-channel scoping: channel B is untouched by A's escalation")
    check("cross-channel: B's ops support received no DM", fake.dm_channels.get(lead_b.mattermost_user_id) is None)

    print("--- idempotency: replaying the same thread does not duplicate anything")
    before = (await ticket_count(), len(fake.posts))
    bind(learner.mattermost_user_id, channel_a, thread_id=f"{PREFIX}thread-1")
    replay_result = await escalation_tools.escalate_to_human.ainvoke(
        {"question": "How many late days do I have left?"}
    )
    check(
        "idempotency: replay reports ESCALATION_ALREADY_OPEN",
        result_code_of(replay_result) == ResultCode.ESCALATION_ALREADY_OPEN,
        replay_result,
    )
    check("idempotency: no new ticket row was added", await ticket_count() == before[0])
    check("idempotency: no second DM was sent", len(fake.posts) == before[1])
    check(
        "idempotency: replay message still describes the real (handed-off) state honestly",
        ticket.ticket_ref in replay_result and "looped in a colleague" in replay_result.lower(),
        replay_result,
    )
    check(
        "idempotency: DB-level guard exists (partial unique index enforced even under a raced insert)",
        await escalation_repo.get_open_escalation_ticket_for_learner_thread(f"{PREFIX}thread-1") is not None,
    )
    unbind()

    print("--- no human assigned for the required role")
    channel_c = channel_under_test("c")
    learner_c = await make_learner("learner-c")
    before = (await ticket_count(), len(fake.posts))
    bind(learner_c.mattermost_user_id, channel_c)
    no_human_result = await escalation_tools.escalate_to_human.ainvoke({"question": "What's the leave policy?"})
    check(
        "no human: tool reports ESCALATION_OPENED_NO_HUMAN",
        result_code_of(no_human_result) == ResultCode.ESCALATION_OPENED_NO_HUMAN,
        no_human_result,
    )
    check("no human: a ticket row was still created", await ticket_count() == before[0] + 1)
    check("no human: no DM was attempted", len(fake.posts) == before[1])
    check(
        "no human: message is honest — mentions no one is assigned, not that someone is looking into it",
        "ops support" in no_human_result.lower() and "looped in a colleague" not in no_human_result.lower(),
        no_human_result,
    )
    no_human_ticket = await escalation_repo.list_escalation_tickets(channel_c)
    check(
        "no human: ticket stays open with no assigned human",
        len(no_human_ticket) == 1 and no_human_ticket[0].status == "open" and no_human_ticket[0].assigned_human_id is None,
    )
    before = (await ticket_count(), len(fake.posts))
    bind(learner_c.mattermost_user_id, channel_c)
    no_human_replay = await escalation_tools.escalate_to_human.ainvoke({"question": "What's the leave policy?"})
    check(
        "idempotency (no-human case): replay is ALREADY_OPEN, no duplicate row, message stays honest",
        result_code_of(no_human_replay) == ResultCode.ESCALATION_ALREADY_OPEN
        and await ticket_count() == before[0]
        and "ops support" in no_human_replay.lower(),
        no_human_replay,
    )
    unbind()

    print("--- human assigned but Mattermost is unreachable")
    channel_d = channel_under_test("d")
    lead_d = await make_learner("lead-d")
    learner_d = await make_learner("learner-d")
    await add_role(lead_d, channel_d, RoleKey.OPS_SUPPORT)
    fake.fail = True
    before = (await ticket_count(), len(fake.posts))
    bind(learner_d.mattermost_user_id, channel_d)
    unreachable_result = await escalation_tools.escalate_to_human.ainvoke({"question": "Is the demo cancelled?"})
    fake.fail = False
    check(
        "unreachable: tool still reports ESCALATION_OPENED (ticket exists, assigned)",
        result_code_of(unreachable_result) == ResultCode.ESCALATION_OPENED,
        unreachable_result,
    )
    check("unreachable: a ticket row was still created", await ticket_count() == before[0] + 1)
    check("unreachable: no DM was actually recorded", len(fake.posts) == before[1])
    check(
        "unreachable: message is honest — does not claim a colleague already has it",
        "couldn't reach them" in unreachable_result.lower() and "looped in a colleague" not in unreachable_result.lower(),
        unreachable_result,
    )
    unreachable_tickets = await escalation_repo.list_escalation_tickets(channel_d)
    check(
        "unreachable: ticket keeps its assigned human but status stays open (not waiting_human)",
        len(unreachable_tickets) == 1
        and unreachable_tickets[0].assigned_human_id == lead_d.id
        and unreachable_tickets[0].status == "open",
    )
    before = (await ticket_count(), len(fake.posts))
    bind(learner_d.mattermost_user_id, channel_d)
    unreachable_replay = await escalation_tools.escalate_to_human.ainvoke({"question": "Is the demo cancelled?"})
    check(
        "idempotency (unreachable case): replay is ALREADY_OPEN, no duplicate row, still honest",
        result_code_of(unreachable_replay) == ResultCode.ESCALATION_ALREADY_OPEN
        and await ticket_count() == before[0]
        and "couldn't reach them" in unreachable_replay.lower(),
        unreachable_replay,
    )
    unbind()

    print("--- two ops-support holders in one channel: earliest-assigned wins, deterministically")
    channel_e = channel_under_test("e")
    first_lead = await make_learner("first-lead")
    second_lead = await make_learner("second-lead")
    learner_e = await make_learner("learner-e")
    await add_role(first_lead, channel_e, RoleKey.OPS_SUPPORT)
    await add_role(second_lead, channel_e, RoleKey.OPS_SUPPORT)
    bind(learner_e.mattermost_user_id, channel_e)
    await escalation_tools.escalate_to_human.ainvoke({"question": "Which lead answers this?"})
    tie_tickets = await escalation_repo.list_escalation_tickets(channel_e)
    check(
        "tie: routed to whichever lead was assigned first, not arbitrarily",
        len(tie_tickets) == 1 and tie_tickets[0].assigned_human_id == first_lead.id,
    )
    unbind()

    print("--- edge cases that add no ticket row")
    before = await ticket_count()

    current_requester.set(
        RequesterContext(
            mattermost_user_id=learner.mattermost_user_id,
            channel_type="O",
        )
    )
    no_channel_result = await escalation_tools.escalate_to_human.ainvoke({"question": "still open?"})
    check(
        "no channel id in context -> VALIDATION_ERROR mentioning the channel",
        result_code_of(no_channel_result) == ResultCode.VALIDATION_ERROR and "channel" in no_channel_result.lower(),
        no_channel_result,
    )
    current_requester.set(None)

    bind(learner.mattermost_user_id, channel_a)
    empty_result = await escalation_tools.escalate_to_human.ainvoke({"question": "   "})
    check(
        "empty question -> VALIDATION_ERROR", result_code_of(empty_result) == ResultCode.VALIDATION_ERROR, empty_result
    )
    unbind()

    bind(f"{PREFIX}never-synced-{STAMP}", channel_a)
    unknown_learner_result = await escalation_tools.escalate_to_human.ainvoke({"question": "who am I?"})
    check(
        "learner never synced -> SYSTEM_ERROR, not a crash",
        result_code_of(unknown_learner_result) == ResultCode.SYSTEM_ERROR,
        unknown_learner_result,
    )
    unbind()

    check("edge cases: no ticket rows were added by any of them", await ticket_count() == before)

    print("--- channel id with no stored roles (no registry look-up to crash on)")
    channel_nowhere = channel_under_test("nowhere")
    before = (await ticket_count(), len(fake.posts))
    bind(learner.mattermost_user_id, channel_nowhere)
    nowhere_result = await escalation_tools.escalate_to_human.ainvoke({"question": "anyone there?"})
    unbind()
    check(
        "unknown channel -> ESCALATION_OPENED_NO_HUMAN, not a crash",
        result_code_of(nowhere_result) == ResultCode.ESCALATION_OPENED_NO_HUMAN,
        nowhere_result,
    )
    check(
        "unknown channel: message says nobody is assigned, does not claim a colleague is on it",
        "ops support" in nowhere_result.lower() and "looped in a colleague" not in nowhere_result.lower(),
        nowhere_result,
    )
    nowhere_tickets = await escalation_repo.list_escalation_tickets(channel_nowhere)
    check(
        "unknown channel: ticket stored unassigned (open) and no DM was attempted",
        len(nowhere_tickets) == 1
        and nowhere_tickets[0].assigned_human_id is None
        and nowhere_tickets[0].status == "open"
        and len(fake.posts) == before[1],
    )

    print("--- fallback: learner_thread_id defaults to channel_id when unset")
    channel_f = channel_under_test("f")
    lead_f = await make_learner("lead-f")
    learner_f = await make_learner("learner-f")
    await add_role(lead_f, channel_f, RoleKey.OPS_SUPPORT)
    bind(learner_f.mattermost_user_id, channel_f)  # no thread_id supplied
    await escalation_tools.escalate_to_human.ainvoke({"question": "fallback check"})
    fallback_tickets = await escalation_repo.list_escalation_tickets(channel_f)
    check(
        "fallback: learner_thread_id falls back to the channel id",
        len(fallback_tickets) == 1 and fallback_tickets[0].learner_thread_id == channel_f,
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


def main() -> int:
    """Run every check.

    Returns:
        int: 0 when all pass.
    """
    print("=" * 84)
    print("SprintFlow escalation handoff — in-container probe (channels era)")
    print("=" * 84)
    try:
        asyncio.run(main_async())
    except Exception as exc:  # noqa: BLE001 - report, never hide, a crash
        check(f"probe crashed: {type(exc).__name__}", False, str(exc)[:300])
    print("=" * 84)
    passed = sum(results)
    print(f"{passed}/{len(results)} checks passed")
    print("ESCALATION PROBE OK" if all(results) else "ESCALATION PROBE FAILED")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())