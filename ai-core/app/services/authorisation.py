"""The one authorisation implementation every privileged action uses.

Properties this module guarantees:

- Decisions are made in code from **stored data** (``users.is_superadmin`` and
  ``cohort_memberships``), read from the database at decision time. Nothing a
  person wrote, and nothing the model believes, participates.
- Identity reaches the check through the ``current_requester`` ContextVar,
  which only the conversation layer writes. No tool accepts it as an argument.
- A refusal is one fixed sentence (``REFUSAL_MESSAGE``) so the agent relays it
  rather than reinterpreting it, and it is a different exception from a
  validation failure so "you may not" and "that does not exist" stay distinct.
- Authority is cohort-scoped: a role in one cohort grants nothing in another.
  The only global authority is the superadmin flag.

Callers raise before any mutation; the exceptions carry a machine-readable
``reason`` for logs and a user-facing message for the tool boundary.
"""

from typing import (
    Iterable,
    NamedTuple,
)

from app.core.logging import logger
from app.core.requester import (
    RequesterContext,
    current_requester,
)
from app.models import (
    Cohort,
    User,
)
from app.models.enums import (
    COHORT_ADMIN_ROLES,
    RoleKey,
)
from app.services.domain import cohorts as cohort_repo
from app.services.domain import identity as identity_repo

REFUSAL_MESSAGE = "Refused: You do not have permission to execute this action."


class AuthorisationRefused(Exception):
    """The requester may not do this. ``str(exc)`` is always ``REFUSAL_MESSAGE``."""

    def __init__(self, reason: str, *, action: str = "", cohort_id: int | None = None) -> None:
        """Build the refusal.

        Args:
            reason: Machine-readable cause for logs (never shown to the requester).
            action: What was attempted.
            cohort_id: The cohort in scope, if any.
        """
        super().__init__(REFUSAL_MESSAGE)
        self.reason = reason
        self.action = action
        self.cohort_id = cohort_id


class ValidationFailed(Exception):
    """The request itself is wrong (unknown role, missing cohort, bad date). ``str(exc)`` is the user-facing sentence."""


class AuthorisationDecision(NamedTuple):
    """Outcome of an authorisation check, useful for logging why something was allowed."""

    allowed: bool
    reason: str
    role_key: str | None
    user: User | None


def require_requester() -> RequesterContext:
    """Return the current turn's requester or refuse.

    Returns:
        RequesterContext: The bound context.

    Raises:
        AuthorisationRefused: When no requester is bound (a tool invoked outside a turn).
    """
    requester = current_requester.get()
    if requester is None:
        logger.warning("authorisation_refused_no_requester")
        raise AuthorisationRefused("no_requester_bound")
    return requester


async def require_requester_user(requester: RequesterContext | None = None, *, action: str = "") -> User:
    """Resolve the requester to their stored record or refuse.

    Args:
        requester: The context; defaults to the bound one.
        action: What is being attempted, for logs.

    Returns:
        User: The stored row (fresh read).

    Raises:
        AuthorisationRefused: When the requester was never synced into ``users``.
    """
    context = requester or require_requester()
    user = await identity_repo.get_user_by_mattermost_id(context.mattermost_user_id)
    if user is None:
        logger.warning(
            "authorisation_refused_unknown_user", mattermost_user_id=context.mattermost_user_id, action=action
        )
        raise AuthorisationRefused("requester_not_synced", action=action)
    return user


async def require_superadmin(requester: RequesterContext | None = None, *, action: str = "") -> User:
    """Allow platform-level actions only for stored superadmins.

    Args:
        requester: The context; defaults to the bound one.
        action: What is being attempted, for logs.

    Returns:
        User: The superadmin's stored row.

    Raises:
        AuthorisationRefused: Otherwise.
    """
    user = await require_requester_user(requester, action=action)
    if not user.is_superadmin:
        logger.warning("authorisation_refused_not_superadmin", user_id=user.id, action=action)
        raise AuthorisationRefused("not_superadmin", action=action)
    logger.info("authorisation_granted", user_id=user.id, action=action, reason="superadmin")
    return user


async def decide_cohort_authority(
    requester: RequesterContext | None,
    cohort_id: int,
    *,
    allowed_roles: Iterable[RoleKey] = COHORT_ADMIN_ROLES,
    action: str = "",
) -> AuthorisationDecision:
    """Decide, from stored data, whether the requester may act on a cohort.

    Args:
        requester: The context; defaults to the bound one.
        cohort_id: The cohort in scope.
        allowed_roles: Roles that confer the authority (default: the cohort admin roles).
        action: What is being attempted, for logs.

    Returns:
        AuthorisationDecision: Allowed or not, and why.
    """
    context = requester or require_requester()
    user = await identity_repo.get_user_by_mattermost_id(context.mattermost_user_id)
    if user is None:
        return AuthorisationDecision(False, "requester_not_synced", None, None)
    if user.is_superadmin:
        return AuthorisationDecision(True, "superadmin", None, user)
    assert user.id is not None
    role = await cohort_repo.get_role_for_user_in_cohort(user.id, cohort_id, active_only=True)
    if role is None:
        return AuthorisationDecision(False, "not_a_member", None, user)
    allowed = {str(key) for key in allowed_roles}
    if role.key not in allowed:
        return AuthorisationDecision(False, f"role_not_permitted:{role.key}", role.key, user)
    return AuthorisationDecision(True, f"cohort_role:{role.key}", role.key, user)


async def require_cohort_authority(
    requester: RequesterContext | None,
    cohort_id: int,
    *,
    allowed_roles: Iterable[RoleKey] = COHORT_ADMIN_ROLES,
    action: str = "",
) -> AuthorisationDecision:
    """Refuse unless the requester may administer the cohort.

    Args:
        requester: The context; defaults to the bound one.
        cohort_id: The cohort in scope.
        allowed_roles: Roles that confer the authority.
        action: What is being attempted, for logs.

    Returns:
        AuthorisationDecision: The allowing decision (with the stored user).

    Raises:
        AuthorisationRefused: When not allowed.
    """
    decision = await decide_cohort_authority(requester, cohort_id, allowed_roles=allowed_roles, action=action)
    user_id = decision.user.id if decision.user else None
    if not decision.allowed:
        logger.warning(
            "authorisation_refused_cohort", user_id=user_id, cohort_id=cohort_id, action=action, reason=decision.reason
        )
        raise AuthorisationRefused(decision.reason, action=action, cohort_id=cohort_id)
    logger.info("authorisation_granted", user_id=user_id, cohort_id=cohort_id, action=action, reason=decision.reason)
    return decision


async def require_cohort_membership(
    requester: RequesterContext | None,
    cohort_id: int,
    *,
    action: str = "",
) -> AuthorisationDecision:
    """Refuse unless the requester belongs to the cohort (any active role) or is a superadmin.

    Reading a cohort's calendar or member list is not privileged, but it is
    scoped: a member of one cohort does not see another's.

    Args:
        requester: The context; defaults to the bound one.
        cohort_id: The cohort in scope.
        action: What is being attempted, for logs.

    Returns:
        AuthorisationDecision: The allowing decision.

    Raises:
        AuthorisationRefused: When not a member.
    """
    return await require_cohort_authority(requester, cohort_id, allowed_roles=list(RoleKey), action=action)


def require_active_cohort(cohort: Cohort) -> Cohort:
    """Reject work on a deactivated cohort with a validation failure (not a refusal).

    Args:
        cohort: The cohort.

    Returns:
        Cohort: The same cohort, when active.

    Raises:
        ValidationFailed: When the kill switch is thrown.
    """
    if not cohort.is_active:
        raise ValidationFailed(f"Cohort '{cohort.name}' is inactive; no changes can be made to it.")
    return cohort
