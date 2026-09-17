"""Regression tests for the four confirmed bugs fixed in announcements.py.

Each test checks the ACTUAL call signature used against the real function
being called, not just that a mock was invoked — this is specifically to
catch the recurring failure pattern in this codebase: code that calls a real
function with the wrong argument order/shape, which a loosely-configured
mock silently accepts.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.announcements import (
    create_announcement_preview,
    resolve_announcement_channel,
    resolve_recipients_by_role,
    resolve_recipients_by_usernames,
)
from app.services.authorisation import ValidationFailed


@pytest.fixture
def anyio_backend():
    return "asyncio"


# --------------------------------------------------------------------------
# Fix 1: channel must be resolved from the stored cohort, never accepted
# directly from the caller (the "arbitrary channel override" the brief
# prohibits).
# --------------------------------------------------------------------------

@pytest.mark.anyio
async def test_channel_is_resolved_from_stored_sprint_not_caller_input(mocker):
    fake_sprint = MagicMock(channel_id="chan-from-db", status="active")
    mocker.patch(
        "app.services.announcements.sprint_repo.get_sprint",
        new_callable=AsyncMock,
        return_value=fake_sprint,
    )
    mock_authority = mocker.patch(
        "app.services.announcements.require_channel_authority", new_callable=AsyncMock
    )

    requester = MagicMock()
    result = await resolve_announcement_channel(AsyncMock(), requester, cohort_id=42)

    # The function signature itself no longer accepts a channel_id at all —
    # this call can only have gotten "chan-from-db" from the stored sprint.
    assert result == "chan-from-db"
    mock_authority.assert_awaited_once_with(requester, "chan-from-db", action="send_announcement")


@pytest.mark.anyio
async def test_missing_cohort_raises_validation_failed(mocker):
    mocker.patch(
        "app.services.announcements.sprint_repo.get_sprint",
        new_callable=AsyncMock,
        return_value=None,
    )
    with pytest.raises(ValidationFailed):
        await resolve_announcement_channel(AsyncMock(), MagicMock(), cohort_id=999)


@pytest.mark.anyio
async def test_inactive_cohort_raises_validation_failed(mocker):
    fake_sprint = MagicMock(channel_id="chan-1", status="completed")
    mocker.patch(
        "app.services.announcements.sprint_repo.get_sprint",
        new_callable=AsyncMock,
        return_value=fake_sprint,
    )
    with pytest.raises(ValidationFailed):
        await resolve_announcement_channel(AsyncMock(), MagicMock(), cohort_id=1)


# --------------------------------------------------------------------------
# Fix 2: cohort_id must reach the Announcement row (the model field is
# required — omitting it raised a ValidationError on every call before).
# --------------------------------------------------------------------------

@pytest.mark.anyio
async def test_preview_writes_cohort_id_onto_the_announcement_row(mocker):
    captured = {}

    def fake_add(obj):
        captured["announcement"] = obj

    session = AsyncMock()
    session.add = MagicMock(side_effect=fake_add)

    async def fake_refresh(obj):
        obj.id = 1

    session.refresh = AsyncMock(side_effect=fake_refresh)

    result = await create_announcement_preview(
        session=session,
        cohort_id=55,
        raw_text="Hello cohort",
        delivery_mode="broadcast",
        resolved_channel="chan-1",
        resolved_audience=[],
        created_by_user_id=1,
    )

    assert captured["announcement"].cohort_id == 55
    assert result["preview"]["cohort_id"] == 55


# --------------------------------------------------------------------------
# Fix 3: resolve_recipients_by_role must call list_channel_roles with the
# real signature: channel_id positional, session as a keyword.
# --------------------------------------------------------------------------

@pytest.mark.anyio
async def test_resolve_by_role_calls_list_channel_roles_with_real_signature(mocker):
    fake_member = MagicMock()
    fake_member.user = MagicMock(id=1, username="alice")
    fake_member.role = MagicMock(key="tech_lead")

    mock_list = mocker.patch(
        "app.services.announcements.channel_repo.list_channel_roles",
        new_callable=AsyncMock,
        return_value=[fake_member],
    )

    session = AsyncMock()
    result = await resolve_recipients_by_role(session, channel_id="chan-9", role="tech_lead")

    # The bug: this used to be called as (session, channel_id=channel_id) —
    # session must never land in the channel_id slot.
    mock_list.assert_awaited_once_with("chan-9", session=session)
    assert result == [{"user_id": 1, "username": "alice", "role": "tech_lead", "channel_id": "chan-9"}]


@pytest.mark.anyio
async def test_resolve_by_role_filters_out_non_matching_roles(mocker):
    member_a = MagicMock()
    member_a.user = MagicMock(id=1, username="alice")
    member_a.role = MagicMock(key="tech_lead")
    member_b = MagicMock()
    member_b.user = MagicMock(id=2, username="bob")
    member_b.role = MagicMock(key="learner")

    mocker.patch(
        "app.services.announcements.channel_repo.list_channel_roles",
        new_callable=AsyncMock,
        return_value=[member_a, member_b],
    )

    result = await resolve_recipients_by_role(AsyncMock(), channel_id="chan-9", role="learner")
    assert [r["username"] for r in result] == ["bob"]


# --------------------------------------------------------------------------
# Fix 4: resolve_recipients_by_usernames must call
# get_role_for_user_in_channel with the real signature: user_id, channel_id
# positional, session as a keyword.
# --------------------------------------------------------------------------

@pytest.mark.anyio
async def test_resolve_by_username_calls_get_role_with_real_signature(mocker):
    fake_user = MagicMock(id=7, username="carol")
    mocker.patch(
        "app.services.announcements.identity_repo.get_user_by_username",
        new_callable=AsyncMock,
        return_value=fake_user,
    )
    fake_role = MagicMock(key="ops_support")
    mock_get_role = mocker.patch(
        "app.services.announcements.channel_repo.get_role_for_user_in_channel",
        new_callable=AsyncMock,
        return_value=fake_role,
    )

    session = AsyncMock()
    result = await resolve_recipients_by_usernames(session, channel_id="chan-2", usernames=["carol"])

    # The bug: this used to be called as (session, user.id, channel_id) —
    # three positional args against a two-positional-param function.
    mock_get_role.assert_awaited_once_with(7, "chan-2", session=session)
    assert result == [{"user_id": 7, "username": "carol", "role": "ops_support", "channel_id": "chan-2"}]


@pytest.mark.anyio
async def test_resolve_by_username_rejects_non_member(mocker):
    mocker.patch(
        "app.services.announcements.identity_repo.get_user_by_username",
        new_callable=AsyncMock,
        return_value=MagicMock(id=8, username="dave"),
    )
    mocker.patch(
        "app.services.announcements.channel_repo.get_role_for_user_in_channel",
        new_callable=AsyncMock,
        return_value=None,
    )

    with pytest.raises(ValidationFailed):
        await resolve_recipients_by_usernames(AsyncMock(), channel_id="chan-2", usernames=["dave"])


@pytest.mark.anyio
async def test_resolve_by_username_rejects_nonexistent_user(mocker):
    mocker.patch(
        "app.services.announcements.identity_repo.get_user_by_username",
        new_callable=AsyncMock,
        return_value=None,
    )

    with pytest.raises(ValidationFailed):
        await resolve_recipients_by_usernames(AsyncMock(), channel_id="chan-2", usernames=["ghost"])