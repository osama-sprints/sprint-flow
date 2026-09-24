"""Pure-logic tests for the back-office service and tool wrappers (no database, no network)."""

from datetime import date

import pytest

from app.core.requester import (
    current_requester,
)
from app.models.enums import RoleKey
from app.services import back_office
from app.services.authorisation import (
    REFUSAL_MESSAGE,
    ValidationFailed,
)

REFUSED = f"[AUTHORISATION_REFUSED] {REFUSAL_MESSAGE}"


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
        ("scrum master", RoleKey.SCRUM_MASTER),
        ("Scrum Master", RoleKey.SCRUM_MASTER),
        ("tech lead", RoleKey.TECH_LEAD),
        ("tech_lead", RoleKey.TECH_LEAD),
        ("ops support", RoleKey.OPS_SUPPORT),
        ("ops", RoleKey.OPS_SUPPORT),
        ("learner", RoleKey.LEARNER),
    ],
)
def test_role_from_text_resolves_known_variants(typed: str, expected: RoleKey):
    assert back_office.role_from_text(typed) == expected


def test_role_from_text_raises_validation_failed_on_unknown():
    with pytest.raises(ValidationFailed, match="Unknown role 'wizard'"):
        back_office.role_from_text("wizard")


@pytest.mark.parametrize(
    ("typed", "expected"),
    [
        ("2026-09-01", date(2026, 9, 1)),
        (" 2026-12-31 ", date(2026, 12, 31)),
    ],
)
def test_parse_iso_date_valid(typed: str, expected: date):
    assert back_office.parse_iso_date(typed, "start date") == expected


def test_parse_iso_date_invalid_raises_validation_failed():
    with pytest.raises(ValidationFailed, match="The start date '2026-02-30' is not a valid date"):
        back_office.parse_iso_date("2026-02-30", "start date")


def test_resolve_sprint_dates_defaults():
    today = date(2026, 9, 8)
    start, end = back_office.resolve_sprint_dates(None, None, today=today, default_length_days=14)
    assert start == today
    assert end == date(2026, 9, 22)


def test_resolve_sprint_dates_rejects_end_before_start():
    with pytest.raises(ValidationFailed, match="is before the start date"):
        back_office.resolve_sprint_dates("2026-09-10", "2026-09-05")


def test_clean_name_trims_and_bounds():
    assert back_office._clean_name("   Sprint 1   ", what="sprint", max_length=50) == "Sprint 1"
    with pytest.raises(ValidationFailed, match="A sprint needs a name"):
        back_office._clean_name("   ", what="sprint", max_length=50)
    with pytest.raises(ValidationFailed, match="can be at most 5 characters"):
        back_office._clean_name("Sprint 1", what="sprint", max_length=5)