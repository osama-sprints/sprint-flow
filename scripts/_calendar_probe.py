"""In-container calendar integration probe, driven by ``scripts/verify_calendar_integration.py``
over stdin.

Exercises ``commit_schedule``/``commit_amendment`` against the live database with
``app.services.google_meet``'s Calendar API client mocked at the ``build()``
boundary (same technique ``tests/test_google_meet.py`` uses), so the real
retry/idempotency/wiring code runs, only the actual HTTP call to Google is faked.

Modes (``argv[1]``):

- ``checks``         — run every scenario, print one PASS/FAIL line per assertion,
                        clean up, exit non-zero on any failure.
- ``cleanup <stamp>``  — best-effort cleanup if a previous run was interrupted.

Run in the stack:  docker compose exec -T ai-core /app/.venv/bin/python - checks < scripts/_calendar_probe.py
"""

import json
import os
import sys
from datetime import UTC, datetime, timedelta
from types import MappingProxyType
from unittest.mock import AsyncMock, MagicMock, patch

import httplib2
from googleapiclient.errors import HttpError
from sqlalchemy import text

sys.path.insert(0, "/app")
sys.path.insert(0, os.getcwd())

from app.core.config import settings  # noqa: E402
from app.core.requester import RequesterContext, current_requester  # noqa: E402
from app.models.enums import CeremonyTypeKey  # noqa: E402
from app.services import ceremony_scheduling as scheduling  # noqa: E402
from app.services.ceremony_scheduling import AmendmentProposal, ScheduleProposal  # noqa: E402
from app.services.database import database_service  # noqa: E402
from app.services.domain import ceremonies as ceremony_repo  # noqa: E402
from app.services.domain import identity as identity_repo  # noqa: E402

PREFIX = "verify-cal-"
results: list[bool] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    """Record and print one assertion."""
    results.append(ok)
    suffix = f"  ({detail[:200]})" if detail and not ok else ""
    print(f"  {label:70} {'PASS' if ok else 'FAIL'}{suffix}")


def fake_http_error(status: int) -> HttpError:
    """Build a real HttpError with a given status, the way googleapiclient itself raises one."""
    resp = httplib2.Response({"status": status})
    resp.status = status
    return HttpError(resp, f'{{"error":"simulated {status}"}}'.encode())


async def delete_probe_rows(user_ids: list[int], channel_id: str) -> None:
    """Delete everything this probe created, in dependency order."""
    async with database_service.session() as s:
        await s.exec(
            text("DELETE FROM ceremony_amendments WHERE ceremony_id IN (SELECT id FROM ceremonies WHERE channel_id = :c)"),
            params={"c": channel_id},
        )
        await s.exec(text("DELETE FROM ceremonies WHERE channel_id = :c"), params={"c": channel_id})
        if user_ids:
            await s.exec(text("DELETE FROM users WHERE id = ANY(:ids)"), params={"ids": user_ids})
        await s.commit()


async def checks() -> None:
    """Exercise create/reschedule/cancel/outage/disabled, then clean up."""
    stamp = str(int(datetime.now(UTC).timestamp()))
    channel_id = f"{PREFIX}{stamp}"
    user_ids: list[int] = []

    organizer = await identity_repo.upsert_mattermost_user(
        mattermost_user_id=f"{PREFIX}{stamp}",
        username=f"{PREFIX}{stamp}",
        email=f"{PREFIX}{stamp}@example.test",
        display_name="Probe Organizer",
        timezone="UTC",
        is_superadmin=True,  # bypass channel-role checks; not what this probe is testing
    )
    assert organizer.id is not None
    user_ids.append(organizer.id)

    ctx = RequesterContext(
        mattermost_user_id=organizer.mattermost_user_id,
        username=organizer.username,
        email=organizer.email,
        channel_id=channel_id,
        team_id=f"{PREFIX}{stamp}-team",
        channel_type="O",
        user_id=organizer.id,
        is_superadmin=True,
        timezone="UTC",
        channel_roles=MappingProxyType({}),
    )
    current_requester.set(ctx)

    planning_type = await ceremony_repo.get_ceremony_type_by_key(CeremonyTypeKey.SPRINT_PLANNING)
    assert planning_type is not None and planning_type.id is not None

    def proposal(start: datetime, *, with_meet: bool) -> ScheduleProposal:
        return ScheduleProposal(
            team_id=ctx.team_id,
            channel_id=channel_id,
            ceremony_type_id=planning_type.id,  # type: ignore[arg-type]
            ceremony_type_key=CeremonyTypeKey.SPRINT_PLANNING.value,
            ceremony_type_label=planning_type.label,
            organizer_id=organizer.id,  # type: ignore[arg-type]
            scheduled_at=start,
            duration_minutes=60,
            agenda="Probe agenda",
            time_expression="probe",
            zone="UTC",
            local_display="probe",
            utc_display="probe",
            with_meet=with_meet,
        )

    settings.GOOGLE_MEET_ENABLED = True
    settings.MEETING_LINK_PROVIDER = "google_meet"
    settings.GOOGLE_SERVICE_ACCOUNT_CREDENTIALS = '{"type": "service_account"}'

    try:
        # --- 1. Create success -------------------------------------------------
        with (
            patch("app.services.google_meet.build") as mock_build,
            patch("app.services.google_meet.service_account.Credentials.from_service_account_info", MagicMock()),
        ):
            mock_execute = MagicMock(
                return_value={
                    "id": "evt-create-1",
                    "conferenceData": {"entryPoints": [{"entryPointType": "video", "uri": "https://meet.google.com/create-1"}]},
                }
            )
            mock_build.return_value.events.return_value.insert.return_value.execute = mock_execute
            start = datetime.now(UTC) + timedelta(days=1)
            created = await scheduling.commit_schedule(proposal(start, with_meet=True), requester=ctx)

        is_ceremony = not isinstance(created, scheduling.SchedulingProblem)
        check("create: no conflict/refusal", is_ceremony, str(created))
        if is_ceremony:
            check("create: meet_link stored", created.meet_link == "https://meet.google.com/create-1", created.meet_link)
            check("create: external_event_id stored", created.external_event_id == "evt-create-1", created.external_event_id)
            fresh = await ceremony_repo.get_ceremony(created.id)  # type: ignore[arg-type]
            check("create: persisted row matches", fresh is not None and fresh.external_event_id == "evt-create-1")
        else:
            check("create: external_event_id stored", False, "skipped, no ceremony")
            check("create: persisted row matches", False, "skipped, no ceremony")
            return

        ceremony_id = created.id
        assert ceremony_id is not None

        # --- 2. Reschedule -------------------------------------------------------
        new_start = start + timedelta(hours=3)
        with patch("app.services.meeting_link.update_meeting_link", AsyncMock(return_value=True)) as mock_update:
            amendment = AmendmentProposal(
                ceremony_id=ceremony_id,
                team_id=ctx.team_id,
                channel_id=channel_id,
                ceremony_type_label=planning_type.label,
                amended_by_id=organizer.id,  # type: ignore[arg-type]
                changes={"scheduled_at": new_start, "time_expression": "probe reschedule", "time_zone": "UTC"},
                reason="probe reschedule",
                cancel=False,
                requires_confirmation=True,
                previous_local_display="probe",
                previous_utc_display="probe",
                new_local_display="probe",
                new_utc_display="probe",
                conflict_warning=None,
            )
            updated, _trail = await scheduling.commit_amendment(amendment, requester=ctx)

        check("reschedule: update_meeting_link called once", mock_update.await_count == 1, str(mock_update.await_args_list))
        check(
            "reschedule: called with the same external_event_id (no new event)",
            mock_update.await_args is not None and mock_update.await_args.args[0] == "evt-create-1",
        )
        check("reschedule: external_event_id unchanged", updated.external_event_id == "evt-create-1")
        check("reschedule: scheduled_at actually moved", updated.scheduled_at == new_start)

        # --- 3. Cancellation -------------------------------------------------------
        with patch("app.services.meeting_link.cancel_meeting_link", AsyncMock(return_value=True)) as mock_cancel:
            cancel_amendment = AmendmentProposal(
                ceremony_id=ceremony_id,
                team_id=ctx.team_id,
                channel_id=channel_id,
                ceremony_type_label=planning_type.label,
                amended_by_id=organizer.id,  # type: ignore[arg-type]
                changes={"status": "cancelled"},
                reason="probe cancel",
                cancel=True,
                requires_confirmation=True,
                previous_local_display="probe",
                previous_utc_display="probe",
                new_local_display=None,
                new_utc_display=None,
                conflict_warning=None,
            )
            cancelled, _trail = await scheduling.commit_amendment(cancel_amendment, requester=ctx)

        check("cancel: cancel_meeting_link called once", mock_cancel.await_count == 1)
        check(
            "cancel: called with the same external_event_id",
            mock_cancel.await_args is not None and mock_cancel.await_args.args[0] == "evt-create-1",
        )
        check("cancel: ceremony status is cancelled", cancelled.status == "cancelled", cancelled.status)

        # --- 4. Provider outage never blocks local persistence --------------------
        with (
            patch("app.services.google_meet.build") as mock_build_outage,
            patch("app.services.google_meet.service_account.Credentials.from_service_account_info", MagicMock()),
        ):
            mock_execute_outage = MagicMock(side_effect=fake_http_error(503))
            mock_build_outage.return_value.events.return_value.insert.return_value.execute = mock_execute_outage
            outage_start = start + timedelta(days=2)
            outage_created = await scheduling.commit_schedule(proposal(outage_start, with_meet=True), requester=ctx)

        outage_is_ceremony = not isinstance(outage_created, scheduling.SchedulingProblem)
        check("outage: ceremony still persists despite provider failure", outage_is_ceremony, str(outage_created))
        if outage_is_ceremony:
            check("outage: meet_link stayed null", outage_created.meet_link is None)
            check("outage: external_event_id stayed null", outage_created.external_event_id is None)
            check(
                "outage: retried a bounded number of times (3), not indefinitely",
                mock_execute_outage.call_count == 3,
                f"call_count={mock_execute_outage.call_count}",
            )
        else:
            check("outage: meet_link stayed null", False, "skipped, no ceremony")
            check("outage: external_event_id stayed null", False, "skipped, no ceremony")
            check("outage: retried a bounded number of times (3), not indefinitely", False, "skipped, no ceremony")

        # --- 5. Disabled/unconfigured mode -----------------------------------------
        settings.GOOGLE_MEET_ENABLED = False
        with patch("app.services.google_meet.create_meet_event", AsyncMock()) as mock_create_disabled:
            disabled_start = start + timedelta(days=3)
            disabled_created = await scheduling.commit_schedule(proposal(disabled_start, with_meet=True), requester=ctx)

        disabled_is_ceremony = not isinstance(disabled_created, scheduling.SchedulingProblem)
        check("disabled: scheduling still succeeds", disabled_is_ceremony, str(disabled_created))
        if disabled_is_ceremony:
            check("disabled: no external call was made", mock_create_disabled.await_count == 0)
            check("disabled: fields stay null", disabled_created.meet_link is None and disabled_created.external_event_id is None)
        else:
            check("disabled: no external call was made", False, "skipped, no ceremony")
            check("disabled: fields stay null", False, "skipped, no ceremony")

    finally:
        settings.GOOGLE_MEET_ENABLED = False
        settings.MEETING_LINK_PROVIDER = "jitsi"
        settings.GOOGLE_SERVICE_ACCOUNT_CREDENTIALS = ""
        await delete_probe_rows(user_ids, channel_id)


async def cleanup(stamp: str) -> dict:
    """Best-effort cleanup for a specific stamp, if a prior run was interrupted."""
    channel_id = f"{PREFIX}{stamp}"
    async with database_service.session() as s:
        rows = await s.exec(text("SELECT id FROM users WHERE mattermost_user_id = :m"), params={"m": channel_id})
        ids = [r for (r,) in rows.all()]
    await delete_probe_rows(ids, channel_id)
    return {"cleaned": True}


async def main(argv: list[str]) -> int:
    """Dispatch by mode; ``checks`` exits non-zero if any assertion failed."""
    if not argv:
        print("usage: _calendar_probe.py <checks|cleanup>", file=sys.stderr)
        return 2
    mode = argv[0]
    if mode == "checks":
        await checks()
        return 0 if all(results) else 1
    if mode == "cleanup":
        print(json.dumps(await cleanup(argv[1])))
        return 0
    print(f"unknown mode: {mode}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    import asyncio

    sys.exit(asyncio.run(main(sys.argv[1:])))