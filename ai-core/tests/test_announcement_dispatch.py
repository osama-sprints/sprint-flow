import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from app.models.announcement import Announcement
from app.models.enums import AnnouncementOutcome
from app.services.announcements import (
    confirm_and_dispatch_announcement,
    cancel_announcement
)


@pytest.fixture
def anyio_backend():
    return 'asyncio'


@pytest.mark.anyio
async def test_dispatch_success_stores_post_id():
    session = AsyncMock()

    mock_announcement = MagicMock(spec=Announcement)
    mock_announcement.cohort_id = 1
    mock_announcement.resolved_channel_id = "valid_channel"
    mock_announcement.exact_text = "Hello team!"
    session.get = AsyncMock(return_value=mock_announcement)

    async def fake_exec(stmt):
        mock_res = MagicMock()
        mock_res.rowcount = 1
        mock_res.one.return_value = 0
        return mock_res

    session.exec = AsyncMock(side_effect=fake_exec)
    session.commit = AsyncMock()

    with patch("app.services.announcements.send_to_mattermost", new_callable=AsyncMock) as mock_send:
        mock_send.return_value = "mm_post_12345"

        res = await confirm_and_dispatch_announcement(session, announcement_id=10)

        assert res["dispatched"] is True
        assert res["outcome"] == AnnouncementOutcome.SENT
        assert res["mattermost_post_id"] == "mm_post_12345"
        mock_send.assert_called_once_with(channel_id="valid_channel", message="Hello team!")


@pytest.mark.anyio
async def test_dispatch_failure_logs_failed_outcome():
    session = AsyncMock()

    mock_announcement = MagicMock(spec=Announcement)
    mock_announcement.cohort_id = 1
    mock_announcement.resolved_channel_id = "invalid_channel"
    mock_announcement.exact_text = "Hello team!"
    session.get = AsyncMock(return_value=mock_announcement)

    async def fake_exec(stmt):
        mock_res = MagicMock()
        mock_res.rowcount = 1
        mock_res.one.return_value = 0
        return mock_res

    session.exec = AsyncMock(side_effect=fake_exec)
    session.commit = AsyncMock()

    with patch("app.services.announcements.send_to_mattermost", side_effect=ValueError("Channel not found")):
        res = await confirm_and_dispatch_announcement(session, announcement_id=11)

        assert res["dispatched"] is False
        assert res["status"] == "failed"
        assert res["outcome"] == AnnouncementOutcome.FAILED
        assert "Channel not found" in res["error_detail"]


@pytest.mark.anyio
async def test_cancellation_path_never_calls_mattermost():
    session = AsyncMock()

    async def fake_exec(stmt):
        mock_res = MagicMock()
        mock_res.rowcount = 1
        return mock_res

    session.exec = AsyncMock(side_effect=fake_exec)
    session.commit = AsyncMock()

    with patch("app.services.announcements.send_to_mattermost", new_callable=AsyncMock) as mock_send:
        res = await cancel_announcement(session, announcement_id=12)

        assert res["cancelled"] is True
        assert res["outcome"] == AnnouncementOutcome.CANCELLED
        mock_send.assert_not_called()