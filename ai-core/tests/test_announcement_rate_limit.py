import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock
from app.models.announcement import Announcement
from app.models.enums import AnnouncementOutcome
from app.services.announcements import confirm_and_dispatch_announcement


@pytest.fixture
def anyio_backend():
    return 'asyncio'


@pytest.mark.anyio
async def test_announcement_rate_limiting_workflow():
    session = AsyncMock()

    # Shared DB state simulation
    count_holder = {"count": 0}

    # Mock fetching Announcement
    mock_announcement = MagicMock(spec=Announcement)
    mock_announcement.cohort_id = 100
    session.get = AsyncMock(return_value=mock_announcement)

    async def fake_exec(stmt):
        mock_res = MagicMock()
        stmt_str = str(stmt)

        # Handle UPDATE ... WHERE confirmation_status='pending'
        if "UPDATE" in stmt_str and "confirmation_status" in stmt_str:
            mock_res.rowcount = 1
        # Handle SELECT COUNT(...)
        elif "count" in stmt_str.lower() or "SELECT" in stmt_str:
            mock_res.one.return_value = count_holder["count"]
        else:
            mock_res.rowcount = 1

        return mock_res

    session.exec = AsyncMock(side_effect=fake_exec)
    session.commit = AsyncMock()

    now_time = datetime.now(timezone.utc)

    # 1. Send within limit (Count = 2, Limit = 3) -> SUCCESS
    count_holder["count"] = 2
    res_ok = await confirm_and_dispatch_announcement(session, announcement_id=101, now=now_time)
    assert res_ok["dispatched"] is True
    assert res_ok["status"] == "success"

    # 2. Exceed limit (Count = 3, Limit = 3) -> RATE_LIMITED
    count_holder["count"] = 3
    res_limited = await confirm_and_dispatch_announcement(session, announcement_id=102, now=now_time)
    assert res_limited["dispatched"] is False
    assert res_limited["status"] == "rate_limited"
    assert res_limited["outcome"] == AnnouncementOutcome.RATE_LIMITED

    # 3. Window Passes (Window shifts forward 61 minutes, Count resets to 0) -> SUCCESS
    future_time = now_time + timedelta(minutes=61)
    count_holder["count"] = 0
    res_after_window = await confirm_and_dispatch_announcement(session, announcement_id=103, now=future_time)
    assert res_after_window["dispatched"] is True
    assert res_after_window["status"] == "success"