"""Database-backed checks of the data-access layer's concurrency and boundary behaviour.

Skipped unless ``SPRINTFLOW_INTEGRATION_DB=1`` and the ``POSTGRES_*`` settings
point at a throwaway database that is already at Alembic head:

    docker exec sprintflow-devdb createdb -U sprintflow s1e1
    APP_ENV=test POSTGRES_DB=s1e1 ... .venv/bin/alembic upgrade head
    SPRINTFLOW_INTEGRATION_DB=1 APP_ENV=test POSTGRES_DB=s1e1 ... .venv/bin/python -m pytest -q tests/integration

Every test tags the rows it creates and removes them afterwards.
"""

import asyncio
import os
import re
from collections.abc import Awaitable, Callable
from datetime import (
    UTC,
    date,
    datetime,
    timedelta,
)
from typing import TypeVar

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.models.enums import (
    CeremonyStatus,
    CeremonyTypeKey,
    EscalationType,
    OnboardingStepKind,
    RoleKey,
)
from app.services.database import (
    database_service,
    session_scope,
)
from app.services.domain import ceremonies as ceremony_repo
from app.services.domain import channels as channel_repo
from app.services.domain import escalations as escalation_repo
from app.services.domain import identity as identity_repo
from app.services.domain import onboarding as onboarding_repo
from app.services.domain import sprints as sprint_repo

pytestmark = pytest.mark.skipif(
    not os.getenv("SPRINTFLOW_INTEGRATION_DB"),
    reason="set SPRINTFLOW_INTEGRATION_DB=1 with POSTGRES_* pointing at a migrated throwaway database",
)

T = TypeVar("T")


def run(coro_factory: Callable[[], Awaitable[T]]) -> T:
    """Run one coroutine on a fresh event loop and dispose the engine afterwards (it is loop-bound)."""

    async def wrapped() -> T:
        try:
            return await coro_factory()
        finally:
            await database_service.close()

    return asyncio.run(wrapped())


def tag() -> str:
    return datetime.now(UTC).strftime("%H%M%S%f")


async def make_user(prefix: str, index: int = 0):
    return await identity_repo.upsert_mattermost_user(
        mattermost_user_id=f"{prefix}-{index}",
        username=f"{prefix}{index}",
        email=None,
        display_name=None,
        timezone="UTC",
        is_superadmin=False,
    )


async def cleanup(prefix: str, channel_ids: list[int]) -> None:
    async with session_scope() as s:
        params = {"p": f"{prefix}%", "channels": channel_ids or [-1]}
        await s.exec(
            text(
                "DELETE FROM onboarding_steps WHERE user_id IN (SELECT id FROM users WHERE mattermost_user_id LIKE :p)"
            ),
            params=params,
        )  # type: ignore[call-overload]
        await s.exec(text("DELETE FROM escalation_tickets WHERE channel_id = ANY(:channels)"), params=params)  # type: ignore[call-overload]
        await s.exec(
            text(
                "DELETE FROM ceremony_amendments WHERE ceremony_id IN (SELECT id FROM ceremonies WHERE channel_id = ANY(:channels))"
            ),
            params=params,
        )  # type: ignore[call-overload]
        await s.exec(text("DELETE FROM ceremonies WHERE channel_id = ANY(:channels)"), params=params)  # type: ignore[call-overload]
        await s.exec(text("DELETE FROM sprints WHERE channel_id = ANY(:channels)"), params=params)  # type: ignore[call-overload]
        await s.exec(text("DELETE FROM channel_memberships WHERE channel_id = ANY(:channels)"), params=params)  # type: ignore[call-overload]
        await s.exec(text("DELETE FROM channels WHERE id = ANY(:channels)"), params=params)  # type: ignore[call-overload]
        await s.exec(text("DELETE FROM users WHERE mattermost_user_id LIKE :p"), params=params)  # type: ignore[call-overload]


def test_concurrent_identity_upserts_for_one_mattermost_id_converge_on_one_row():
    prefix = f"it-id-{tag()}"

    async def scenario():
        try:
            users = await asyncio.gather(*(make_user(prefix) for _ in range(10)))
            assert len({u.id for u in users}) == 1
        finally:
            await cleanup(prefix, [])

    run(scenario)


def test_concurrent_enqueue_step_creates_exactly_one_row():
    prefix = f"it-enq-{tag()}"

    async def scenario():
        try:
            user = await make_user(prefix)
            assert user.id
            due = datetime.now(UTC)
            outcomes = await asyncio.gather(
                *(
                    onboarding_repo.enqueue_step(
                        user_id=user.id, channel_id=None, step_kind=OnboardingStepKind.WELCOME, due_at=due
                    )
                    for _ in range(10)
                )
            )
            assert sum(1 for _, created in outcomes if created) == 1
            assert len({step.id for step, _ in outcomes}) == 1
            with pytest.raises(ValueError):
                await onboarding_repo.enqueue_step(
                    user_id=user.id,
                    channel_id=None,
                    step_kind=OnboardingStepKind.FOLLOW_UP,
                    due_at=datetime(2030, 1, 1),
                )
        finally:
            await cleanup(prefix, [])

    run(scenario)


def test_two_workers_claim_disjoint_steps_with_skip_locked():
    prefix = f"it-claim-{tag()}"

    async def scenario():
        channel_ids: list[int] = []
        try:
            channel_id = f"Claim-{prefix}"
            channel_ids.append(channel_id)
            due = datetime.now(UTC) - timedelta(minutes=1)
            for i in range(10):
                user = await make_user(prefix, i)
                assert user.id
                await onboarding_repo.enqueue_step(
                    user_id=user.id, channel_id=channel_id, step_kind=OnboardingStepKind.ORIENTATION, due_at=due
                )
            session_a = database_service.session()
            try:
                claimed_a = await onboarding_repo.claim_due_steps(
                    worker_id="A", lease_seconds=120, limit=5, session=session_a
                )
                claimed_b = await onboarding_repo.claim_due_steps(worker_id="B", lease_seconds=120, limit=50)
                await session_a.commit()
            finally:
                await session_a.close()
            a_ids = {s.id for s in claimed_a}
            b_ids = {s.id for s in claimed_b}
            assert len(a_ids) == 5 and len(b_ids) == 5
            assert not a_ids & b_ids
            assert not await onboarding_repo.claim_due_steps(worker_id="C", lease_seconds=120, limit=50)
            later = datetime.now(UTC) + timedelta(seconds=121)
            assert (
                len(await onboarding_repo.claim_due_steps(worker_id="D", lease_seconds=120, limit=50, now=later)) == 10
            )
        finally:
            await cleanup(prefix, channel_ids)

    run(scenario)


def test_concurrent_first_role_assignment_converges_on_one_membership():
    prefix = f"it-mem-{tag()}"

    async def scenario():
        channel_ids: list[int] = []
        try:
            user = await make_user(prefix)
            channel = await channel_repo.create_channel(f"Mem-{prefix}")
            learner = await channel_repo.get_role_by_key(RoleKey.LEARNER)
            lead = await channel_repo.get_role_by_key(RoleKey.TECH_LEAD)
            assert user.id and channel_id and learner and learner.id and lead and lead.id
            channel_ids.append(channel_id)
            changes = await asyncio.gather(
                *(
                    channel_repo.upsert_membership(
                        user_id=user.id, channel_id=channel_id, role_id=learner.id, assigned_by_id=None
                    )
                    for _ in range(10)
                )
            )
            assert len({c.membership.id for c in changes}) == 1
            assert sum(1 for c in changes if c.created) == 1
            change = await channel_repo.upsert_membership(
                user_id=user.id, channel_id=channel_id, role_id=lead.id, assigned_by_id=None
            )
            assert change.previous_role_id == learner.id and not change.created
            with pytest.raises(IntegrityError):
                async with session_scope() as s:
                    await s.exec(  # type: ignore[call-overload]
                        text("INSERT INTO channel_memberships (channel_id, user_id, role_id) VALUES (:c, :u, :r)"),
                        params={"c": channel_id, "u": user.id, "r": learner.id},
                    )
        finally:
            await cleanup(prefix, channel_ids)

    run(scenario)


def test_ceremony_overlap_boundaries_and_amendment_trail():
    prefix = f"it-cer-{tag()}"

    async def scenario():
        channel_ids: list[int] = []
        try:
            user = await make_user(prefix)
            channel = await channel_repo.create_channel(f"Cer-{prefix}")
            ctype = await ceremony_repo.get_ceremony_type_by_key(CeremonyTypeKey.DAILY_STANDUP)
            assert user.id and channel_id and ctype and ctype.id
            channel_ids.append(channel_id)
            start = datetime(2030, 6, 1, 10, 0, tzinfo=UTC)
            ceremony = await ceremony_repo.create_ceremony(
                channel_id=channel_id,
                ceremony_type_id=ctype.id,
                organizer_id=user.id,
                scheduled_at=start,
                duration_minutes=30,
            )
            assert ceremony.id
            assert not await ceremony_repo.find_overlapping_ceremonies(channel_id, start + timedelta(minutes=30), 30)
            assert not await ceremony_repo.find_overlapping_ceremonies(channel_id, start - timedelta(minutes=30), 30)
            hits = await ceremony_repo.find_overlapping_ceremonies(channel_id, start + timedelta(minutes=29), 30)
            assert [c.id for c in hits] == [ceremony.id]
            assert not await ceremony_repo.find_overlapping_ceremonies(channel_id, start, 30, exclude_id=ceremony.id)
            with pytest.raises(ValueError):
                await ceremony_repo.update_ceremony(ceremony.id, amended_by_id=user.id, changes={"channel_id": 1})
            with pytest.raises(ValueError):
                await ceremony_repo.update_ceremony(
                    ceremony.id, amended_by_id=user.id, changes={"scheduled_at": datetime(2030, 6, 1, 11)}
                )
            await ceremony_repo.update_ceremony(
                ceremony.id,
                amended_by_id=user.id,
                changes={"agenda": "x", "duration_minutes": 30, "status": CeremonyStatus.CANCELLED},
            )
            trail = await ceremony_repo.list_amendments(ceremony.id)
            assert sorted(a.field for a in trail) == ["agenda", "status"]
            assert not await ceremony_repo.find_overlapping_ceremonies(channel_id, start, 30)
        finally:
            await cleanup(prefix, channel_ids)

    run(scenario)


def test_concurrent_escalation_tickets_get_unique_references():
    prefix = f"it-esc-{tag()}"

    async def scenario():
        channel_ids: list[int] = []
        try:
            user = await make_user(prefix)
            channel = await channel_repo.create_channel(f"Esc-{prefix}")
            assert user.id and channel_id
            channel_ids.append(channel_id)
            tickets = await asyncio.gather(
                *(
                    escalation_repo.create_escalation_ticket(
                        channel_id=channel_id,
                        learner_id=user.id,
                        ticket_type=EscalationType.TECH,
                        question=f"q{i}",
                        learner_channel_id="c",
                        learner_thread_id=f"t{i}",
                    )
                    for i in range(10)
                )
            )
            refs = [t.ticket_ref for t in tickets]
            assert len(set(refs)) == 10
            assert all(re.fullmatch(r"ESC-\d{6}", ref) for ref in refs)
            found = await escalation_repo.get_escalation_ticket(refs[0].lower())
            assert found and found.ticket_ref == refs[0]
        finally:
            await cleanup(prefix, channel_ids)

    run(scenario)


def test_sprint_overlap_boundaries():
    prefix = f"it-spr-{tag()}"

    async def scenario():
        channel_ids: list[int] = []
        try:
            channel_id = f"Spr-{prefix}"
            channel_ids.append(channel_id)
            sprint = await sprint_repo.create_sprint(
                channel_id=channel_id, name="Sprint 1", start_date=date(2030, 1, 1), end_date=date(2030, 1, 14)
            )
            assert not await sprint_repo.find_overlapping_sprints(channel_id, date(2030, 1, 15), date(2030, 1, 28))
            same_day = await sprint_repo.find_overlapping_sprints(channel_id, date(2030, 1, 14), date(2030, 1, 28))
            assert [s.id for s in same_day] == [sprint.id]
        finally:
            await cleanup(prefix, channel_ids)

    run(scenario)


def test_sprint_names_are_unique_case_insensitively():
    """Revision 0002: 'Sprint 1' and 'sprint 1' cannot coexist in one channel (the service's rule, enforced by the DB)."""
    prefix = f"it-spr-ci-{tag()}"

    async def scenario():
        channel_ids: list[int] = []
        try:
            channel_id = f"Spr-{prefix}"
            channel_ids.append(channel_id)
            await sprint_repo.create_sprint(
                channel_id=channel_id, name="Sprint 1", start_date=date(2030, 2, 1), end_date=date(2030, 2, 14)
            )
            with pytest.raises(IntegrityError):
                await sprint_repo.create_sprint(
                    channel_id=channel_id, name="sprint 1", start_date=date(2030, 3, 1), end_date=date(2030, 3, 14)
                )
        finally:
            await cleanup(prefix, channel_ids)

    run(scenario)
