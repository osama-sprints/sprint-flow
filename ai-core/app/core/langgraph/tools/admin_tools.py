from langchain_core.tools import tool
from langchain_core.runnables import RunnableConfig
from pydantic import BaseModel, Field
from app.services.admin_service import AdminService, AuthorisationRefusalError

admin_service = AdminService()


class CreateCohortInput(BaseModel):
    cohort_name: str = Field(description="Display name of the cohort")
    cohort_id: str = Field(description="Unique identifier for the cohort")


class AssignRoleInput(BaseModel):
    target_user_id: str = Field(description="ID of the user receiving the role")
    role: str = Field(description="Role to assign (e.g. mentor, learner, admin)")
    cohort_id: str = Field(description="Target cohort ID for role scope")


class OpenSprintInput(BaseModel):
    cohort_id: str = Field(description="Target cohort ID")
    sprint_id: str = Field(description="Sprint identifier to open")


def _get_requester_id(config: RunnableConfig) -> str:
    """Extracts requester identity out-of-band from injected RunnableConfig context."""
    configurable = config.get("configurable", {})
    requester_id = configurable.get("requester_id")
    if not requester_id:
        raise ValueError("Security Context Fault: Missing authenticated requester_id in execution context.")
    return requester_id


@tool(args_schema=CreateCohortInput)
def create_cohort_tool(cohort_name: str, cohort_id: str, config: RunnableConfig) -> str:
    """Creates a new cohort. Requires administrative permissions."""
    try:
        requester_id = _get_requester_id(config)
        res = admin_service.create_cohort(requester_id=requester_id, cohort_name=cohort_name, cohort_id=cohort_id)
        return f"SUCCESS: Cohort '{cohort_id}' processed ({res['action']})."
    except AuthorisationRefusalError as err:
        return f"REFUSAL_DETERMINISTIC: {str(err)}"


@tool(args_schema=AssignRoleInput)
def assign_role_tool(target_user_id: str, role: str, cohort_id: str, config: RunnableConfig) -> str:
    """Assigns a user role within a specific cohort."""
    try:
        requester_id = _get_requester_id(config)
        res = admin_service.assign_role(
            requester_id=requester_id, target_user_id=target_user_id, role=role, cohort_id=cohort_id
        )
        return f"SUCCESS: Role '{role}' for user '{target_user_id}' in cohort '{cohort_id}' processed ({res['action']})."
    except AuthorisationRefusalError as err:
        return f"REFUSAL_DETERMINISTIC: {str(err)}"


@tool(args_schema=OpenSprintInput)
def open_sprint_tool(cohort_id: str, sprint_id: str, config: RunnableConfig) -> str:
    """Opens a sprint for a specified cohort."""
    try:
        requester_id = _get_requester_id(config)
        res = admin_service.open_sprint(requester_id=requester_id, cohort_id=cohort_id, sprint_id=sprint_id)
        return f"SUCCESS: Sprint '{sprint_id}' for cohort '{cohort_id}' processed ({res['action']})."
    except AuthorisationRefusalError as err:
        return f"REFUSAL_DETERMINISTIC: {str(err)}"