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
    Cohort,
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
    "create_cohort": ["name", "mattermost_team"],
    "assign_role": ["person", "role", "cohort"],
    "open_sprint": ["cohort", "sprint_name", "start_date", "end_date"],
    "list_cohorts": [],
    "list_cohort_members": ["cohort"],
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


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("abcdefghijklmnopqrstuvwxyz", True),
        ("  9k3j2h1g0f9e8d7c6b5a4z3y2x  ", True),
        ("sprints-community", False),
        ("", False),
    ],
)
def test_looks_like_mattermost_id(value, expected):
    assert back_office.looks_like_mattermost_id(value) is expected


def test_describe_cohorts_reads_well_when_empty_and_when_populated():
    assert back_office._describe_cohorts([], is_superadmin=False) == "You are not a member of any cohort yet."
    assert back_office._describe_cohorts([], is_superadmin=True) == "There are no cohorts yet."
    cohort = Cohort(id=7, name="Backend-01", is_active=False)
    role = Role(id=1, key="learner", label="Learner")
    text = back_office._describe_cohorts([(cohort, role)], is_superadmin=False)
    assert text.startswith("Your cohorts:") and "Backend-01 (id 7) (inactive) — your role: Learner" in text


# ---------------------------------------------------------------------------
# Tool boundary
# ---------------------------------------------------------------------------


def test_tool_names_and_signatures_match_the_contract():
    assert [tool.name for tool in back_office_tools.TOOLS] == list(CONTRACT_SIGNATURES)
    for tool in back_office_tools.TOOLS:
        assert list(tool.args) == CONTRACT_SIGNATURES[tool.name], tool.name


def test_no_tool_argument_can_carry_the_requesters_identity():
    for tool in back_office_tools.TOOLS:
        for argument in tool.args:
            assert not any(fragment in argument for fragment in IDENTITY_FRAGMENTS), f"{tool.name}.{argument}"


def test_tool_docstrings_tell_the_model_to_relay_refusals():
    for tool in back_office_tools.TOOLS:
        if tool.name == "list_cohorts":
            continue
        assert "AUTHORISATION_REFUSED" in tool.description, tool.name
        assert "verbatim" in tool.description, tool.name


def test_refusal_becomes_the_fixed_result_string(monkeypatch):
    async def refuse(*_args, **_kwargs):
        raise AuthorisationRefused("not_superadmin", action="create_cohort")

    monkeypatch.setattr(back_office, "create_cohort", refuse)
    assert asyncio.run(back_office_tools.create_cohort.ainvoke({"name": "X"})) == REFUSED


def test_validation_failure_becomes_a_validation_error_with_its_sentence(monkeypatch):
    async def invalid(*_args, **_kwargs):
        raise ValidationFailed("Unknown role 'emperor'. Known roles: learner, tech lead, ops support, scrum master.")

    monkeypatch.setattr(back_office, "assign_role", invalid)
    result = asyncio.run(back_office_tools.assign_role.ainvoke({"person": "@a", "role": "emperor", "cohort": "A"}))
    assert (
        result
        == "[VALIDATION_ERROR] Unknown role 'emperor'. Known roles: learner, tech lead, ops support, scrum master."
    )
    assert result_code_of(result) != ResultCode.AUTHORISATION_REFUSED


def test_unexpected_failure_becomes_a_readable_system_error_without_database_detail(monkeypatch):
    async def explode(*_args, **_kwargs):
        raise RuntimeError('psycopg.OperationalError: connection to server at "10.0.0.7" failed')

    monkeypatch.setattr(back_office, "open_sprint", explode)
    result = asyncio.run(back_office_tools.open_sprint.ainvoke({"cohort": "A", "sprint_name": "S1"}))
    assert result_code_of(result) == ResultCode.SYSTEM_ERROR
    assert "psycopg" not in result and "10.0.0.7" not in result
    assert "try again" in result


def test_tools_refuse_when_no_requester_is_bound_before_touching_data(monkeypatch):
    async def must_not_run(*_args, **_kwargs):
        raise AssertionError("data access happened before authorisation")

    monkeypatch.setattr(back_office.cohort_repo, "get_cohort_by_name", must_not_run)
    monkeypatch.setattr(back_office.cohort_repo, "resolve_cohort", must_not_run)
    monkeypatch.setattr(authorisation.identity_repo, "get_user_by_mattermost_id", must_not_run)
    assert asyncio.run(back_office_tools.create_cohort.ainvoke({"name": "X"})) == REFUSED
    assert asyncio.run(back_office_tools.list_cohorts.ainvoke({})) == REFUSED
    assert (
        asyncio.run(back_office_tools.assign_role.ainvoke({"person": "@a", "role": "learner", "cohort": "A"}))
        == REFUSED
    )
    assert asyncio.run(back_office_tools.open_sprint.ainvoke({"cohort": "A", "sprint_name": "S1"})) == REFUSED
    assert asyncio.run(back_office_tools.list_cohort_members.ainvoke({"cohort": "A"})) == REFUSED


def test_refusal_precedes_validation_so_an_outsider_learns_nothing(monkeypatch):
    """A requester without authority gets the refusal even when the role is nonsense."""
    cohort = Cohort(id=3, name="Backend-01")

    async def resolve(_reference, session=None):
        return cohort

    async def refuse(_requester, _cohort_id, **_kwargs):
        raise AuthorisationRefused("not_a_member", action="assign_role", cohort_id=3)

    def role_must_not_be_parsed(_value):
        raise AssertionError("role parsed before authorisation")

    monkeypatch.setattr(back_office.cohort_repo, "resolve_cohort", resolve)
    monkeypatch.setattr(back_office, "require_cohort_authority", refuse)
    monkeypatch.setattr(back_office, "role_from_text", role_must_not_be_parsed)
    current_requester.set(RequesterContext(mattermost_user_id="mm-learner", username="learner"))
    result = asyncio.run(back_office_tools.assign_role.ainvoke({"person": "@x", "role": "emperor", "cohort": "3"}))
    assert result == REFUSED


def test_context_hints_do_not_decide_authority(monkeypatch):
    """A forged ``is_superadmin`` hint on the context changes nothing: the stored row decides."""

    async def stored_row_is_not_superadmin(_mattermost_user_id, session=None):
        return User(id=9, mattermost_user_id="mm-learner", username="learner", is_superadmin=False)

    async def must_not_run(*_args, **_kwargs):
        raise AssertionError("cohort lookup happened despite the refusal")

    monkeypatch.setattr(authorisation.identity_repo, "get_user_by_mattermost_id", stored_row_is_not_superadmin)
    monkeypatch.setattr(back_office.cohort_repo, "get_cohort_by_name", must_not_run)
    current_requester.set(RequesterContext(mattermost_user_id="mm-learner", username="learner", is_superadmin=True))
    assert asyncio.run(back_office_tools.create_cohort.ainvoke({"name": "Forged"})) == REFUSED


def test_authorised_create_cohort_reports_existing_cohort_without_writing(monkeypatch):
    existing = Cohort(id=11, name="Backend-01")
    writes: list[str] = []

    async def superadmin(_requester, *, action=""):
        return User(id=1, mattermost_user_id="mm-admin", username="admin", is_superadmin=True)

    async def by_name(_name, session=None):
        return existing

    async def create(*_args, **_kwargs):
        writes.append("create_cohort")
        return existing

    monkeypatch.setattr(back_office, "require_superadmin", superadmin)
    monkeypatch.setattr(back_office.cohort_repo, "get_cohort_by_name", by_name)
    monkeypatch.setattr(back_office.cohort_repo, "create_cohort", create)
    result = asyncio.run(back_office_tools.create_cohort.ainvoke({"name": "  backend-01 "}))
    assert result == "[COHORT_ALREADY_EXISTS] Cohort 'Backend-01' already exists (id 11); nothing was changed."
    assert writes == []


def test_open_sprint_rejects_an_empty_name_after_authorisation(monkeypatch):
    cohort = Cohort(id=3, name="Backend-01", is_active=True)
    actor = User(id=1, mattermost_user_id="mm-sm", username="sm")

    async def resolve(_reference, session=None):
        return cohort

    async def allow(_requester, _cohort_id, **_kwargs):
        return AuthorisationDecision(True, "cohort_role:scrum_master", "scrum_master", actor)

    monkeypatch.setattr(back_office.cohort_repo, "resolve_cohort", resolve)
    monkeypatch.setattr(back_office, "require_cohort_authority", allow)
    current_requester.set(RequesterContext(mattermost_user_id="mm-sm", username="sm"))
    result = asyncio.run(back_office_tools.open_sprint.ainvoke({"cohort": "3", "sprint_name": "   "}))
    assert result == "[VALIDATION_ERROR] A sprint needs a name."
