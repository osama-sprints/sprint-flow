"""Verification suite for cross-channel ceremony authorisation capabilities."""

from datetime import datetime, timedelta, timezone
import pytest
from unittest.mock import MagicMock, patch

from app.core.langgraph.tools.ceremony_scheduler import (
    schedule_ceremony,
    amend_ceremony,
    read_ceremonies,
)
from app.models.ceremony import Ceremony
from app.models.ceremony_type import CeremonyType

@pytest.fixture
def mock_db_session():
    session = MagicMock()
    mock_type = MagicMock()
    mock_type.id = "type_1"
    mock_type.name = "standup"
    session.query.return_value.filter_by.return_value.first.return_value = mock_type
    return session


# 1. SCHEDULE CEREMONY INHERENT SECURITY
@patch("app.core.langgraph.tools.ceremony_scheduler._get_requester")
@patch("app.core.langgraph.tools.ceremony_scheduler.ask_human")
@patch("app.core.langgraph.tools.ceremony_scheduler.Session")
@patch("app.core.langgraph.tools.ceremony_scheduler.admin_service")
@patch("app.core.langgraph.tools.ceremony_scheduler.find_conflict")
def test_schedule_ceremony_inherently_uses_requester_channel(
    mock_find, mock_admin, mock_session_cls, mock_ask_human, mock_get_requester, mock_db_session
):
    """Test that a user cannot specify a different channel to schedule a ceremony."""
    mock_get_requester.return_value = {"channel_id": "chan_frontend", "team_id": "team_1"}
    mock_session_cls.return_value.__enter__.return_value = mock_db_session
    mock_admin.evaluate_permission.return_value = None
    mock_find.return_value = None
    mock_ask_human.invoke.return_value = "yes"

    res = schedule_ceremony.invoke({
        "ceremony_type": "standup",
        "raw_time": "tomorrow at 10 AM UTC",
        "organizer_id": "user1",
    })

    assert "SUCCESS" in res
    mock_db_session.add.assert_called_once()
    
    # Check that the channel_id used was the requester's channel
    added_ceremony = mock_db_session.add.call_args[0][0]
    assert added_ceremony.channel_id == "chan_frontend"
    assert added_ceremony.team_id == "team_1"


# 2. AMEND CEREMONY CROSS-CHANNEL REFUSAL
@patch("app.core.langgraph.tools.ceremony_scheduler._get_requester")
@patch("app.core.langgraph.tools.ceremony_scheduler.Session")
def test_amend_ceremony_rejects_cross_channel(
    mock_session_cls, mock_get_requester, mock_db_session
):
    """A user in chan_frontend cannot amend a ceremony belonging to chan_backend."""
    mock_get_requester.return_value = {"channel_id": "chan_frontend"}
    mock_session_cls.return_value.__enter__.return_value = mock_db_session

    # Ceremony exists but belongs to a different channel
    target_ceremony = MagicMock()
    target_ceremony.id = 42
    target_ceremony.organizer = "admin_user"
    target_ceremony.status = "scheduled"
    target_ceremony.channel_id = "chan_backend"
    target_ceremony.scheduled_at = datetime.now(timezone.utc) + timedelta(days=1)
    
    mock_db_session.get.return_value = target_ceremony

    res = amend_ceremony.invoke({
        "ceremony_id": 42,
        "organizer_id": "admin_user", # Even if they are the original organizer!
        "cancel": True,
    })

    assert "Error: You can only amend ceremonies scheduled in the current channel" in res
    mock_db_session.commit.assert_not_called()


# 3. READ CEREMONIES CROSS-CHANNEL ISOLATION
@patch("app.core.langgraph.tools.ceremony_scheduler._get_requester")
@patch("app.core.langgraph.tools.ceremony_scheduler.Session")
@patch("app.core.langgraph.tools.ceremony_scheduler._ceremony_type_name")
def test_read_ceremonies_isolates_by_channel(
    mock_type_name, mock_session_cls, mock_get_requester, mock_db_session
):
    """A user in chan_frontend only reads ceremonies for chan_frontend."""
    mock_get_requester.return_value = {"channel_id": "chan_frontend"}
    mock_session_cls.return_value.__enter__.return_value = mock_db_session
    mock_type_name.return_value = "standup"

    # Verify query works for an empty result (to just inspect the generated where clause if we wanted to)
    mock_db_session.exec.return_value.all.return_value = []
    res_empty = read_ceremonies.invoke({})
    
    assert "No upcoming ceremonies found in this channel" in res_empty
    
    # We can inspect the actual query passed to mock_db_session.exec
    call_args = mock_db_session.exec.call_args[0][0]
    query_str = str(call_args)
    
    assert "ceremony.channel_id = :channel_id" in query_str or "ceremony.channel_id = :channel_id_1" in query_str
