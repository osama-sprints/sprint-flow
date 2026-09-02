"""Administrative core domain service providing deterministic authorisation and idempotent operations."""

from typing import Any, Dict, Optional
from app.services.database import database_service

VALID_ROLES = {"admin", "mentor", "learner"}


class CustomAuthorisationError(Exception):
    """Base exception for deterministic authorisation failures."""

    pass


class AuthorisationRefusalError(CustomAuthorisationError):
    """Raised when requester identity lacks necessary permissions."""

    def __init__(self, requester_id: str, action: str, cohort_id: Optional[str] = None):
        self.requester_id = requester_id
        self.action = action
        self.cohort_id = cohort_id
        message = (
            f"AUTHORISATION_REFUSAL: Requester '{requester_id}' is not authorised "
            f"to perform '{action}'"
            + (f" on cohort '{cohort_id}'." if cohort_id else ".")
        )
        super().__init__(message)


class ValidationError(Exception):
    """Raised when request payload or role definition fails validation rules."""

    pass


class AdminService:
    def __init__(self, db=database_service):
        self.db = db

    def evaluate_permission(
        self, requester_id: str, required_role: str, cohort_id: Optional[str] = None
    ) -> None:
        """
        Deterministically evaluates requester permissions against stored DB records.
        Bypasses LLM reasoning completely.
        """
        if not requester_id or not requester_id.strip():
            raise AuthorisationRefusalError(
                requester_id="anonymous",
                action=f"access_role:{required_role}",
                cohort_id=cohort_id,
            )

        user_roles = self.db.get_user_roles(requester_id=requester_id, cohort_id=cohort_id)

        # Global admin bypass
        if "admin" in user_roles.get("global", []):
            return

        # Cohort-specific role match
        if cohort_id and required_role in user_roles.get("cohort_roles", {}).get(cohort_id, []):
            return

        raise AuthorisationRefusalError(
            requester_id=requester_id,
            action=f"access_role:{required_role}",
            cohort_id=cohort_id,
        )

    def create_cohort(
        self, requester_id: str, cohort_name: str, cohort_id: str
    ) -> Dict[str, Any]:
        """Creates a cohort idempotently."""
        if not cohort_id or not cohort_name:
            raise ValidationError("Cohort ID and Cohort Name cannot be empty.")

        self.evaluate_permission(requester_id, required_role="admin")

        existing = self.db.get_cohort(cohort_id=cohort_id)
        if existing:
            return {
                "status": "success",
                "action": "noop",
                "cohort": existing,
                "message": f"Cohort '{cohort_id}' already exists",
            }

        cohort = self.db.create_cohort(cohort_id=cohort_id, name=cohort_name)
        return {
            "status": "success",
            "action": "created",
            "cohort": cohort,
            "message": f"Cohort '{cohort_id}' successfully created",
        }

    def assign_role(
        self, requester_id: str, target_user_id: str, role: str, cohort_id: str
    ) -> Dict[str, Any]:
        """Assigns a role within a cohort deterministically and idempotently."""
        if role not in VALID_ROLES:
            raise ValidationError(
                f"Invalid role '{role}'. Valid roles are: {', '.join(sorted(VALID_ROLES))}"
            )

        if not target_user_id or not cohort_id:
            raise ValidationError("Target User ID and Cohort ID are required.")

        self.evaluate_permission(
            requester_id, required_role="admin", cohort_id=cohort_id
        )

        already_assigned = self.db.check_user_has_role(
            user_id=target_user_id, role=role, cohort_id=cohort_id
        )
        if already_assigned:
            return {
                "status": "success",
                "action": "noop",
                "message": f"User '{target_user_id}' already has role '{role}' in cohort '{cohort_id}'",
            }

        result = self.db.add_user_role(
            user_id=target_user_id, role=role, cohort_id=cohort_id
        )
        return {
            "status": "success",
            "action": "assigned",
            "result": result,
            "message": f"Role '{role}' assigned to '{target_user_id}' in cohort '{cohort_id}'",
        }

    def open_sprint(
        self, requester_id: str, cohort_id: str, sprint_id: str
    ) -> Dict[str, Any]:
        """Opens a sprint for a cohort idempotently."""
        if not cohort_id or not sprint_id:
            raise ValidationError("Cohort ID and Sprint ID are required.")

        self.evaluate_permission(
            requester_id, required_role="admin", cohort_id=cohort_id
        )

        sprint_status = self.db.get_sprint_status(cohort_id=cohort_id, sprint_id=sprint_id)
        if sprint_status == "OPEN":
            return {
                "status": "success",
                "action": "noop",
                "message": f"Sprint '{sprint_id}' is already open for cohort '{cohort_id}'",
            }

        sprint = self.db.set_sprint_status(
            cohort_id=cohort_id, sprint_id=sprint_id, status="OPEN"
        )
        return {
            "status": "success",
            "action": "opened",
            "sprint": sprint,
            "message": f"Sprint '{sprint_id}' opened for cohort '{cohort_id}'",
        }