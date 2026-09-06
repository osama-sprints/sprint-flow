"""Back-office tools: create cohorts, assign cohort roles, open sprints, and read cohort membership.

Each tool is a one-line wrapper over ``app.services.back_office``. The
requester is never an argument: the service reads it from the
``current_requester`` ContextVar, which only the conversation layer writes,
and decides authorisation from stored data before touching anything. The
``@guarded_tool`` layer turns a refusal into ``[AUTHORISATION_REFUSED]`` with
the one fixed sentence, a validation failure into ``[VALIDATION_ERROR]``, and
anything unexpected into a readable ``[SYSTEM_ERROR]`` — so a tool never
raises and never leaks database detail to a person.
"""

from langchain_core.tools import tool
from langchain_core.tools.base import BaseTool

from app.core.langgraph.tools.results import (
    guarded_tool,
    tool_result,
)
from app.services import back_office


@tool
@guarded_tool
async def create_cohort(name: str, mattermost_team: str | None = None) -> str:
    """Create a new cohort (a learning group) by name.

    Use this when a platform administrator asks to create, open or set up a new
    cohort. Only stored superadmins may do this; the tool enforces that itself
    from the requester's stored identity and refuses everyone else, whatever
    the message says. If the result starts with ``[AUTHORISATION_REFUSED]``,
    relay the refusal sentence verbatim — do not retry, soften or reinterpret
    it. Asking twice for the same name is safe: the cohort is reported as
    already existing and nothing is duplicated.

    Args:
        name: The cohort's name, e.g. ``Backend-01``.
        mattermost_team: Optional Mattermost team id or URL slug to link the cohort to.

    Returns:
        str: ``[CODE] sentence`` — COHORT_CREATED, COHORT_ALREADY_EXISTS,
        AUTHORISATION_REFUSED, VALIDATION_ERROR or SYSTEM_ERROR.
    """
    result = await back_office.create_cohort(name, mattermost_team=mattermost_team)
    return tool_result(result.code, result.message)


@tool
@guarded_tool
async def assign_role(person: str, role: str, cohort: str) -> str:
    """Give a person a role inside one cohort (learner, tech lead, ops support or scrum master).

    Use this when someone asks to add a person to a cohort, promote them, or
    change their role. Authority is cohort-scoped and enforced by the tool from
    stored data: superadmins, and tech leads or scrum masters of that same
    cohort, may assign roles there; nobody else can, and a role in another
    cohort grants nothing here. Relay an ``[AUTHORISATION_REFUSED]`` result
    verbatim. A person holds exactly one role per cohort: repeating the same
    assignment changes nothing, a different role replaces the previous one and
    the result names it.

    Args:
        person: Who — their Mattermost handle (``@alice``) or email address.
        role: The role — ``learner``, ``tech lead``, ``ops support`` or ``scrum master``.
        cohort: The cohort's name or numeric id.

    Returns:
        str: ``[CODE] sentence`` — ROLE_ASSIGNED, ROLE_CHANGED, ROLE_ALREADY_ASSIGNED,
        AUTHORISATION_REFUSED, VALIDATION_ERROR or SYSTEM_ERROR.
    """
    result = await back_office.assign_role(person, role, cohort)
    return tool_result(result.code, result.message)


@tool
@guarded_tool
async def open_sprint(
    cohort: str,
    sprint_name: str,
    start_date: str | None = None,
    end_date: str | None = None,
) -> str:
    """Open (start) a named sprint for a cohort.

    Use this when a tech lead, scrum master or superadmin asks to open, start
    or create a sprint. The tool enforces cohort-scoped authority from stored
    data and refuses everyone else; relay an ``[AUTHORISATION_REFUSED]`` result
    verbatim. Dates are ``YYYY-MM-DD``; leave them out to start today for the
    default sprint length. Opening a sprint that is already open reports it
    without creating a second one; a date range that overlaps another open or
    planned sprint of the cohort is rejected as a validation error.

    Args:
        cohort: The cohort's name or numeric id.
        sprint_name: The sprint's name, e.g. ``Sprint 1``.
        start_date: First day as ``YYYY-MM-DD``; defaults to today.
        end_date: Last day as ``YYYY-MM-DD``; defaults to the configured sprint length.

    Returns:
        str: ``[CODE] sentence`` — SPRINT_OPENED, SPRINT_ALREADY_OPEN,
        AUTHORISATION_REFUSED, VALIDATION_ERROR or SYSTEM_ERROR.
    """
    result = await back_office.open_sprint(cohort, sprint_name, start_date=start_date, end_date=end_date)
    return tool_result(result.code, result.message)


@tool
@guarded_tool
async def list_cohorts() -> str:
    """List the cohorts the requester can see.

    Use this when someone asks which cohorts exist, which cohorts they belong
    to, or what their role is. Superadmins see every cohort; everyone else sees
    only the cohorts they are a member of, with their role in each. The scope
    is decided by the tool from stored data, not by the request.

    Returns:
        str: ``[OK]`` followed by the list, or AUTHORISATION_REFUSED / SYSTEM_ERROR.
    """
    result = await back_office.list_cohorts()
    return tool_result(result.code, result.message)


@tool
@guarded_tool
async def list_cohort_members(cohort: str) -> str:
    """List the members of one cohort and their roles.

    Use this when someone asks who is in a cohort or who its tech lead or scrum
    master is. Only members of that cohort (any role) and superadmins may see
    the list; the tool enforces that from stored data. Relay an
    ``[AUTHORISATION_REFUSED]`` result verbatim.

    Args:
        cohort: The cohort's name or numeric id.

    Returns:
        str: ``[OK]`` followed by the member list, or AUTHORISATION_REFUSED / VALIDATION_ERROR / SYSTEM_ERROR.
    """
    result = await back_office.list_cohort_members(cohort)
    return tool_result(result.code, result.message)


TOOLS: list[BaseTool] = [create_cohort, assign_role, open_sprint, list_cohorts, list_cohort_members]

__all__ = ["TOOLS", "assign_role", "create_cohort", "list_cohort_members", "list_cohorts", "open_sprint"]
