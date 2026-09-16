"""Smoke tests for the September 15-16 wiring changes."""

import os
from unittest.mock import AsyncMock

import pytest

os.environ.setdefault("JWT_SECRET_KEY", "test-secret-key-with-at-least-32-characters")

from app.core.langgraph.tools import back_office as back_office_tools


@pytest.mark.asyncio
async def test_announcement_tools_are_registered_and_delegate(monkeypatch: pytest.MonkeyPatch) -> None:
    tool_names = {tool.name for tool in back_office_tools.TOOLS}
    assert {
        "prepare_announcement_preview_tool",
        "confirm_announcement_tool",
        "cancel_announcement_tool",
    } <= tool_names

    resolve_channel = AsyncMock(return_value="mm-channel")
    resolve_audience = AsyncMock(return_value=[{"user_id": 7, "username": "learner"}])
    create_preview = AsyncMock(return_value={"audit_id": 12, "status": "pending"})
    confirm = AsyncMock(return_value={"status": "success", "dispatched": True})
    cancel = AsyncMock(return_value={"status": "cancelled", "cancelled": True})
    monkeypatch.setattr(back_office_tools, "resolve_announcement_channel", resolve_channel)
    monkeypatch.setattr(back_office_tools, "resolve_recipients_by_role", resolve_audience)
    monkeypatch.setattr(back_office_tools, "create_announcement_preview", create_preview)
    monkeypatch.setattr(back_office_tools, "confirm_and_dispatch_announcement", confirm)
    monkeypatch.setattr(back_office_tools, "cancel_announcement", cancel)

    preview = await back_office_tools.prepare_announcement_preview_tool.ainvoke(
        {
            "cohort_id": 3,
            "raw_text": "Standup moved to 10:00",
            "delivery_mode": "channel",
            "target_type": "role",
            "target_value": "learner",
            "created_by_user_id": 9,
        }
    )
    confirmed = await back_office_tools.confirm_announcement_tool.ainvoke(
        {"announcement_id": 12}
    )
    cancelled = await back_office_tools.cancel_announcement_tool.ainvoke(
        {"announcement_id": 13}
    )

    assert preview == {"audit_id": 12, "status": "pending"}
    assert confirmed["dispatched"] is True
    assert cancelled["cancelled"] is True
    resolve_channel.assert_awaited_once()
    resolve_audience.assert_awaited_once()
    create_preview.assert_awaited_once()
    confirm.assert_awaited_once_with(None, 12)
    cancel.assert_awaited_once_with(None, 13)