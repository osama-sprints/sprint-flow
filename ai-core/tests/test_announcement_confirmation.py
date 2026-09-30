import pytest
import asyncio
from unittest.mock import AsyncMock, MagicMock
from app.services.announcements import confirm_and_dispatch_announcement


@pytest.fixture
def anyio_backend():
    return 'asyncio'


@pytest.mark.anyio
async def test_confirm_replay_protection():
    session = AsyncMock()
    db_state = {"status": "pending"}

    async def fake_exec(stmt):
        mock_res = MagicMock()
        if db_state["status"] == "pending":
            db_state["status"] = "sending"
            mock_res.rowcount = 1
        else:
            mock_res.rowcount = 0
        return mock_res

    session.exec = AsyncMock(side_effect=fake_exec)
    session.commit = AsyncMock()

    # First call: Should succeed (rowcount = 1)
    res1 = await confirm_and_dispatch_announcement(session, announcement_id=1)
    assert res1["dispatched"] is True
    assert res1["status"] == "success"

    res2 = await confirm_and_dispatch_announcement(session, announcement_id=1)
    assert res2["dispatched"] is False
    assert res2["status"] == "already_processed"


@pytest.mark.anyio
async def test_confirm_concurrent_race_condition():
    session = AsyncMock()

    db_state = {"status": "pending"}
    lock = asyncio.Lock()

    async def fake_exec_atomic(stmt):
        async with lock:
            mock_res = MagicMock()
            if db_state["status"] == "pending":
                db_state["status"] = "sending"
                mock_res.rowcount = 1
            else:
                mock_res.rowcount = 0
            return mock_res

    session.exec = AsyncMock(side_effect=fake_exec_atomic)
    session.commit = AsyncMock()

    res1, res2 = await asyncio.gather(
        confirm_and_dispatch_announcement(session, announcement_id=1),
        confirm_and_dispatch_announcement(session, announcement_id=1)
    )
    dispatched_results = [res1["dispatched"], res2["dispatched"]]
    assert dispatched_results.count(True) == 1
    assert dispatched_results.count(False) == 1