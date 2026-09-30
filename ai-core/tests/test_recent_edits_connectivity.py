"""Smoke tests for the September 15-16 wiring changes."""

import os
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.core.requester import RequesterContext, current_requester

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

    request_announcement_mock = AsyncMock(return_value={"audit_id": 12, "status": "pending"})
    confirm = AsyncMock(return_value={"status": "success", "dispatched": True})
    cancel = AsyncMock(return_value={"status": "cancelled", "cancelled": True})
    # prepare_announcement_preview_tool delegates to request_announcement as the
    # single entry point (channel resolution, audience resolution, preview and
    # the refusal audit row all live inside it).
    monkeypatch.setattr(back_office_tools, "request_announcement", request_announcement_mock)
    monkeypatch.setattr(back_office_tools, "confirm_and_dispatch_announcement", confirm)
    monkeypatch.setattr(back_office_tools, "cancel_announcement", cancel)
    internal_session = object()

    @asynccontextmanager
    async def fake_session_scope():
        yield internal_session

    monkeypatch.setattr(back_office_tools, "session_scope", fake_session_scope)

    stored_user = SimpleNamespace(id=9)

    with patch("app.core.langgraph.tools.back_office.require_requester_user", new_callable=AsyncMock) as mock_require:
        # The tools read the requester from the real ContextVar — set it here
        # (and reset after) rather than patching the singleton; prepare and
        # confirm each resolve it to a stored user through the mocked lookup.
        token = current_requester.set(RequesterContext(mattermost_user_id="mm-lead", username="lead"))
        mock_require.side_effect = [stored_user, stored_user]
        try:
            preview = await back_office_tools.prepare_announcement_preview_tool.ainvoke(
                {
                    "cohort_id": 3,
                    "raw_text": "Standup moved to 10:00",
                    "delivery_mode": "channel",
                    "target_type": "role",
                    "target_value": "learner",
                }
            )
            confirmed = await back_office_tools.confirm_announcement_tool.ainvoke({"announcement_id": 12})
            cancelled = await back_office_tools.cancel_announcement_tool.ainvoke({"announcement_id": 13})
        finally:
            current_requester.reset(token)

    assert preview == {"audit_id": 12, "status": "pending"}
    assert confirmed["dispatched"] is True
    assert cancelled["cancelled"] is True
    request_announcement_mock.assert_awaited_once()
    confirm.assert_awaited_once_with(internal_session, 12, confirming_user_id=9)
    cancel.assert_awaited_once_with(internal_session, 13)
