import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from app.core.config import settings
from app.services.google_meet import create_meet_event, _format_rfc3339
from app.models.ceremony import Ceremony
from app.core.langgraph.tools.ceremonies import schedule_ceremony
from app.core.langgraph.tools.results import ResultCode, tool_result
from app.services.ceremony_reminders import send_reminder
from app.services.ceremony_scheduling import ScheduleProposal


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def mock_google_settings(monkeypatch):
    monkeypatch.setattr(settings, "GOOGLE_MEET_ENABLED", True)
    monkeypatch.setattr(settings, "GOOGLE_SERVICE_ACCOUNT_CREDENTIALS", '{"type": "service_account"}')
    monkeypatch.setattr(settings, "GOOGLE_CALENDAR_ID", "test-calendar-id")


@pytest.fixture
def mock_google_api(monkeypatch):
    mock_build = MagicMock()
    mock_service = MagicMock()
    mock_build.return_value = mock_service
    
    mock_events = MagicMock()
    mock_service.events.return_value = mock_events
    
    mock_insert = MagicMock()
    mock_events.insert.return_value = mock_insert
    
    mock_execute = MagicMock()
    mock_insert.execute = mock_execute
    
    # Needs to be patched where it is used
    monkeypatch.setattr("app.services.google_meet.build", mock_build)
    monkeypatch.setattr("app.services.google_meet.service_account.Credentials.from_service_account_info", MagicMock())
    
    return mock_execute


@pytest.mark.anyio
async def test_create_meet_event_success(mock_google_settings, mock_google_api):
    mock_google_api.return_value = {
        "conferenceData": {
            "entryPoints": [
                {"entryPointType": "video", "uri": "https://meet.google.com/abc-defg-hij"}
            ]
        }
    }
    
    start = datetime(2026, 9, 8, 14, 0, tzinfo=timezone.utc)
    link = await create_meet_event(
        title="Sprint Planning",
        start=start,
        duration_minutes=60,
        description="Agenda text",
        organizer_email="alice@example.com"
    )
    
    assert link == "https://meet.google.com/abc-defg-hij"


@pytest.mark.anyio
async def test_create_meet_event_api_failure_returns_none(mock_google_settings, mock_google_api):
    # Simulate an error during execute
    mock_google_api.side_effect = Exception("API Error")
    
    start = datetime(2026, 9, 8, 14, 0, tzinfo=timezone.utc)
    link = await create_meet_event(
        title="Sprint Planning",
        start=start,
        duration_minutes=60
    )
    
    assert link is None


@pytest.mark.anyio
async def test_meet_disabled_via_settings(monkeypatch, mock_google_api):
    monkeypatch.setattr(settings, "GOOGLE_MEET_ENABLED", False)
    
    start = datetime(2026, 9, 8, 14, 0, tzinfo=timezone.utc)
    link = await create_meet_event(
        title="Sprint Planning",
        start=start,
        duration_minutes=60
    )
    
    assert link is None
    mock_google_api.assert_not_called()


@pytest.mark.anyio
@patch("app.core.langgraph.tools.ceremonies.scheduling.prepare_schedule")
@patch("app.core.langgraph.tools.ceremonies._confirm")
@patch("app.core.langgraph.tools.ceremonies.scheduling.commit_schedule")
async def test_ceremony_scheduled_result_includes_meet_link(
    mock_commit, mock_confirm, mock_prepare
):
    # Setup mocks
    start = datetime(2026, 9, 8, 14, 0, tzinfo=timezone.utc)
    mock_prepare.return_value = ScheduleProposal(
        team_id="team1",
        channel_id="chan1",
        ceremony_type_id=1,
        ceremony_type_key="planning",
        ceremony_type_label="Sprint Planning",
        organizer_id=1,
        scheduled_at=start,
        duration_minutes=60,
        agenda=None,
        time_expression="tomorrow",
        zone="UTC",
        local_display="tomorrow",
        utc_display="tomorrow",
        with_meet=True
    )
    mock_confirm.return_value = True
    
    mock_ceremony = Ceremony(
        id=42,
        team_id="team1",
        channel_id="chan1",
        ceremony_type_id=1,
        organizer_id=1,
        scheduled_at=start,
        duration_minutes=60,
        meet_link="https://meet.google.com/xyz"
    )
    mock_commit.return_value = mock_ceremony
    
    result = await schedule_ceremony.ainvoke(
        {"ceremony_type": "planning", "time_expression": "tomorrow", "with_meet": True}
    )
    
    assert "https://meet.google.com/xyz" in result
    assert "🔗 Join:" in result


@pytest.mark.anyio
@patch("app.services.ceremony_reminders._already_sent")
@patch("app.services.ceremony_reminders.mattermost_client")
@patch("app.services.ceremony_reminders._organizer_display")
@patch("app.services.ceremony_reminders._record_sent")
async def test_reminder_message_includes_meet_link(
    mock_record_sent, mock_org, mock_mm, mock_already
):
    mock_already.return_value = False
    mock_mm.get_user_timezone = AsyncMock(return_value="UTC")
    mock_org.return_value = "Alice"
    mock_mm.create_direct_channel = AsyncMock(return_value={"id": "dm1"})
    mock_mm.create_post = AsyncMock(return_value={"id": "post1"})
    
    # Create ceremony with meet_link
    start = datetime(2026, 9, 8, 14, 0, tzinfo=timezone.utc)
    ceremony = Ceremony(
        id=42,
        team_id="team1",
        channel_id="chan1",
        ceremony_type_id=1,
        organizer_id=1,
        scheduled_at=start,
        duration_minutes=60,
        meet_link="https://meet.google.com/abc"
    )
    
    success = await send_reminder(ceremony, "user1", "1h", "Sprint Planning")
    assert success is True
    
    # Verify the message sent to mattermost contains the link
    call_args = mock_mm.create_post.call_args[0]
    message = call_args[1]
    assert "https://meet.google.com/abc" in message
    assert "🔗 Join:" in message
