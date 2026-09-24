import pytest
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from app.models.announcement import Announcement
from app.services.announcements import confirm_and_dispatch_announcement

CONFIRMING_USER_ID = 9


def _announcement(announcement_id: int = 1) -> MagicMock:
    """A pending announcement row owned by CONFIRMING_USER_ID.

    The confirm contract resolves the row after the atomic claim, so the mock
    must carry the identity and delivery fields the service reads.
    """
    row = MagicMock(spec=Announcement)
    row.id = announcement_id
    row.requester_id = CONFIRMING_USER_ID
    row.resolved_channel_id = "chan-confirm"
    row.exact_text = "Hello team!"
    return row


@pytest.fixture
def anyio_backend():
    return "asyncio"


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
    session.get = AsyncMock(return_value=_announcement())

    with patch("app.services.announcements.send_to_mattermost", new_callable=AsyncMock) as mock_send:
        mock_send.return_value = "mm_post_replay_1"

        # First call: Should succeed (rowcount = 1)
        res1 = await confirm_and_dispatch_announcement(
            session, announcement_id=1, confirming_user_id=CONFIRMING_USER_ID
        )
        assert res1["dispatched"] is True
        assert res1["status"] == "success"
        mock_send.assert_awaited_once()

    # Replay without the dispatcher patched: the claim must already be taken,
    # so nothing is dispatched a second time regardless of delivery outcome.
    res2 = await confirm_and_dispatch_announcement(session, announcement_id=1, confirming_user_id=CONFIRMING_USER_ID)
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
    session.get = AsyncMock(return_value=_announcement())

    with patch("app.services.announcements.send_to_mattermost", new_callable=AsyncMock) as mock_send:
        mock_send.return_value = "mm_post_race_1"

        res1, res2 = await asyncio.gather(
            confirm_and_dispatch_announcement(session, announcement_id=1, confirming_user_id=CONFIRMING_USER_ID),
            confirm_and_dispatch_announcement(session, announcement_id=1, confirming_user_id=CONFIRMING_USER_ID),
        )
        dispatched_results = [res1["dispatched"], res2["dispatched"]]
        assert dispatched_results.count(True) == 1
        assert dispatched_results.count(False) == 1
        # Exactly one dispatch happened: the atomic claim is the guard.
        assert mock_send.await_count == 1
