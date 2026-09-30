"""Capability 15 — Conversational standup summaries: the tool boundary.

The service logic (grounding, member matching, sprint window, channel
authorisation) is covered in ``test_standup_summary.py`` and, against real
PostgreSQL, in ``tests/integration/test_standups_db.py``. This file proves the
TOOL contract the LLM sees:

- the requester's own channel is the only scope the tool can ever read;
- no channel context, no summary;
- a malformed date is a validation error, not a crash;
- the JSON payload reflects stored facts exactly (no invention possible).
"""

import asyncio
import json
from datetime import date
from unittest.mock import AsyncMock, patch

import pytest

from app.core.langgraph.tools.standups import summarize_standups
from app.core.requester import RequesterContext, current_requester
from app.services.domain.standups import StandupSummary


def _summary(**overrides) -> StandupSummary:
    defaults = dict(
        submitted_updates=(
            {
                "learner_id": 1,
                "what_i_did": "Finished the API work",
                "what_i_will_do": "Add tests",
                "blockers": None,
            },
        ),
        blockers=(),
        missing_members=({"user_id": 2, "username": "bob", "display_name": "Bob"},),
        sprint_info={"id": 7, "name": "Sprint 1", "start_date": "2026-09-14", "end_date": "2026-09-25"},
        message="Daily standup summary for 2026-09-15.",
    )
    defaults.update(overrides)
    return StandupSummary(**defaults)


def run_tool(target_date: str):
    return asyncio.run(summarize_standups.ainvoke({"target_date": target_date}))


@pytest.fixture
def bound_channel():
    token = current_requester.set(RequesterContext(mattermost_user_id="mm-admin", channel_id="chan-summary"))
    yield "chan-summary"
    current_requester.reset(token)


def test_tool_returns_the_stored_facts_as_json(bound_channel):
    summary = _summary()
    with patch(
        "app.core.langgraph.tools.standups.get_standup_summary_for_channel", AsyncMock(return_value=summary)
    ) as read:
        result = run_tool("2026-09-15")

    read.assert_awaited_once_with("chan-summary", date(2026, 9, 15))
    payload = json.loads(result)
    assert payload["submitted_updates"] == [summary.submitted_updates[0]]
    assert payload["missing_members"] == [summary.missing_members[0]]
    assert payload["sprint_info"]["name"] == "Sprint 1"
    assert payload["blockers"] == []


def test_tool_cannot_read_any_channel_but_the_requesters(bound_channel):
    """The tool takes no channel argument: the scope is fixed by the ContextVar."""
    captured: dict = {}

    async def read(channel_id, target_date):
        captured["channel_id"] = channel_id
        return _summary()

    with patch("app.core.langgraph.tools.standups.get_standup_summary_for_channel", AsyncMock(side_effect=read)):
        run_tool("2026-09-15")

    assert captured["channel_id"] == "chan-summary"


def test_tool_without_channel_context_is_a_validation_error():
    """guarded_tool turns the service's ValidationFailed into the stable
    [VALIDATION_ERROR] result the agent relays — the tool never raises."""
    current_requester.set(None)
    try:
        result = run_tool("2026-09-15")
    finally:
        current_requester.set(None)
    assert result == "[VALIDATION_ERROR] Standup summaries require a channel context."


def test_tool_rejects_a_malformed_date_before_any_read(bound_channel):
    with patch("app.core.langgraph.tools.standups.get_standup_summary_for_channel", AsyncMock()) as read:
        result = run_tool("15/09/2026")
    assert result.startswith("[VALIDATION_ERROR]") and "YYYY-MM-DD" in result
    read.assert_not_awaited()


def test_tool_no_data_scenario_is_represented_faithfully(bound_channel):
    summary = _summary(
        submitted_updates=(),
        missing_members=(
            {"user_id": 1, "username": "alice", "display_name": "Alice"},
            {"user_id": 2, "username": "bob", "display_name": "Bob"},
        ),
        message="No daily standup updates were submitted for 2026-09-15 in this channel.",
    )
    with patch("app.core.langgraph.tools.standups.get_standup_summary_for_channel", AsyncMock(return_value=summary)):
        result = run_tool("2026-09-15")

    payload = json.loads(result)
    assert payload["submitted_updates"] == []
    assert len(payload["missing_members"]) == 2
    assert "No daily standup updates" in payload["message"]
