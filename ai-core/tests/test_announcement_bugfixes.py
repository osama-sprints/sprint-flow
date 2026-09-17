"""Regression tests for the confirmed bugs fixed in announcements.py (Channel refactored)."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.models import announcement as announcement_module
from app.services.announcements import (
    is_rate_limited,
    resolve_announcement_channel,
    resolve_recipients_by_usernames,
)
from app.services.authorisation import ValidationFailed


@pytest.fixture
def anyio_backend():
    return "asyncio"


# --------------------------------------------------------------------------
# Channel Resolution & Authority Verification
# --------------------------------------------------------------------------

@pytest.mark.anyio
async def test_resolve_channel_calls_require_channel_authority_with_real_signature(mocker):
    mock_authority = mocker.patch(
        "app.services.announcements.require_channel_authority", new_callable=AsyncMock
    )

    requester = MagicMock()
    session = AsyncMock()

    result = await resolve_announcement_channel(session, requester, channel_id="chan-42")

    mock_authority.assert_awaited_once_with(
        requester, "chan-42", action="send_announcement"
    )
    assert result == "chan-42"


@pytest.mark.anyio
async def test_resolve_channel_unauthorized_raises_validation_failed(mocker):
    mocker.patch(
        "app.services.announcements.require_channel_authority",
        new_callable=AsyncMock,
        side_effect=ValidationFailed("Unauthorized channel access"),
    )

    with pytest.raises(ValidationFailed):
        await resolve_announcement_channel(AsyncMock(), MagicMock(), channel_id="chan-999")


# --------------------------------------------------------------------------
# Rate Limiting Logic
# --------------------------------------------------------------------------

@pytest.mark.anyio
async def test_rate_limit_query_filters_by_sent_outcome_not_confirmation_status():
    captured = {}

    async def fake_exec(stmt):
        captured["stmt"] = str(stmt)
        mock_res = MagicMock()
        mock_res.one.return_value = 0
        return mock_res

    session = AsyncMock()
    session.exec = AsyncMock(side_effect=fake_exec)

    await is_rate_limited(session, channel_id="chan-1")

    stmt_str = captured["stmt"].lower()
    assert "outcome" in stmt_str
    assert "confirmation_status" not in stmt_str


@pytest.mark.anyio
async def test_rate_limit_true_when_sent_count_meets_max():
    session = AsyncMock()
    mock_res = MagicMock()
    mock_res.one.return_value = 3  # RATE_LIMIT_MAX_REQUESTS
    session.exec = AsyncMock(return_value=mock_res)

    assert await is_rate_limited(session, channel_id="chan-1") is True


@pytest.mark.anyio
async def test_rate_limit_false_when_sent_count_below_max():
    session = AsyncMock()
    mock_res = MagicMock()
    mock_res.one.return_value = 2
    session.exec = AsyncMock(return_value=mock_res)

    assert await is_rate_limited(session, channel_id="chan-1") is False


# --------------------------------------------------------------------------
# Dead-code cleanup
# --------------------------------------------------------------------------

def test_dead_stub_functions_removed_from_model_module():
    assert not hasattr(announcement_module, "resolve_audience_by_role")
    assert not hasattr(announcement_module, "resolve_audience_by_username")
    assert not hasattr(announcement_module, "check_rate_limit")


# --------------------------------------------------------------------------
# resolve_recipients_by_usernames: real role lookup via channel_repo
# --------------------------------------------------------------------------

@pytest.mark.anyio
async def test_username_path_uses_real_role_not_hardcoded_member(mocker):
    fake_user = MagicMock(id=5, username="alice")
    mocker.patch(
        "app.services.announcements.identity_repo.get_user_by_username",
        new_callable=AsyncMock,
        return_value=fake_user,
    )
    fake_role = MagicMock(__str__=lambda self: "tech_lead")
    mocker.patch(
        "app.services.announcements.channel_repo.get_role_for_user_in_channel",
        new_callable=AsyncMock,
        return_value=fake_role,
    )

    result = await resolve_recipients_by_usernames(AsyncMock(), channel_id="chan-1", usernames=["alice"])

    assert result == [{"user_id": 5, "username": "alice", "role": "tech_lead", "channel_id": "chan-1"}]


@pytest.mark.anyio
async def test_username_path_rejects_non_member(mocker):
    mocker.patch(
        "app.services.announcements.identity_repo.get_user_by_username",
        new_callable=AsyncMock,
        return_value=MagicMock(id=6, username="bob"),
    )
    mocker.patch(
        "app.services.announcements.channel_repo.get_role_for_user_in_channel",
        new_callable=AsyncMock,
        return_value=None,  # not a member
    )

    with pytest.raises(ValidationFailed):
        await resolve_recipients_by_usernames(AsyncMock(), channel_id="chan-1", usernames=["bob"])