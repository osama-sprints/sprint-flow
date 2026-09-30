"""Regression tests for standup interception at the Mattermost WebSocket boundary."""

import asyncio
import json
from unittest.mock import AsyncMock

from app.services import escalation_closure, knowledge_review, mattermost_ws, standups


def posted_event(message: str, *, post_id: str = "post-1") -> dict[str, object]:
    return {
        "channel_type": "D",
        "team_id": "team-1",
        "sender_name": "learner",
        "post": json.dumps(
            {
                "id": post_id,
                "user_id": "mm-learner",
                "channel_id": "dm-1",
                "message": message,
            }
        ),
    }


def test_accepted_standup_reply_is_consumed_before_general_agent(monkeypatch):
    listener = mattermost_ws.MattermostWebSocketListener()
    accepted = standups.IngestResult(result=standups.ReplyResult.ACCEPTED)

    monkeypatch.setattr(mattermost_ws.settings, "STANDUP_ENABLED", True)
    monkeypatch.setattr(mattermost_ws, "is_own_post", AsyncMock(return_value=False))
    monkeypatch.setattr(knowledge_review, "handle_reviewer_reply", AsyncMock(return_value=False))
    monkeypatch.setattr(
        escalation_closure,
        "handle_reviewer_reply",
        AsyncMock(return_value=escalation_closure.ClosureResult(escalation_closure.ClosureOutcome.NOT_ESCALATION)),
    )
    ingest = AsyncMock(return_value=accepted)
    monkeypatch.setattr(standups, "ingest_standup_reply", ingest)
    monkeypatch.setattr(listener, "_should_handle", AsyncMock(side_effect=AssertionError("agent path reached")))

    asyncio.run(listener._handle_posted(posted_event("1. shipped\n2. review\n3. none")))

    ingest.assert_awaited_once()


def test_non_standup_dm_continues_to_general_agent(monkeypatch):
    listener = mattermost_ws.MattermostWebSocketListener()
    answer = AsyncMock()

    monkeypatch.setattr(mattermost_ws.settings, "STANDUP_ENABLED", True)
    monkeypatch.setattr(mattermost_ws, "is_own_post", AsyncMock(return_value=False))
    monkeypatch.setattr(knowledge_review, "handle_reviewer_reply", AsyncMock(return_value=False))
    monkeypatch.setattr(
        escalation_closure,
        "handle_reviewer_reply",
        AsyncMock(return_value=escalation_closure.ClosureResult(escalation_closure.ClosureOutcome.NOT_ESCALATION)),
    )
    monkeypatch.setattr(
        standups,
        "ingest_standup_reply",
        AsyncMock(return_value=standups.IngestResult(result=standups.ReplyResult.NOT_A_STANDUP)),
    )
    monkeypatch.setattr(listener, "_should_handle", AsyncMock(return_value=True))
    monkeypatch.setattr(mattermost_ws, "claim_mattermost_event", AsyncMock(return_value=True))
    monkeypatch.setattr(mattermost_ws, "get_thread_history", AsyncMock(return_value=[]))
    monkeypatch.setattr(mattermost_ws, "answer_and_reply", answer)

    async def run() -> None:
        await listener._handle_posted(posted_event("@assistant what is next?", post_id="post-2"))
        await asyncio.sleep(0)

    asyncio.run(run())

    answer.assert_awaited_once()
