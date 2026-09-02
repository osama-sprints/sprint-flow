"""LangGraph tools wrapper for administrative capabilities."""

from typing import Any, Dict, Optional
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from pydantic import BaseModel, Field

from app.core.langgraph.tools.mattermost_admin import current_requester
from app.services.admin_service import AdminService, AuthorisationRefusalError, ValidationError

admin_service_instance = AdminService()


class CreateCohortInput(BaseModel):
    cohort_id: str = Field(description="Unique identifier for the new cohort")
    cohort_name: str = Field(description="Display name for the new cohort")


class AssignRoleInput(BaseModel):
    target_user_id: str = Field(description="User ID receiving the role assignment")
    role: str = Field(description="Role to assign (e.g., mentor, learner, admin)")
    cohort_id: str = Field(description="Cohort ID scope for the role assignment")


class OpenSprintInput(BaseModel):
    cohort_id: str = Field(description="Cohort ID associated with the sprint")
    sprint_id: str = Field(description="Unique sprint identifier to open")
    

def _get_requester_id(config: Optional[RunnableConfig] = None) -> str:
    """
    Extracts the authenticated requester identity out-of-band.
    Primary: Checks current_requester ContextVar (populated by Mattermost/API).
    Secondary: Checks RunnableConfig['configurable']['requester_id'] (for unit/integration tests).
    """
    # 1. Check Mattermost / API ContextVar first
    try:
        ctx_requester = current_requester.get(None)
        if ctx_requester:
            if isinstance(ctx_requester, dict):
                req_id = ctx_requester.get("user_id") or ctx_requester.get("id") or ctx_requester.get("username")
                if req_id:
                    return req_id
            elif isinstance(ctx_requester, str):
                return ctx_requester
    except Exception:
        pass

    # 2. Fall back to RunnableConfig context
    if config and isinstance(config, dict) and "configurable" in config:
        config_requester = config["configurable"].get("requester_id")
        if config_requester:
            return config_requester

    raise ValueError("Security Context Fault: Missing authenticated requester_id")


@tool("create_cohort", args_schema=CreateCohortInput)
def create_cohort_tool(
    cohort_id: str, cohort_name: str, config: Optional[RunnableConfig] = None
) -> str:
    """Creates a new cohort idempotently. Admin privileges required."""
    try:
        requester_id = _get_requester_id(config)
        res = admin_service_instance.create_cohort(
            requester_id=requester_id, cohort_name=cohort_name, cohort_id=cohort_id
        )
        return f"SUCCESS: {res['message']}"
    except AuthorisationRefusalError as e:
        return f"REFUSAL_DETERMINISTIC: {str(e)}"
    except ValidationError as e:
        return f"VALIDATION_ERROR: {str(e)}"
    except Exception as e:
        return f"SYSTEM_FAULT: {str(e)}"


@tool("assign_role", args_schema=AssignRoleInput)
def assign_role_tool(
    target_user_id: str, role: str, cohort_id: str, config: Optional[RunnableConfig] = None
) -> str:
    """Assigns a role to a user within a cohort idempotently. Admin privileges required."""
    try:
        requester_id = _get_requester_id(config)
        res = admin_service_instance.assign_role(
            requester_id=requester_id,
            target_user_id=target_user_id,
            role=role,
            cohort_id=cohort_id,
        )
        return f"SUCCESS: {res['message']}"
    except AuthorisationRefusalError as e:
        return f"REFUSAL_DETERMINISTIC: {str(e)}"
    except ValidationError as e:
        return f"VALIDATION_ERROR: {str(e)}"
    except Exception as e:
        return f"SYSTEM_FAULT: {str(e)}"


@tool("open_sprint", args_schema=OpenSprintInput)
def open_sprint_tool(
    cohort_id: str, sprint_id: str, config: Optional[RunnableConfig] = None
) -> str:
    """Opens a sprint for a cohort idempotently. Admin privileges required."""
    try:
        requester_id = _get_requester_id(config)
        res = admin_service_instance.open_sprint(
            requester_id=requester_id, cohort_id=cohort_id, sprint_id=sprint_id
        )
        return f"SUCCESS: {res['message']}"
    except AuthorisationRefusalError as e:
        return f"REFUSAL_DETERMINISTIC: {str(e)}"
    except ValidationError as e:
        return f"VALIDATION_ERROR: {str(e)}"
    except Exception as e:
        return f"SYSTEM_FAULT: {str(e)}"