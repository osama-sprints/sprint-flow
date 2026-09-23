"""Database-backed proof of the calendar sync lifecycle. Runs only with
SPRINTFLOW_INTEGRATION_DB=1. Mirrors scripts/_calendar_probe.py, converted
to pytest assertions per the testing-sprint brief.
"""

import asyncio
import os
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from types import MappingProxyType
from typing import TypeVar
from unittest.mock import AsyncMock, MagicMock, patch

import httplib2
import pytest
from googleapiclient.errors import HttpError
from sqlalchemy import text

from app.core.config import settings
from app.core.requester import RequesterContext, current_requester
from app.models.enums import CeremonyTypeKey
from app.services import ceremony_scheduling as scheduling
from app.services.ceremony_scheduling import AmendmentProposal, ScheduleProposal, SchedulingProblem
from app.services.database import database_service
from app.services.domain import ceremonies as ceremony_repo
from app.services.domain import identity as identity_repo

pytestmark = pytest.mark.skipif(
    not os.getenv("SPRINTFLOW_INTEGRATION_DB"),
    reason="set SPRINTFLOW_INTEGRATION_DB=1 with POSTGRES_* pointing at a migrated throwaway database",
)

T = TypeVar("T")


def run(coro_factory: Callable[[], Awaitable[T]]) -> T:
    async def wrapped() -> T:
        try:
            return await coro_factory()
        finally:
            await database_service.close()

    return asyncio.run(wrapped())


def tag() -> str:
    return datetime.now(UTC).strftime("%H%M%S%f")


def _http_error(status: int) -> HttpError:
    resp = httplib2.Response({"status": status})
    resp.status = status
    return HttpError(resp, f'{{"error":"simulated {status}"}}'.encode())


async def _seed(prefix: str):
    organizer = await identity_repo.upsert_mattermost_user(
        mattermost_user_id=prefix,
        username=prefix,
        email=f"{prefix}@example.test",
        display_name="Integration Test Organizer",
        timezone="UTC",
        is_superadmin=True,
    )
    assert organizer.id is not None
    ctx = RequesterContext(
        mattermost_user_id=organizer.mattermost_user_id,
        username=organizer.username,
        email=organizer.email,
        channel_id=prefix,
        team_id=f"{prefix}-team",
        channel_type="O",
        user_id=organizer.id,
        is_superadmin=True,
        timezone="UTC",
        channel_roles=MappingProxyType({}),
    )
    planning_type = await ceremony_repo.get_ceremony_type_by_key(CeremonyTypeKey.SPRINT_PLANNING)
    assert planning_type is not None and planning_type.id is not None
    return organizer, ctx, planning_type


def _proposal(ctx, planning_type, start, *, with_meet: bool) -> ScheduleProposal:
    return ScheduleProposal(
        team_id=ctx.team_id,
        channel_id=ctx.channel_id,
        ceremony_type_id=planning_type.id,
        ceremony_type_key=CeremonyTypeKey.SPRINT_PLANNING.value,
        ceremony_type_label=planning_type.label,
        organizer_id=ctx.user_id,
        scheduled_at=start,
        duration_minutes=60,
        agenda="Integration test agenda",
        time_expression="test",
        zone="UTC",
        local_display="test",
        utc_display="test",
        with_meet=with_meet,
    )


async def _cleanup(prefix: str, user_ids: list[int]) -> None:
    async with database_service.session() as s:
        await s.exec(
            text(
                "DELETE FROM ceremony_amendments WHERE ceremony_id IN (SELECT id FROM ceremonies WHERE channel_id = :c)"
            ),
            params={"c": prefix},
        )
        await s.exec(text("DELETE FROM ceremonies WHERE channel_id = :c"), params={"c": prefix})
        if user_ids:
            await s.exec(text("DELETE FROM users WHERE id = ANY(:ids)"), params={"ids": user_ids})
        await s.commit()


def _enable_google_meet():
    settings.GOOGLE_MEET_ENABLED = True
    settings.MEETING_LINK_PROVIDER = "google_meet"
    settings.GOOGLE_SERVICE_ACCOUNT_CREDENTIALS = '{"type": "service_account"}'


def _reset_google_meet():
    settings.GOOGLE_MEET_ENABLED = False
    settings.MEETING_LINK_PROVIDER = "jitsi"
    settings.GOOGLE_SERVICE_ACCOUNT_CREDENTIALS = ""


def test_create_persists_meet_link_and_event_id():
    prefix = f"it-cal-create-{tag()}"

    async def scenario():
        user_ids: list[int] = []
        _enable_google_meet()
        try:
            organizer, ctx, planning_type = await _seed(prefix)
            user_ids.append(organizer.id)
            current_requester.set(ctx)
            with (
                patch("app.services.google_meet.build") as mock_build,
                patch("app.services.google_meet.service_account.Credentials.from_service_account_info", MagicMock()),
            ):
                mock_build.return_value.events.return_value.insert.return_value.execute = MagicMock(
                    return_value={
                        "id": "evt-create-1",
                        "conferenceData": {
                            "entryPoints": [{"entryPointType": "video", "uri": "https://meet.google.com/create-1"}]
                        },
                    }
                )
                start = datetime.now(UTC) + timedelta(days=1)
                created = await scheduling.commit_schedule(
                    _proposal(ctx, planning_type, start, with_meet=True), requester=ctx
                )

            assert not isinstance(created, SchedulingProblem), created
            assert created.meet_link == "https://meet.google.com/create-1"
            assert created.external_event_id == "evt-create-1"
            fresh = await ceremony_repo.get_ceremony(created.id)
            assert (
                fresh is not None and fresh.external_event_id == "evt-create-1"
            )  # actually persisted, not just returned
        finally:
            _reset_google_meet()
            await _cleanup(prefix, user_ids)

    run(scenario)


def test_reschedule_patches_the_same_stored_event_id():
    prefix = f"it-cal-resched-{tag()}"

    async def scenario():
        user_ids: list[int] = []
        _enable_google_meet()
        try:
            organizer, ctx, planning_type = await _seed(prefix)
            user_ids.append(organizer.id)
            current_requester.set(ctx)
            start = datetime.now(UTC) + timedelta(days=1)
            with (
                patch("app.services.google_meet.build") as mock_build,
                patch("app.services.google_meet.service_account.Credentials.from_service_account_info", MagicMock()),
            ):
                mock_build.return_value.events.return_value.insert.return_value.execute = MagicMock(
                    return_value={
                        "id": "evt-resched-1",
                        "conferenceData": {
                            "entryPoints": [{"entryPointType": "video", "uri": "https://meet.google.com/resched-1"}]
                        },
                    }
                )
                created = await scheduling.commit_schedule(
                    _proposal(ctx, planning_type, start, with_meet=True), requester=ctx
                )
            assert not isinstance(created, SchedulingProblem)

            new_start = start + timedelta(hours=3)
            with patch("app.services.meeting_link.update_meeting_link", AsyncMock(return_value=True)) as mock_update:
                amendment = AmendmentProposal(
                    ceremony_id=created.id,
                    team_id=ctx.team_id,
                    channel_id=ctx.channel_id,
                    ceremony_type_label=planning_type.label,
                    amended_by_id=ctx.user_id,
                    changes={"scheduled_at": new_start, "time_expression": "test reschedule", "time_zone": "UTC"},
                    reason="integration test reschedule",
                    cancel=False,
                    requires_confirmation=True,
                    previous_local_display="test",
                    previous_utc_display="test",
                    new_local_display="test",
                    new_utc_display="test",
                    conflict_warning=None,
                )
                updated, _trail = await scheduling.commit_amendment(amendment, requester=ctx)

            mock_update.assert_awaited_once()
            assert mock_update.await_args.args[0] == "evt-resched-1"  # no duplicate event created
            assert updated.external_event_id == "evt-resched-1"
            assert updated.scheduled_at == new_start
        finally:
            _reset_google_meet()
            await _cleanup(prefix, user_ids)

    run(scenario)


def test_cancel_uses_the_same_stored_event_id():
    prefix = f"it-cal-cancel-{tag()}"

    async def scenario():
        user_ids: list[int] = []
        _enable_google_meet()
        try:
            organizer, ctx, planning_type = await _seed(prefix)
            user_ids.append(organizer.id)
            current_requester.set(ctx)
            start = datetime.now(UTC) + timedelta(days=1)
            with (
                patch("app.services.google_meet.build") as mock_build,
                patch("app.services.google_meet.service_account.Credentials.from_service_account_info", MagicMock()),
            ):
                mock_build.return_value.events.return_value.insert.return_value.execute = MagicMock(
                    return_value={
                        "id": "evt-cancel-1",
                        "conferenceData": {
                            "entryPoints": [{"entryPointType": "video", "uri": "https://meet.google.com/cancel-1"}]
                        },
                    }
                )
                created = await scheduling.commit_schedule(
                    _proposal(ctx, planning_type, start, with_meet=True), requester=ctx
                )
            assert not isinstance(created, SchedulingProblem)

            with patch("app.services.meeting_link.cancel_meeting_link", AsyncMock(return_value=True)) as mock_cancel:
                amendment = AmendmentProposal(
                    ceremony_id=created.id,
                    team_id=ctx.team_id,
                    channel_id=ctx.channel_id,
                    ceremony_type_label=planning_type.label,
                    amended_by_id=ctx.user_id,
                    changes={"status": "cancelled"},
                    reason="integration test cancel",
                    cancel=True,
                    requires_confirmation=True,
                    previous_local_display="test",
                    previous_utc_display="test",
                    new_local_display=None,
                    new_utc_display=None,
                    conflict_warning=None,
                )
                cancelled, _trail = await scheduling.commit_amendment(amendment, requester=ctx)

            mock_cancel.assert_awaited_once_with("evt-cancel-1")
            assert cancelled.status == "cancelled"
        finally:
            _reset_google_meet()
            await _cleanup(prefix, user_ids)

    run(scenario)


def test_provider_outage_never_blocks_local_persistence_and_retries_are_bounded():
    prefix = f"it-cal-outage-{tag()}"

    async def scenario():
        user_ids: list[int] = []
        _enable_google_meet()
        try:
            organizer, ctx, planning_type = await _seed(prefix)
            user_ids.append(organizer.id)
            current_requester.set(ctx)
            start = datetime.now(UTC) + timedelta(days=1)
            with (
                patch("app.services.google_meet.build") as mock_build,
                patch("app.services.google_meet.service_account.Credentials.from_service_account_info", MagicMock()),
            ):
                mock_execute = MagicMock(side_effect=_http_error(503))
                mock_build.return_value.events.return_value.insert.return_value.execute = mock_execute
                created = await scheduling.commit_schedule(
                    _proposal(ctx, planning_type, start, with_meet=True), requester=ctx
                )

            assert not isinstance(created, SchedulingProblem), "an outage must never block local persistence"
            assert created.meet_link is None
            assert created.external_event_id is None
            assert mock_execute.call_count == 3  # bounded, not indefinite
        finally:
            _reset_google_meet()
            await _cleanup(prefix, user_ids)

    run(scenario)


def test_disabled_mode_persists_with_null_fields_and_makes_no_external_call():
    prefix = f"it-cal-disabled-{tag()}"

    async def scenario():
        user_ids: list[int] = []
        try:
            organizer, ctx, planning_type = await _seed(prefix)
            user_ids.append(organizer.id)
            current_requester.set(ctx)
            settings.GOOGLE_MEET_ENABLED = False
            start = datetime.now(UTC) + timedelta(days=1)
            with patch("app.services.google_meet.create_meet_event", AsyncMock()) as mock_create:
                created = await scheduling.commit_schedule(
                    _proposal(ctx, planning_type, start, with_meet=True), requester=ctx
                )

            assert not isinstance(created, SchedulingProblem)
            mock_create.assert_not_called()
            assert created.meet_link is None and created.external_event_id is None
        finally:
            await _cleanup(prefix, user_ids)

    run(scenario)
