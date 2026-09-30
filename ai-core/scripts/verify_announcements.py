"""Full-flow verification for Safe Cohort Announcements.

Run against a REAL migrated database (the announcements table must already
exist — see migration fix, item 1). send_to_mattermost is faked in-process
(patched) so no real Mattermost call happens, but every DB write is real.

Usage (from ai-core/, inside the container so DATABASE_URL etc. are set):
    uv run python scripts/verify_announcements.py

This is a standalone script (matching the style of scripts/verify_*.py at
the repo root), not a pytest file, since it's meant to be read top-to-bottom
as a demonstration, not just asserted against.
"""

import asyncio
from unittest.mock import AsyncMock, patch

from app.services import announcements as announcements_module
from app.services.announcements import (
    cancel_announcement,
    confirm_and_dispatch_announcement,
    create_announcement_preview,
)
from app.services.database import session_scope

# ---------------------------------------------------------------------------
# EDIT THESE to real, existing rows in your dev database before running —
# this script does not create a cohort/user, only announcements.
# ---------------------------------------------------------------------------
TEST_COHORT_ID = 1
TEST_REQUESTER_USER_ID = 1
TEST_RESOLVED_CHANNEL = "town-square"


def _fail(msg: str) -> None:
    print(f"  \u2717 FAIL: {msg}")
    raise SystemExit(1)


def _ok(msg: str) -> None:
    print(f"  \u2713 {msg}")


async def scenario_confirm_posts_exactly_once():
    print("\n--- Scenario 1: confirm -> exactly one post ---")
    with patch.object(announcements_module, "send_to_mattermost", new_callable=AsyncMock) as mock_send:
        mock_send.return_value = "mm_post_scenario1"
        async with session_scope() as session:
            preview = await create_announcement_preview(
                session=session,
                cohort_id=TEST_COHORT_ID,
                raw_text="[verify_announcements] scenario 1",
                delivery_mode="broadcast",
                resolved_channel=TEST_RESOLVED_CHANNEL,
                resolved_audience=[],
                created_by_user_id=TEST_REQUESTER_USER_ID,
            )
            announcement_id = preview["audit_id"]

            result = await confirm_and_dispatch_announcement(
                session, announcement_id, confirming_user_id=TEST_REQUESTER_USER_ID
            )

        if result["status"] != "success" or not result["dispatched"]:
            _fail(f"expected success/dispatched, got {result}")
        if mock_send.await_count != 1:
            _fail(f"expected exactly 1 Mattermost call, got {mock_send.await_count}")
        _ok(f"dispatched, 1 Mattermost call, post_id={result['mattermost_post_id']}")


async def scenario_cancel_posts_zero():
    print("\n--- Scenario 2: cancel -> zero posts ---")
    with patch.object(announcements_module, "send_to_mattermost", new_callable=AsyncMock) as mock_send:
        async with session_scope() as session:
            preview = await create_announcement_preview(
                session=session,
                cohort_id=TEST_COHORT_ID,
                raw_text="[verify_announcements] scenario 2 - should be cancelled",
                delivery_mode="broadcast",
                resolved_channel=TEST_RESOLVED_CHANNEL,
                resolved_audience=[],
                created_by_user_id=TEST_REQUESTER_USER_ID,
            )
            result = await cancel_announcement(session, preview["audit_id"])

        if not result["cancelled"]:
            _fail(f"expected cancelled=True, got {result}")
        if mock_send.await_count != 0:
            _fail(f"expected 0 Mattermost calls, got {mock_send.await_count}")
        _ok("cancelled, 0 Mattermost calls")


async def scenario_repeated_confirm_posts_exactly_once():
    print("\n--- Scenario 3: confirm called twice (replay) -> still exactly one post ---")
    with patch.object(announcements_module, "send_to_mattermost", new_callable=AsyncMock) as mock_send:
        mock_send.return_value = "mm_post_scenario3"
        async with session_scope() as session:
            preview = await create_announcement_preview(
                session=session,
                cohort_id=TEST_COHORT_ID,
                raw_text="[verify_announcements] scenario 3 - replay",
                delivery_mode="broadcast",
                resolved_channel=TEST_RESOLVED_CHANNEL,
                resolved_audience=[],
                created_by_user_id=TEST_REQUESTER_USER_ID,
            )
            announcement_id = preview["audit_id"]

            first = await confirm_and_dispatch_announcement(
                session, announcement_id, confirming_user_id=TEST_REQUESTER_USER_ID
            )
            second = await confirm_and_dispatch_announcement(
                session, announcement_id, confirming_user_id=TEST_REQUESTER_USER_ID
            )

        if not first["dispatched"]:
            _fail(f"expected first call to dispatch, got {first}")
        if second["dispatched"] or second["status"] != "already_processed":
            _fail(f"expected second call to no-op as already_processed, got {second}")
        if mock_send.await_count != 1:
            _fail(f"expected exactly 1 Mattermost call across both attempts, got {mock_send.await_count}")
        _ok("replayed confirm correctly no-op'd; still exactly 1 Mattermost call")


async def scenario_concurrent_confirm_posts_exactly_once():
    print("\n--- Scenario 3b: TWO CONCURRENT confirms -> still exactly one post ---")
    with patch.object(announcements_module, "send_to_mattermost", new_callable=AsyncMock) as mock_send:
        mock_send.return_value = "mm_post_scenario3b"
        async with session_scope() as session:
            preview = await create_announcement_preview(
                session=session,
                cohort_id=TEST_COHORT_ID,
                raw_text="[verify_announcements] scenario 3b - concurrent",
                delivery_mode="broadcast",
                resolved_channel=TEST_RESOLVED_CHANNEL,
                resolved_audience=[],
                created_by_user_id=TEST_REQUESTER_USER_ID,
            )
            announcement_id = preview["audit_id"]

        # Separate sessions per concurrent call — a single AsyncSession is
        # not safe to share across concurrent coroutines.
        async def _confirm():
            async with session_scope() as s:
                return await confirm_and_dispatch_announcement(
                    s, announcement_id, confirming_user_id=TEST_REQUESTER_USER_ID
                )

        results = await asyncio.gather(_confirm(), _confirm())
        dispatched_count = sum(1 for r in results if r["dispatched"])

        if dispatched_count != 1:
            _fail(f"expected exactly 1 of 2 concurrent confirms to dispatch, got {dispatched_count}")
        if mock_send.await_count != 1:
            _fail(f"expected exactly 1 real Mattermost call under concurrency, got {mock_send.await_count}")
        _ok("exactly 1 of 2 concurrent confirms dispatched; exactly 1 Mattermost call")


async def scenario_wrong_requester_cannot_confirm():
    print("\n--- Scenario 4: a different requester cannot confirm someone else's announcement ---")
    OTHER_USER_ID = TEST_REQUESTER_USER_ID + 999
    with patch.object(announcements_module, "send_to_mattermost", new_callable=AsyncMock) as mock_send:
        async with session_scope() as session:
            preview = await create_announcement_preview(
                session=session,
                cohort_id=TEST_COHORT_ID,
                raw_text="[verify_announcements] scenario 4",
                delivery_mode="broadcast",
                resolved_channel=TEST_RESOLVED_CHANNEL,
                resolved_audience=[],
                created_by_user_id=TEST_REQUESTER_USER_ID,
            )
            result = await confirm_and_dispatch_announcement(
                session, preview["audit_id"], confirming_user_id=OTHER_USER_ID
            )

        if result["dispatched"] or result["status"] != "not_authorized":
            _fail(f"expected not_authorized/no dispatch, got {result}")
        if mock_send.await_count != 0:
            _fail(f"expected 0 Mattermost calls, got {mock_send.await_count}")
        _ok("wrong requester correctly refused; 0 Mattermost calls")


async def main():
    print("=" * 60)
    print("ANNOUNCEMENTS — FULL FLOW VERIFICATION (real DB, fake Mattermost)")
    print("=" * 60)

    await scenario_confirm_posts_exactly_once()
    await scenario_cancel_posts_zero()
    await scenario_repeated_confirm_posts_exactly_once()
    await scenario_concurrent_confirm_posts_exactly_once()
    await scenario_wrong_requester_cannot_confirm()

    print("\n" + "=" * 60)
    print("ALL SCENARIOS PASSED")
    print("=" * 60)
    print(
        "\nNOTE: unauthorized/inactive-cohort refusal-path scenarios are NOT "
        "included here yet — they depend on request_announcement() and a "
        "confirmed AnnouncementOutcome.UNAUTHORIZED value (see announcements.py "
        "docstring for the flagged assumption). Add them once that's confirmed."
    )


if __name__ == "__main__":
    asyncio.run(main())
