from typing import Any, Dict, Optional
from app.services.database import database_service


class CustomAuthorisationError(Exception):
    """Base exception for deterministic authorization failures."""
    pass


class AuthorisationRefusalError(CustomAuthorisationError):
    """Raised when requester identity lacks necessary cohort permissions."""
    def __init__(self, requester_id: str, action: str, cohort_id: Optional[str] = None):
        self.requester_id = requester_id
        self.action = action
        self.cohort_id = cohort_id
        message = (
            f"AUTHORISATION_REFUSAL: Requester '{requester_id}' is not authorized "
            f"to perform '{action}'"
            + (f" on cohort '{cohort_id}'." if cohort_id else ".")
        )
        super().__init__(message)


class AdminService:
    def __init__(self, db=database_service):
        self.db = db

    def evaluate_permission(
        self, requester_id: str, required_role: str, cohort_id: Optional[str] = None
    ) -> None:
        """
        Deterministically evaluates requester permissions against stored DB records.
        Does not rely on prompt input or model output.
        """
        user_roles = self.db.get_user_roles(requester_id=requester_id, cohort_id=cohort_id)
        
        if "admin" in user_roles.get("global", []):
            return

        if cohort_id and required_role in user_roles.get("cohort_roles", {}).get(cohort_id, []):
            return

        raise AuthorisationRefusalError(
            requester_id=requester_id,
            action=f"access_role:{required_role}",
            cohort_id=cohort_id
        )

    def create_cohort(self, requester_id: str, cohort_name: str, cohort_id: str) -> Dict[str, Any]:
        """Creates a cohort idempotently."""
        self.evaluate_permission(requester_id, required_role="admin")
        
        existing = self.db.get_cohort(cohort_id=cohort_id)
        if existing:
            return {"status": "success", "action": "noop", "cohort": existing, "message": "Cohort already exists"}

        cohort = self.db.create_cohort(cohort_id=cohort_id, name=cohort_name)
        return {"status": "success", "action": "created", "cohort": cohort}

    def assign_role(self, requester_id: str, target_user_id: str, role: str, cohort_id: str) -> Dict[str, Any]:
        """Assigns a role within a cohort deterministically and idempotently."""
        self.evaluate_permission(requester_id, required_role="admin", cohort_id=cohort_id)

        already_assigned = self.db.check_user_has_role(user_id=target_user_id, role=role, cohort_id=cohort_id)
        if already_assigned:
            return {"status": "success", "action": "noop", "message": f"User {target_user_id} already has role {role} in cohort {cohort_id}"}

        result = self.db.add_user_role(user_id=target_user_id, role=role, cohort_id=cohort_id)
        return {"status": "success", "action": "assigned", "result": result}

    def open_sprint(self, requester_id: str, cohort_id: str, sprint_id: str) -> Dict[str, Any]:
        """Opens a sprint for a cohort idempotently."""
        self.evaluate_permission(requester_id, required_role="admin", cohort_id=cohort_id)

        sprint_status = self.db.get_sprint_status(cohort_id=cohort_id, sprint_id=sprint_id)
        if sprint_status == "OPEN":
            return {"status": "success", "action": "noop", "message": f"Sprint {sprint_id} is already open for cohort {cohort_id}"}

        sprint = self.db.set_sprint_status(cohort_id=cohort_id, sprint_id=sprint_id, status="OPEN")
        return {"status": "success", "action": "opened", "sprint": sprint}