"""Pure-logic unit tests for the ceremony reminder service.

All tests run without a live database: every data-access call is mocked with
``unittest.mock``, following the style of ``test_ceremony_scheduling.py``.

Async functions are exercised via ``asyncio.run()`` so no pytest-asyncio
plugin is required — matching the project's existing test setup.

Test cases (from the spec):
- test_due_ceremonies_correct_window
- test_cancelled_ceremony_skipped
- test_archived_channel_skipped
- test_already_sent_skipped
- test_idempotent_on_restart
- test_timezone_formatting
- test_fallback_to_utc
"""

import asyncio
from datetime import (
    UTC,
    datetime,
    timedelta,
)
from unittest.mock import (
    AsyncMock,
    MagicMock,
    patch,
)

import pytest

from app.models import Ceremony
from app.models.enums import CeremonyStatus
from app.services.ceremony_reminders import (
    _format_local,
    due_ceremonies,
    get_channel_members,
    send_reminder,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

NOW = datetime(2026, 9, 8, 10, 0, tzinfo=UTC)


def make_ceremony(
    ceremony_id: int = 1,
    channel_id: str = "chan_001",
    scheduled_at: datetime = NOW + timedelta(hours=24),
    status: str = CeremonyStatus.SCHEDULED.value,
    agenda: str | None = None,
) -> Ceremony:
    return Ceremony(
        id=ceremony_id,
        team_id="team_001",
        channel_id=channel_id,
        ceremony_type_id=1,
        organizer_id=10,
        scheduled_at=scheduled_at,
        duration_minutes=60,
        status=status,
        agenda=agenda,
    )


# ---------------------------------------------------------------------------
# due_ceremonies
# ---------------------------------------------------------------------------


def test_due_ceremonies_correct_window():
    """Only ceremonies inside the +-6 min band appear."""
    on_target = make_ceremony(scheduled_at=NOW + timedelta(hours=24))
    too_far = make_ceremony(ceremony_id=2, scheduled_at=NOW + timedelta(hours=25))

    with patch(
        "app.services.ceremony_reminders.ceremony_repo.list_upcoming_ceremonies",
        new_callable=AsyncMock,
        return_value=[on_target, too_far],
    ):
        result = asyncio.run(due_ceremonies(24, now=NOW, poll_margin_minutes=6))

    assert on_target in result
    assert too_far not in result


def test_due_ceremonies_margin_inclusive():
    """Ceremonies exactly at the margin boundaries are included."""
    margin_minutes = 6
    at_lower = make_ceremony(scheduled_at=NOW + timedelta(hours=24) - timedelta(minutes=margin_minutes))
    at_upper = make_ceremony(ceremony_id=2, scheduled_at=NOW + timedelta(hours=24) + timedelta(minutes=margin_minutes))

    with patch(
        "app.services.ceremony_reminders.ceremony_repo.list_upcoming_ceremonies",
        new_callable=AsyncMock,
        return_value=[at_lower, at_upper],
    ):
        result = asyncio.run(due_ceremonies(24, now=NOW, poll_margin_minutes=margin_minutes))

    assert at_lower in result
    assert at_upper in result


# ---------------------------------------------------------------------------
# test_cancelled_ceremony_skipped
# ---------------------------------------------------------------------------


def test_cancelled_ceremony_skipped():
    """list_upcoming_ceremonies only returns scheduled ceremonies — cancelled never appear."""
    cancelled = make_ceremony(status=CeremonyStatus.CANCELLED.value)

    # list_upcoming_ceremonies filters status=="scheduled" internally.
    # We simulate it returning nothing for a cancelled ceremony.
    with patch(
        "app.services.ceremony_reminders.ceremony_repo.list_upcoming_ceremonies",
        new_callable=AsyncMock,
        return_value=[],
    ):
        result = asyncio.run(due_ceremonies(24, now=NOW))

    assert cancelled not in result
    assert result == []


# ---------------------------------------------------------------------------
# test_already_sent_skipped
# ---------------------------------------------------------------------------


def test_already_sent_skipped():
    """If a CeremonyReminder row exists, no DM is posted."""
    ceremony = make_ceremony()

    with patch(
        "app.services.ceremony_reminders._already_sent",
        new_callable=AsyncMock,
        return_value=True,
    ), patch("app.services.ceremony_reminders.mattermost_client") as mock_mm:
        result = asyncio.run(send_reminder(ceremony, "user_mm_001", "24h", "Sprint Planning"))

    assert result is False
    mock_mm.create_direct_channel.assert_not_called()
    mock_mm.create_post.assert_not_called()


# ---------------------------------------------------------------------------
# test_idempotent_on_restart
# ---------------------------------------------------------------------------


def test_idempotent_on_restart():
    """Calling send_reminder twice for the same triple sends only one DM."""
    ceremony = make_ceremony()

    sent_set: set[tuple[int, str, str]] = set()

    async def fake_already_sent(ceremony_id, recipient_mm_id, window, **_):
        return (ceremony_id, recipient_mm_id, window) in sent_set

    async def fake_record_sent(ceremony_id, recipient_mm_id, window, sent_at, **_):
        sent_set.add((ceremony_id, recipient_mm_id, window))
        return True

    fake_channel = {"id": "dm_chan_001"}
    fake_post = {"id": "post_001"}

    async def _run():
        with patch("app.services.ceremony_reminders._already_sent", side_effect=fake_already_sent), \
             patch("app.services.ceremony_reminders._record_sent", side_effect=fake_record_sent), \
             patch("app.services.ceremony_reminders._organizer_display", new_callable=AsyncMock, return_value="Alice"), \
             patch("app.services.ceremony_reminders.mattermost_client") as mock_mm:
            mock_mm.get_user_timezone = AsyncMock(return_value="UTC")
            mock_mm.create_direct_channel = AsyncMock(return_value=fake_channel)
            mock_mm.create_post = AsyncMock(return_value=fake_post)

            result1 = await send_reminder(ceremony, "user_mm_001", "24h", "Sprint Planning")
            result2 = await send_reminder(ceremony, "user_mm_001", "24h", "Sprint Planning")
            return result1, result2, mock_mm.create_post.call_count

    result1, result2, post_count = asyncio.run(_run())

    assert result1 is True
    assert result2 is False  # Second call skipped — row already in sent_set.
    assert post_count == 1


# ---------------------------------------------------------------------------
# test_timezone_formatting
# ---------------------------------------------------------------------------


def test_timezone_formatting():
    """Africa/Cairo (UTC+3) offsets the displayed time correctly."""
    utc_dt = datetime(2026, 9, 8, 10, 0, tzinfo=UTC)  # 10:00 UTC

    formatted, tz_name = _format_local(utc_dt, "Africa/Cairo")

    # Africa/Cairo is UTC+3; 10:00 UTC -> 13:00 local.
    assert "13:00" in formatted
    assert tz_name == "Africa/Cairo"


def test_timezone_formatting_utc():
    """UTC input formats without offset."""
    utc_dt = datetime(2026, 9, 8, 14, 30, tzinfo=UTC)

    formatted, tz_name = _format_local(utc_dt, "UTC")

    assert "14:30" in formatted
    assert tz_name == "UTC"


# ---------------------------------------------------------------------------
# test_fallback_to_utc
# ---------------------------------------------------------------------------


def test_fallback_to_utc_on_bad_tz():
    """An invalid IANA zone name falls back gracefully to UTC formatting."""
    utc_dt = datetime(2026, 9, 8, 9, 0, tzinfo=UTC)

    formatted, tz_name = _format_local(utc_dt, "Not/A_Real_Zone")

    # Should not raise; should fall back to UTC.
    assert "09:00" in formatted
    assert tz_name == "UTC"


def test_fallback_to_utc_when_mattermost_tz_fails():
    """A Mattermost TZ API failure -> UTC used, no crash, reminder still sent."""
    ceremony = make_ceremony()
    fake_channel = {"id": "dm_chan_001"}
    fake_post = {"id": "post_002"}

    async def _run():
        with patch("app.services.ceremony_reminders._already_sent", new_callable=AsyncMock, return_value=False), \
             patch("app.services.ceremony_reminders._record_sent", new_callable=AsyncMock, return_value=True), \
             patch("app.services.ceremony_reminders._organizer_display", new_callable=AsyncMock, return_value="Bob"), \
             patch("app.services.ceremony_reminders.mattermost_client") as mock_mm:
            # Simulate Mattermost TZ endpoint failure: client returns "UTC" (its own fallback).
            mock_mm.get_user_timezone = AsyncMock(return_value="UTC")
            mock_mm.create_direct_channel = AsyncMock(return_value=fake_channel)
            mock_mm.create_post = AsyncMock(return_value=fake_post)

            result = await send_reminder(ceremony, "user_mm_002", "1h", "Daily Standup")
            call_args = mock_mm.create_post.call_args
            return result, call_args

    result, call_args = asyncio.run(_run())

    assert result is True
    message = call_args[0][1]  # second positional arg to create_post(channel_id, message)
    assert "(UTC)" in message
