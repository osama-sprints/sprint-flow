"""Comprehensive verification suite for Ceremony Scheduler capabilities."""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch
import pytest

from app.core.langgraph.tools.ceremony_scheduler import (
    schedule_ceremony,
    amend_ceremony,
    read_ceremonies,
)
from app.models.cohort import Cohort
from app.services.admin_service import AuthorisationRefusalError

@pytest.fixture
def mock_db_session():
    session = MagicMock()
    # Mock lookup tables
    mock_type = MagicMock()
    mock_type.id = "type_1"
    session.query.return_value.filter_by.return_value.first.return_value = mock_type
    return session


@pytest.fixture
def mock_admin_service():
    service = MagicMock()
    # By default, pass authorization
    service.evaluate_permission.return_value = None
    return service


# 1. SCHEDULE CEREMONY
@patch("app.core.langgraph.tools.ceremony_scheduler.Session")
@patch("app.core.langgraph.tools.ceremony_scheduler.admin_service")
@patch("app.core.langgraph.tools.ceremony_scheduler.find_conflict")
def test_schedule_ceremony_success(mock_find, mock_admin, mock_session_cls, mock_db_session):
    mock_session_cls.return_value.__enter__.return_value = mock_db_session
    mock_admin.evaluate_permission.return_value = None
    mock_find.return_value = None  # No conflicts!
    
    res = schedule_ceremony.invoke({
        "cohort_id": 2026,
        "ceremony_type": "standup",
        "raw_time": "tomorrow at 10 AM UTC",
        "organizer_id": "admin_user",
        "agenda": "Weekly sync"
    })
    
    assert "SUCCESS" in res
    mock_admin.evaluate_permission.assert_called_once_with(
        requester_id="admin_user", required_role="admin", cohort_id="2026"
    )
    mock_db_session.add.assert_called_once()
    mock_db_session.commit.assert_called_once()


@patch("app.core.langgraph.tools.ceremony_scheduler.Session")
@patch("app.core.langgraph.tools.ceremony_scheduler.admin_service")
@patch("app.core.langgraph.tools.ceremony_scheduler.find_conflict")
def test_schedule_ceremony_unauthorized(mock_find, mock_admin, mock_session_cls, mock_db_session):
    mock_session_cls.return_value.__enter__.return_value = mock_db_session
    mock_admin.evaluate_permission.side_effect = AuthorisationRefusalError(
        requester_id="learner_user", action="schedule_ceremony", cohort_id="2026"
    )
    mock_find.return_value = None
    
    res = schedule_ceremony.invoke({
        "cohort_id": 2026,
        "ceremony_type": "standup",
        "raw_time": "tomorrow at 10 AM UTC",
        "organizer_id": "learner_user"
    })
    
    assert "Error: You don't have permission" in res
    mock_db_session.add.assert_not_called()


@patch("app.core.langgraph.tools.ceremony_scheduler.Session")
@patch("app.core.langgraph.tools.ceremony_scheduler.admin_service")
@patch("app.core.langgraph.tools.ceremony_scheduler.find_conflict")
def test_schedule_ceremony_missing_cohort(mock_find, mock_admin, mock_session_cls, mock_db_session):
    mock_session_cls.return_value.__enter__.return_value = mock_db_session
    mock_admin.evaluate_permission.return_value = None
    mock_db_session.get.return_value = None

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


@patch("app.core.langgraph.tools.ceremony_scheduler.Session")
@patch("app.core.langgraph.tools.ceremony_scheduler.admin_service")
@patch("app.core.langgraph.tools.ceremony_scheduler.find_conflict")
@patch("app.core.langgraph.tools.ceremony_scheduler._ceremony_type_name")
def test_schedule_ceremony_conflict(mock_type_name, mock_find, mock_admin, mock_session_cls, mock_db_session):
    mock_session_cls.return_value.__enter__.return_value = mock_db_session
    mock_admin.evaluate_permission.return_value = None
    mock_type_name.return_value = "planning"
    
    # Simulate an existing ceremony that conflicts
    conflict_obj = MagicMock()
    conflict_obj.scheduled_at = datetime.now(timezone.utc) + timedelta(days=1)
    conflict_obj.type_id = "type_1"
    mock_find.return_value = conflict_obj
    
    res = schedule_ceremony.invoke({
        "cohort_id": 2026,
        "ceremony_type": "standup",
        "raw_time": "tomorrow at 10 AM UTC",
        "organizer_id": "admin_user"
    })
    
    assert "Error:" in res and "already has a 'planning' at" in res
    mock_db_session.add.assert_not_called()


# 2. AMEND CEREMONY
@patch("app.core.langgraph.tools.ceremony_scheduler.Session")
def test_amend_ceremony_success(mock_session_cls, mock_db_session):
    mock_session_cls.return_value.__enter__.return_value = mock_db_session
    
    target_ceremony = MagicMock()
    target_ceremony.organizer = "admin_user"
    target_ceremony.status = "scheduled"
    mock_db_session.get.return_value = target_ceremony
    
    res = amend_ceremony.invoke({
        "ceremony_id": 1,
        "organizer_id": "admin_user",
        "cancel": True
    })
    
    assert "SUCCESS" in res
    assert target_ceremony.status == "cancelled"
    mock_db_session.commit.assert_called_once()


@patch("app.core.langgraph.tools.ceremony_scheduler.Session")
def test_amend_ceremony_unauthorized_organizer(mock_session_cls, mock_db_session):
    mock_session_cls.return_value.__enter__.return_value = mock_db_session
    
    target_ceremony = MagicMock()
    target_ceremony.organizer = "other_admin"
    target_ceremony.status = "scheduled"
    mock_db_session.get.return_value = target_ceremony
    
    res = amend_ceremony.invoke({
        "ceremony_id": 1,
        "organizer_id": "attacker_user",
        "cancel": True
    })
    
    assert "Error: Only the original organizer" in res
    mock_db_session.commit.assert_not_called()


# 3. READ CEREMONIES
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
    
    res = read_ceremonies.invoke({
        "cohort_id": 2026,
        "include_inactive": False
    })
    
    assert "Upcoming ceremonies for cohort #2026:" in res
    assert "#1" in res
    assert "standup" in res
    assert "Discuss stuff" in res
