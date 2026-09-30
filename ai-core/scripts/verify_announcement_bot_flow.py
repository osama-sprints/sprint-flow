from sqlalchemy import Table, Column, Integer
from app.models.announcement import Announcement
Table("cohorts", Announcement.metadata, Column("id", Integer, primary_key=True), extend_existing=True)
"""End-to-end verification through the TOOL layer — as close as we get to
"talking to the bot" without actually running the LLM/graph/Mattermost
webhook. Calls prepare_announcement_preview_tool / confirm_announcement_tool
/ cancel_announcement_tool exactly as the graph would invoke them, against a
REAL database. Only the outbound Mattermost call is faked.

Usage (inside the container, so DATABASE_URL etc. are set):
    uv run python scripts/verify_announcement_bot_flow.py

EDIT THESE FIRST to real, existing rows in your dev database:
"""

import asyncio
from unittest.mock import AsyncMock, patch

from app.core.requester import RequesterContext, current_requester
from app.core.langgraph.tools.back_office import (
    cancel_announcement_tool,
    confirm_announcement_tool,
    prepare_announcement_preview_tool,
)
from app.services import announcements as announcements_module

# ---------------------------------------------------------------------------
# EDIT THESE to real, existing rows in your dev database before running.
# TECH_LEAD_* must hold tech_lead or scrum_master in TEST_COHORT_ID.
# LEARNER_* must hold learner (or nothing) in TEST_COHORT_ID.
# ---------------------------------------------------------------------------
TEST_COHORT_ID = 1
TECH_LEAD_MATTERMOST_ID = "mm-user-techlead-1"
TECH_LEAD_USER_ID = 1  # the `users.id` row matching TECH_LEAD_MATTERMOST_ID
LEARNER_MATTERMOST_ID = "mm-user-learner-1"
LEARNER_USER_ID = 2
OTHER_TECH_LEAD_USER_ID = 3  # a DIFFERENT tech lead, for the wrong-requester test


def _fail(msg: str) -> None:
    print(f"  \u2717 FAIL: {msg}")
    raise SystemExit(1)


def _ok(msg: str) -> None:
    print(f"  \u2713 {msg}")


def _set_tech_lead():
    current_requester.set(
        RequesterContext(
            mattermost_user_id=TECH_LEAD_MATTERMOST_ID,
            channel_roles={TEST_COHORT_ID: "tech_lead"},
        )
    )


def _set_learner():
    current_requester.set(
        RequesterContext(
            mattermost_user_id=LEARNER_MATTERMOST_ID,
            channel_roles={TEST_COHORT_ID: "learner"},
        )
    )


async def scenario_authorized_full_conversation():
    print("\n--- Scenario 1: tech lead asks to announce -> preview -> confirm -> posted ---")
    _set_tech_lead()
    with patch.object(
        announcements_module.mattermost_client, "create_post", new_callable=AsyncMock
    ) as mock_post:
        mock_post.return_value = {"id": "mm_post_bot_flow_1"}

        preview = await prepare_announcement_preview_tool.ainvoke(
            {
                "cohort_id": TEST_COHORT_ID,
                "raw_text": "[verify_bot_flow] scenario 1",
                "delivery_mode": "broadcast",
                "target_type": "role",
                "target_value": "learner",
            }
        )
        if "audit_id" not in preview:
            _fail(f"expected a preview with audit_id, got {preview}")
        _ok(f"preview created, audit_id={preview['audit_id']}, audience={preview['preview']['resolved_audience']}")

        result = await confirm_announcement_tool.ainvoke({"announcement_id": preview["audit_id"]})
        if result["status"] != "success" or not result["dispatched"]:
            _fail(f"expected dispatched success, got {result}")
        if mock_post.await_count != 1:
            _fail(f"expected exactly 1 Mattermost call, got {mock_post.await_count}")
        _ok(f"confirmed and dispatched, post_id={result['mattermost_post_id']}")


async def scenario_learner_cannot_announce():
    print("\n--- Scenario 2: learner tries to announce -> refused before preview, audited ---")
    _set_learner()
    with patch.object(
        announcements_module.mattermost_client, "create_post", new_callable=AsyncMock
    ) as mock_post:
        result = await prepare_announcement_preview_tool.ainvoke(
            {
                "cohort_id": TEST_COHORT_ID,
                "raw_text": "[verify_bot_flow] scenario 2 - should be refused",
                "delivery_mode": "broadcast",
                "target_type": "role",
                "target_value": "learner",
            }
        )
        if result.get("status") != "unauthorized":
            _fail(f"expected status=unauthorized, got {result}")
        if mock_post.await_count != 0:
            _fail(f"expected 0 Mattermost calls, got {mock_post.await_count}")
        _ok(f"learner correctly refused before any preview/post: {result['message']!r}")
        print("  (check the announcements table by hand: latest row for this cohort should show outcome=unauthorized)")


async def scenario_replay_confirm():
    print("\n--- Scenario 3: confirming the same announcement twice -> one post only ---")
    _set_tech_lead()
    with patch.object(
        announcements_module.mattermost_client, "create_post", new_callable=AsyncMock
    ) as mock_post:
        mock_post.return_value = {"id": "mm_post_bot_flow_3"}

        preview = await prepare_announcement_preview_tool.ainvoke(
            {
                "cohort_id": TEST_COHORT_ID,
                "raw_text": "[verify_bot_flow] scenario 3 - replay",
                "delivery_mode": "broadcast",
                "target_type": "role",
                "target_value": "learner",
            }
        )
        announcement_id = preview["audit_id"]

        first = await confirm_announcement_tool.ainvoke({"announcement_id": announcement_id})
        second = await confirm_announcement_tool.ainvoke({"announcement_id": announcement_id})

        if not first["dispatched"]:
            _fail(f"expected first confirm to dispatch, got {first}")
        if second["dispatched"] or second["status"] != "already_processed":
            _fail(f"expected second confirm to no-op, got {second}")
        if mock_post.await_count != 1:
            _fail(f"expected exactly 1 Mattermost call across both confirms, got {mock_post.await_count}")
        _ok("replayed confirm correctly no-op'd; exactly 1 Mattermost call")


async def scenario_wrong_requester_cannot_confirm():
    print("\n--- Scenario 4: a different tech lead cannot confirm someone else's announcement ---")
    _set_tech_lead()
    with patch.object(
        announcements_module.mattermost_client, "create_post", new_callable=AsyncMock
    ) as mock_post:
        preview = await prepare_announcement_preview_tool.ainvoke(
            {
                "cohort_id": TEST_COHORT_ID,
                "raw_text": "[verify_bot_flow] scenario 4",
                "delivery_mode": "broadcast",
                "target_type": "role",
                "target_value": "learner",
            }
        )
        announcement_id = preview["audit_id"]

        # Switch identity to a different tech lead before confirming.
        current_requester.set(
            RequesterContext(
                mattermost_user_id="mm-user-techlead-2",
                channel_roles={TEST_COHORT_ID: "tech_lead"},
            )
        )
        result = await confirm_announcement_tool.ainvoke({"announcement_id": announcement_id})

        if result["dispatched"] or result["status"] != "not_authorized":
            _fail(f"expected not_authorized/no dispatch, got {result}")
        if mock_post.await_count != 0:
            _fail(f"expected 0 Mattermost calls, got {mock_post.await_count}")
        _ok("a different requester correctly could not confirm; 0 Mattermost calls")


async def scenario_cancel():
    print("\n--- Scenario 5: cancel -> zero posts ---")
    _set_tech_lead()
    with patch.object(
        announcements_module.mattermost_client, "create_post", new_callable=AsyncMock
    ) as mock_post:
        preview = await prepare_announcement_preview_tool.ainvoke(
            {
                "cohort_id": TEST_COHORT_ID,
                "raw_text": "[verify_bot_flow] scenario 5 - cancel",
                "delivery_mode": "broadcast",
                "target_type": "role",
                "target_value": "learner",
            }
        )
        result = await cancel_announcement_tool.ainvoke({"announcement_id": preview["audit_id"]})

        if not result["cancelled"]:
            _fail(f"expected cancelled=True, got {result}")
        if mock_post.await_count != 0:
            _fail(f"expected 0 Mattermost calls, got {mock_post.await_count}")
        _ok("cancelled; 0 Mattermost calls")


async def scenario_rate_limit():
    print("\n--- Scenario 6: rate limit after repeated sends ---")
    _set_tech_lead()
    with patch.object(
        announcements_module.mattermost_client, "create_post", new_callable=AsyncMock
    ) as mock_post:
        mock_post.return_value = {"id": "mm_post_rate_limit"}

        results = []
        for i in range(announcements_module.RATE_LIMIT_MAX_REQUESTS + 1):
            preview = await prepare_announcement_preview_tool.ainvoke(
                {
                    "cohort_id": TEST_COHORT_ID,
                    "raw_text": f"[verify_bot_flow] scenario 6 - #{i}",
                    "delivery_mode": "broadcast",
                    "target_type": "role",
                    "target_value": "learner",
                }
            )
            results.append(await confirm_announcement_tool.ainvoke({"announcement_id": preview["audit_id"]}))

        rate_limited = [r for r in results if r["status"] == "rate_limited"]
        if not rate_limited:
            _fail(f"expected at least one rate_limited result among {results}")
        _ok(f"{len(rate_limited)} of {len(results)} attempts correctly rate-limited")
        print(
            "  NOTE: this scenario assumes no other SENT announcements exist for "
            "this cohort within RATE_LIMIT_WINDOW_MINUTES — re-run against a "
            "clean window if it fails unexpectedly."
        )


async def main():
    print("=" * 60)
    print("ANNOUNCEMENTS — BOT-LEVEL FLOW VERIFICATION (real DB, fake Mattermost)")
    print("=" * 60)

    await scenario_authorized_full_conversation()
    await scenario_learner_cannot_announce()
    await scenario_replay_confirm()
    await scenario_wrong_requester_cannot_confirm()
    await scenario_cancel()
    await scenario_rate_limit()

    print("\n" + "=" * 60)
    print("ALL SCENARIOS PASSED")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
