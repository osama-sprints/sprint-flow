import pytest
from unittest.mock import MagicMock, patch
from langchain_core.runnables import RunnableConfig

from app.services.admin_service import AdminService, AuthorisationRefusalError
from app.core.langgraph.tools.admin_tools import (
    create_cohort_tool,
    assign_role_tool,
    open_sprint_tool,
    _get_requester_id,
)


@pytest.fixture
def mock_db():
    db = MagicMock()
    return db


@pytest.fixture
def admin_service(mock_db):
    return AdminService(db=mock_db)


# ============================================================================
# 1. POSITIVE EXECUTION & IDEMPOTENCY TESTS
# ============================================================================

def test_create_cohort_success_and_idempotency(admin_service, mock_db):
    requester_id = "user_admin_123"
    mock_db.get_user_roles.return_value = {"global": ["admin"]}

    # First call: Cohort created
    mock_db.get_cohort.return_value = None
    mock_db.create_cohort.return_value = {"id": "c101", "name": "Cohort 101"}
    
    res1 = admin_service.create_cohort(requester_id, "Cohort 101", "c101")
    assert res1["status"] == "success"
    assert res1["action"] == "created"
    mock_db.create_cohort.assert_called_once_with(cohort_id="c101", name="Cohort 101")

    # Second call: Idempotent no-op
    mock_db.get_cohort.return_value = {"id": "c101", "name": "Cohort 101"}
    res2 = admin_service.create_cohort(requester_id, "Cohort 101", "c101")
    assert res2["status"] == "success"
    assert res2["action"] == "noop"


def test_assign_role_idempotency(admin_service, mock_db):
    requester_id = "user_admin_123"
    mock_db.get_user_roles.return_value = {"global": ["admin"]}

    # First call: Role assigned
    mock_db.check_user_has_role.return_value = False
    mock_db.add_user_role.return_value = {"user_id": "u456", "role": "learner", "cohort_id": "c101"}
    
    res1 = admin_service.assign_role(requester_id, "u456", "learner", "c101")
    assert res1["action"] == "assigned"

    # Second call: Already assigned (no duplicate database records)
    mock_db.check_user_has_role.return_value = True
    res2 = admin_service.assign_role(requester_id, "u456", "learner", "c101")
    assert res2["action"] == "noop"


def test_open_sprint_idempotency(admin_service, mock_db):
    requester_id = "user_admin_123"
    mock_db.get_user_roles.return_value = {"global": ["admin"]}

    # First call: Open sprint
    mock_db.get_sprint_status.return_value = "CLOSED"
    mock_db.set_sprint_status.return_value = {"sprint_id": "spt_1", "status": "OPEN"}
    
    res1 = admin_service.open_sprint(requester_id, "c101", "spt_1")
    assert res1["action"] == "opened"

    # Second call: Sprint already open
    mock_db.get_sprint_status.return_value = "OPEN"
    res2 = admin_service.open_sprint(requester_id, "c101", "spt_1")
    assert res2["action"] == "noop"


# ============================================================================
# 2. NEGATIVE REFUSAL PATHS & COHORT ISOLATION
# ============================================================================

def test_unauthorized_learner_refusal_zero_side_effects(admin_service, mock_db):
    unauthorized_user = "user_learner_999"
    mock_db.get_user_roles.return_value = {"global": ["learner"], "cohort_roles": {}}

    with pytest.raises(AuthorisationRefusalError) as exc_info:
        admin_service.assign_role(
            requester_id=unauthorized_user,
            target_user_id="user_learner_999",
            role="admin",
            cohort_id="c101"
        )

    assert "AUTHORISATION_REFUSAL" in str(exc_info.value)
    # Ensure zero mutation calls were executed against the data layer
    mock_db.add_user_role.assert_not_called()


def test_cohort_isolation_permissions(admin_service, mock_db):
    requester_id = "user_mentor_777"
    mock_db.get_user_roles.return_value = {
        "global": [],
        "cohort_roles": {"cohort_A": ["admin"]}
    }

    # Action in cohort_A succeeds
    mock_db.get_sprint_status.return_value = "CLOSED"
    res = admin_service.open_sprint(requester_id, cohort_id="cohort_A", sprint_id="s1")
    assert res["status"] == "success"

    # Action in cohort_B is refused strictly
    with pytest.raises(AuthorisationRefusalError):
        admin_service.open_sprint(requester_id, cohort_id="cohort_B", sprint_id="s1")


# ============================================================================
# 3. PROMPT INJECTION RESILIENCE & OUT-OF-BAND IDENTITY BINDING
# ============================================================================

def test_missing_injected_identity_raises_security_fault():
    config_empty: RunnableConfig = {"configurable": {}}
    
    with pytest.raises(ValueError) as exc_info:
        _get_requester_id(config_empty)
    assert "Security Context Fault" in str(exc_info.value)


@patch("app.core.langgraph.tools.admin_tools.admin_service")
def test_prompt_injection_cannot_override_injected_requester(mock_admin_service):
    mock_admin_service.assign_role.side_effect = AuthorisationRefusalError(
        requester_id="unauthorized_attacker", action="access_role:admin", cohort_id="c101"
    )

    config: RunnableConfig = {"configurable": {"requester_id": "unauthorized_attacker"}}

    tool_result = assign_role_tool.invoke(
        input={
            "target_user_id": "unauthorized_attacker",
            "role": "admin",
            "cohort_id": "c101",
        },
        config=config
    )

    assert "REFUSAL_DETERMINISTIC" in tool_result
    assert "unauthorized_attacker" in tool_result

    mock_admin_service.assign_role.assert_called_once_with(
        requester_id="unauthorized_attacker",
        target_user_id="unauthorized_attacker",
        role="admin",
        cohort_id="c101"
    )