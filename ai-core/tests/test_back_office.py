"""Pure-logic tests for the back-office service and tool wrappers (no database, no network)."""

import asyncio
from datetime import date

import pytest

from app.core.langgraph.tools import back_office as back_office_tools
from app.core.langgraph.tools.results import (
    ResultCode,
    result_code_of,
)
from app.core.requester import (
    RequesterContext,
    current_requester,
)
from app.models import (
    Role,
    User,
)
from app.models.enums import RoleKey
from app.services import back_office
from app.services import authorisation
from app.services.authorisation import (
    REFUSAL_MESSAGE,
    AuthorisationDecision,
    AuthorisationRefused,
    ValidationFailed,
)

REFUSED = f"[AUTHORISATION_REFUSED] {REFUSAL_MESSAGE}"
CONTRACT_SIGNATURES = {
    "create_channel": ["name", "mattermost_team"],
    "assign_role": ["person", "role", "channel"],
    "open_sprint": ["channel", "sprint_name", "start_date", "end_date"],
    "list_channel_roles_for_requester": [],
    "list_channel_members": ["channel"],
}
IDENTITY_FRAGMENTS = ("requester", "user_id", "mattermost_user", "superadmin", "identity", "channel", "is_admin")


@pytest.fixture(autouse=True)
def no_requester():
    """Every test starts and ends with nobody bound."""
    token = current_requester.set(None)
    yield
    current_requester.reset(token)


# ---------------------------------------------------------------------------
# Role and date parsing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("typed", "expected"),
    [
        ("scrum_master", RoleKey.SCRUM_MASTER),
        ("Scrum Master", RoleKey.SCRUM_MASTER),
        ("  scrummaster ", RoleKey.SCRUM_MASTER),
        ("tech-lead", RoleKey.TECH_LEAD),
        ("Technical Lead", RoleKey.TECH_LEAD),
        ("ops", RoleKey.OPS_SUPPORT),
        ("student", RoleKey.LEARNER),
        ("LEARNER", RoleKey.LEARNER),
    ],
)
def test_role_from_text_accepts_key_label_and_alias(typed, expected):
    assert back_office.role_from_text(typed) is expected


def test_role_from_text_unknown_role_is_a_validation_failure_naming_known_roles():
    with pytest.raises(ValidationFailed) as excinfo:
        back_office.role_from_text(" emperor ")
    assert str(excinfo.value) == "Unknown role 'emperor'. Known roles: learner, tech lead, ops support, scrum master."


def test_parse_iso_date_accepts_calendar_dates_and_rejects_the_rest():
    assert back_office.parse_iso_date(" 2030-02-28 ", "start date") == date(2030, 2, 28)
    with pytest.raises(ValidationFailed) as excinfo:
        back_office.parse_iso_date("next monday", "start date")
    assert "start date 'next monday'" in str(excinfo.value) and "YYYY-MM-DD" in str(excinfo.value)


def test_resolve_sprint_dates_defaults_to_today_plus_configured_length():
    today = date(2030, 5, 1)
    assert back_office.resolve_sprint_dates(None, None, today=today, default_length_days=14) == (
        today,
        date(2030, 5, 15),
    )
    assert back_office.resolve_sprint_dates("2030-06-01", None, today=today, default_length_days=10) == (
        date(2030, 6, 1),
        date(2030, 6, 11),
    )
    assert back_office.resolve_sprint_dates("2030-06-01", "2030-06-01", today=today) == (
        date(2030, 6, 1),
        date(2030, 6, 1),
    )


def test_resolve_sprint_dates_rejects_end_before_start():
    with pytest.raises(ValidationFailed) as excinfo:
        back_office.resolve_sprint_dates("2030-06-10", "2030-06-01", today=date(2030, 5, 1))
    assert "2030-06-01 is before the start date 2030-06-10" in str(excinfo.value)


def test_resolve_sprint_dates_uses_the_setting_by_default(monkeypatch):
    monkeypatch.setattr(back_office.settings, "SPRINT_DEFAULT_LENGTH_DAYS", 7)
    start, end = back_office.resolve_sprint_dates(None, None, today=date(2030, 1, 1))
    assert (start, end) == (date(2030, 1, 1), date(2030, 1, 8))

