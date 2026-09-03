"""Comprehensive verification suite for Ceremony Scheduler capabilities."""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch
import pytest

from app.core.langgraph.tools.ceremony_scheduler import (
    schedule_ceremony,
    amend_ceremony,
    read_ceremonies,
    validate_and_parse_time,
    _is_affirmative,
)
from app.models.cohort import Cohort
from app.services.admin_service import AuthorisationRefusalError


@pytest.fixture
def mock_db_session():
    session = MagicMock()
    mock_type = MagicMock()
    mock_type.id = "type_1"
    session.query.return_value.filter_by.return_value.first.return_value = mock_type
    return session


@pytest.fixture
def mock_admin_service():
    service = MagicMock()
    service.evaluate_permission.return_value = None
    return service


# ---------------------------------------------------------------------------
# TIME VALIDATION
# ---------------------------------------------------------------------------

def test_validate_time_accepts_explicit_utc_time():
    parsed, error = validate_and_parse_time("January 1, 2100 at 6 PM UTC")

    assert error == ""
    assert parsed == datetime(2100, 1, 1, 18, 0, tzinfo=timezone.utc)


def test_validate_time_rejects_missing_timezone():
    parsed, error = validate_and_parse_time("January 1, 2100 at 6 PM")

    assert parsed is None
    assert "time is ambiguous" in error


def test_validate_time_rejects_missing_ampm():
    parsed, error = validate_and_parse_time("January 1, 2100 at 6 UTC")

    assert parsed is None
    assert error.startswith("Error:")


def test_validate_time_rejects_unparseable_input():
    parsed, error = validate_and_parse_time("not a real time")

    assert parsed is None
    assert "couldn't understand that time" in error


def test_validate_time_rejects_past_time():
    parsed, error = validate_and_parse_time("January 1, 2000 at 6 PM UTC")

    assert parsed is None
    assert "time is in the past" in error


# ---------------------------------------------------------------------------
# _IS_AFFIRMATIVE
# ---------------------------------------------------------------------------

def test_is_affirmative_accepts_yes_variants():
    for word in ["yes", "y", "ok", "sure", "go", "confirm", "confirmed", "yep", "yeah"]:
        assert _is_affirmative(word), f"Expected {word!r} to be affirmative"
    # strips whitespace and is case-insensitive
    assert _is_affirmative("  YES  ")
    assert _is_affirmative("GO AHEAD")


def test_is_affirmative_rejects_negative_responses():
    for word in ["no", "n", "nope", "cancel", "abort", "stop", "skip", ""]:
        assert not _is_affirmative(word), f"Expected {word!r} NOT to be affirmative"


# ---------------------------------------------------------------------------
# 1. SCHEDULE CEREMONY
# ---------------------------------------------------------------------------

@patch("app.core.langgraph.tools.ceremony_scheduler.ask_human")
@patch("app.core.langgraph.tools.ceremony_scheduler.Session")
@patch("app.core.langgraph.tools.ceremony_scheduler.admin_service")
@patch("app.core.langgraph.tools.ceremony_scheduler.find_conflict")
def test_schedule_ceremony_success(
    mock_find, mock_admin, mock_session_cls, mock_ask_human, mock_db_session
):
    """Happy path: permission passes, time valid, user confirms -> row written."""
    mock_session_cls.return_value.__enter__.return_value = mock_db_session
    mock_admin.evaluate_permission.return_value = None
    mock_find.return_value = None
    mock_ask_human.invoke.return_value = "yes"  # User confirms the parsed time

    res = schedule_ceremony.invoke({
        "cohort_id": 2026,
        "ceremony_type": "standup",
        "raw_time": "tomorrow at 10 AM UTC",
        "organizer_id": "admin_user",
        "agenda": "Weekly sync",
    })

    assert "SUCCESS" in res
    mock_admin.evaluate_permission.assert_called_once_with(
        requester_id="admin_user", required_role="admin", cohort_id="2026"
    )
    # ask_human must have been called exactly once for the confirmation prompt
    mock_ask_human.invoke.assert_called_once()
    mock_db_session.add.assert_called_once()
    mock_db_session.commit.assert_called_once()


@patch("app.core.langgraph.tools.ceremony_scheduler.Session")
@patch("app.core.langgraph.tools.ceremony_scheduler.admin_service")
def test_schedule_ceremony_unauthorized(mock_admin, mock_session_cls, mock_db_session):
    """Non-admin users are rejected before any ask_human or DB operation."""
    mock_session_cls.return_value.__enter__.return_value = mock_db_session
    mock_admin.evaluate_permission.side_effect = AuthorisationRefusalError(
        requester_id="learner_user", action="schedule_ceremony", cohort_id="2026"
    )

    res = schedule_ceremony.invoke({
        "cohort_id": 2026,
        "ceremony_type": "standup",
        "raw_time": "tomorrow at 10 AM UTC",
        "organizer_id": "learner_user",
    })

    assert "Error: You don't have permission" in res
    mock_db_session.add.assert_not_called()


@patch("app.core.langgraph.tools.ceremony_scheduler.ask_human")
@patch("app.core.langgraph.tools.ceremony_scheduler.Session")
@patch("app.core.langgraph.tools.ceremony_scheduler.admin_service")
@patch("app.core.langgraph.tools.ceremony_scheduler.find_conflict")
def test_schedule_ceremony_missing_cohort(
    mock_find, mock_admin, mock_session_cls, mock_ask_human, mock_db_session
):
    """Unknown cohort ID is rejected after confirmation, before DB insert."""
    mock_session_cls.return_value.__enter__.return_value = mock_db_session
    mock_admin.evaluate_permission.return_value = None
    mock_db_session.get.return_value = None  # cohort not found
    mock_ask_human.invoke.return_value = "yes"

    res = schedule_ceremony.invoke({
        "cohort_id": 202,
        "ceremony_type": "planning",
        "raw_time": "tomorrow at 6 PM UTC",
        "organizer_id": "admin_user",
    })

    assert res == (
        "Error: Cohort #202 does not exist. "
        "Ask the user to create it or choose an existing cohort."
    )
    mock_db_session.get.assert_called_once_with(Cohort, 202)
    mock_db_session.add.assert_not_called()
    mock_db_session.commit.assert_not_called()
    mock_find.assert_not_called()


@patch("app.core.langgraph.tools.ceremony_scheduler.ask_human")
@patch("app.core.langgraph.tools.ceremony_scheduler.Session")
@patch("app.core.langgraph.tools.ceremony_scheduler.admin_service")
@patch("app.core.langgraph.tools.ceremony_scheduler.find_conflict")
@patch("app.core.langgraph.tools.ceremony_scheduler._ceremony_type_name")
def test_schedule_ceremony_conflict(
    mock_type_name, mock_find, mock_admin, mock_session_cls, mock_ask_human, mock_db_session
):
    """Time conflict blocks the insert even after user confirms the parsed time."""
    mock_session_cls.return_value.__enter__.return_value = mock_db_session
    mock_admin.evaluate_permission.return_value = None
    mock_type_name.return_value = "planning"
    mock_ask_human.invoke.return_value = "yes"

    conflict_obj = MagicMock()
    conflict_obj.scheduled_at = datetime.now(timezone.utc) + timedelta(days=1)
    conflict_obj.type_id = "type_1"
    mock_find.return_value = conflict_obj

    res = schedule_ceremony.invoke({
        "cohort_id": 2026,
        "ceremony_type": "standup",
        "raw_time": "tomorrow at 10 AM UTC",
        "organizer_id": "admin_user",
    })

    assert "Error:" in res and "already has a 'planning' at" in res
    mock_db_session.add.assert_not_called()


@patch("app.core.langgraph.tools.ceremony_scheduler.ask_human")
@patch("app.core.langgraph.tools.ceremony_scheduler.Session")
@patch("app.core.langgraph.tools.ceremony_scheduler.admin_service")
@patch("app.core.langgraph.tools.ceremony_scheduler.find_conflict")
def test_schedule_ceremony_aborts_on_negative_confirmation(
    mock_find, mock_admin, mock_session_cls, mock_ask_human, mock_db_session
):
    """User replies 'no' to the time-confirmation prompt -> no DB write."""
    mock_session_cls.return_value.__enter__.return_value = mock_db_session
    mock_admin.evaluate_permission.return_value = None
    mock_find.return_value = None
    mock_ask_human.invoke.return_value = "no"

    res = schedule_ceremony.invoke({
        "cohort_id": 2026,
        "ceremony_type": "standup",
        "raw_time": "tomorrow at 10 AM UTC",
        "organizer_id": "admin_user",
    })

    assert "cancelled" in res.lower() or "no changes" in res.lower()
    mock_db_session.add.assert_not_called()
    mock_db_session.commit.assert_not_called()


@patch("app.core.langgraph.tools.ceremony_scheduler.ask_human")
@patch("app.core.langgraph.tools.ceremony_scheduler.Session")
@patch("app.core.langgraph.tools.ceremony_scheduler.admin_service")
def test_schedule_ceremony_ambiguous_time_calls_ask_human(
    mock_admin, mock_session_cls, mock_ask_human, mock_db_session
):
    """Ambiguous time (missing timezone) triggers ask_human for clarification.
    The tool returns a re-invoke hint; no DB write occurs."""
    mock_session_cls.return_value.__enter__.return_value = mock_db_session
    mock_admin.evaluate_permission.return_value = None
    mock_ask_human.invoke.return_value = "Monday 9 AM UTC"

    res = schedule_ceremony.invoke({
        "cohort_id": 2026,
        "ceremony_type": "standup",
        "raw_time": "Monday at 9",  # missing timezone and AM/PM
        "organizer_id": "admin_user",
    })

    mock_ask_human.invoke.assert_called_once()
    assert "corrected time" in res.lower() or "Monday 9 AM UTC" in res
    mock_db_session.add.assert_not_called()
    mock_db_session.commit.assert_not_called()


# ---------------------------------------------------------------------------
# 2. AMEND CEREMONY
# ---------------------------------------------------------------------------

@patch("app.core.langgraph.tools.ceremony_scheduler.ask_human")
@patch("app.core.langgraph.tools.ceremony_scheduler.Session")
def test_amend_ceremony_success_cancel(mock_session_cls, mock_ask_human, mock_db_session):
    """Organizer cancels a future ceremony after confirming -> status set to cancelled."""
    mock_session_cls.return_value.__enter__.return_value = mock_db_session
    mock_ask_human.invoke.return_value = "yes"

    target = MagicMock()
    target.organizer = "admin_user"
    target.status = "scheduled"
    target.scheduled_at = datetime.now(timezone.utc) + timedelta(days=1)
    mock_db_session.get.return_value = target

    res = amend_ceremony.invoke({
        "ceremony_id": 1,
        "organizer_id": "admin_user",
        "cancel": True,
    })

    assert "SUCCESS" in res
    assert target.status == "cancelled"
    mock_db_session.commit.assert_called()


@patch("app.core.langgraph.tools.ceremony_scheduler.Session")
def test_amend_ceremony_unauthorized_organizer(mock_session_cls, mock_db_session):
    """A different user cannot amend the ceremony — no confirmation prompt needed."""
    mock_session_cls.return_value.__enter__.return_value = mock_db_session

    target = MagicMock()
    target.organizer = "other_admin"
    target.status = "scheduled"
    target.scheduled_at = datetime.now(timezone.utc) + timedelta(days=1)
    mock_db_session.get.return_value = target

    res = amend_ceremony.invoke({
        "ceremony_id": 1,
        "organizer_id": "attacker_user",
        "cancel": True,
    })

    assert "Error: Only the original organizer" in res
    mock_db_session.commit.assert_not_called()


@patch("app.core.langgraph.tools.ceremony_scheduler.Session")
def test_amend_ceremony_rejects_past_ceremony(mock_session_cls, mock_db_session):
    """A ceremony whose scheduled_at has already passed cannot be edited."""
    mock_session_cls.return_value.__enter__.return_value = mock_db_session

    past_ceremony = MagicMock()
    past_ceremony.organizer = "admin_user"
    past_ceremony.status = "scheduled"
    past_ceremony.scheduled_at = datetime.now(timezone.utc) - timedelta(hours=2)
    mock_db_session.get.return_value = past_ceremony

    res = amend_ceremony.invoke({
        "ceremony_id": 42,
        "organizer_id": "admin_user",
        "cancel": True,
    })

    assert "Error:" in res
    assert "already passed" in res
    mock_db_session.commit.assert_not_called()


@patch("app.core.langgraph.tools.ceremony_scheduler.ask_human")
@patch("app.core.langgraph.tools.ceremony_scheduler.Session")
def test_amend_ceremony_aborts_on_negative_confirmation(
    mock_session_cls, mock_ask_human, mock_db_session
):
    """User says 'no' to the cancellation prompt -> no DB write."""
    mock_session_cls.return_value.__enter__.return_value = mock_db_session
    mock_ask_human.invoke.return_value = "no"

    target = MagicMock()
    target.organizer = "admin_user"
    target.status = "scheduled"
    target.scheduled_at = datetime.now(timezone.utc) + timedelta(days=1)
    mock_db_session.get.return_value = target

    res = amend_ceremony.invoke({
        "ceremony_id": 1,
        "organizer_id": "admin_user",
        "cancel": True,
    })

    assert "aborted" in res.lower() or "no changes" in res.lower()
    mock_db_session.commit.assert_not_called()


@patch("app.core.langgraph.tools.ceremony_scheduler.ask_human")
@patch("app.core.langgraph.tools.ceremony_scheduler.Session")
def test_amend_ceremony_ambiguous_time_calls_ask_human(
    mock_session_cls, mock_ask_human, mock_db_session
):
    """Ambiguous new_raw_time triggers ask_human for clarification; no write occurs."""
    mock_session_cls.return_value.__enter__.return_value = mock_db_session
    mock_ask_human.invoke.return_value = "Friday 2 PM UTC"

    target = MagicMock()
    target.organizer = "admin_user"
    target.status = "scheduled"
    target.scheduled_at = datetime.now(timezone.utc) + timedelta(days=1)
    mock_db_session.get.return_value = target

    res = amend_ceremony.invoke({
        "ceremony_id": 1,
        "organizer_id": "admin_user",
        "new_raw_time": "Friday at 2",  # missing timezone and AM/PM
    })

    mock_ask_human.invoke.assert_called_once()
    assert "corrected time" in res.lower() or "Friday 2 PM UTC" in res
    mock_db_session.commit.assert_not_called()


@patch("app.core.langgraph.tools.ceremony_scheduler.Session")
def test_amend_ceremony_not_found(mock_session_cls, mock_db_session):
    """Attempting to amend a non-existent ceremony ID returns a clear error."""
    mock_session_cls.return_value.__enter__.return_value = mock_db_session
    mock_db_session.get.return_value = None

    res = amend_ceremony.invoke({
        "ceremony_id": 9999,
        "organizer_id": "admin_user",
        "cancel": True,
    })

    assert "Error:" in res
    assert "No ceremony found" in res
    mock_db_session.commit.assert_not_called()


# ---------------------------------------------------------------------------
# 3. READ CEREMONIES
# ---------------------------------------------------------------------------

@patch("app.core.langgraph.tools.ceremony_scheduler.Session")
@patch("app.core.langgraph.tools.ceremony_scheduler._ceremony_type_name")
def test_read_ceremonies(mock_type_name, mock_session_cls, mock_db_session):
    mock_session_cls.return_value.__enter__.return_value = mock_db_session
    mock_type_name.return_value = "standup"

    cerem1 = MagicMock()
    cerem1.id = 1
    cerem1.type_id = "type_1"
    cerem1.scheduled_at = datetime.now(timezone.utc) + timedelta(hours=2)
    cerem1.status = "scheduled"
    cerem1.organizer = "admin_user"
    cerem1.agenda = "Discuss stuff"

    mock_db_session.exec.return_value.all.return_value = [cerem1]

    res = read_ceremonies.invoke({"cohort_id": 2026, "include_inactive": False})

    assert "Upcoming ceremonies for cohort #2026:" in res
    assert "#1" in res
    assert "standup" in res
    assert "Discuss stuff" in res


@patch("app.core.langgraph.tools.ceremony_scheduler.Session")
def test_read_ceremonies_empty(mock_session_cls, mock_db_session):
    """When no ceremonies exist a clear 'none found' message is returned."""
    mock_session_cls.return_value.__enter__.return_value = mock_db_session
    mock_db_session.exec.return_value.all.return_value = []

    res = read_ceremonies.invoke({"cohort_id": 999})

    assert "No upcoming ceremonies found for cohort #999." in res
