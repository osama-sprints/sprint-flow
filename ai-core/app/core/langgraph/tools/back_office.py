"""Back-office tools: assign channel roles, open sprints, and read channel membership.

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
async def assign_role(person: str, role: str) -> str:
    """Give a person a role inside the current channel (learner, tech lead, ops support or scrum master).

    Use this when someone asks to add a person to the channel's roster, promote them, or
    change their role. Authority is channel-scoped and enforced by the tool from
    stored data: superadmins, and tech leads or scrum masters of the current channel, 
    may assign roles here. Relay an ``[AUTHORISATION_REFUSED]`` result
    verbatim. A person holds exactly one role per channel: repeating the same
    assignment changes nothing, a different role replaces the previous one and
    the result names it.

    Args:
        person: Who — their Mattermost handle (``@alice``) or email address.
        role: The role — ``learner``, ``tech lead``, ``ops support`` or ``scrum master``.

    Returns:
        str: ``[CODE] sentence`` — ROLE_ASSIGNED, ROLE_CHANGED, ROLE_ALREADY_ASSIGNED,
        AUTHORISATION_REFUSED, VALIDATION_ERROR or SYSTEM_ERROR.
    """
    result = await back_office.assign_role(person, role)
    return tool_result(result.code, result.message)


@tool
@guarded_tool
async def open_sprint(
    sprint_name: str,
    start_date: str | None = None,
    end_date: str | None = None,
) -> str:
    """Open (start) a named sprint for the current channel.

    Use this when a tech lead, scrum master or superadmin asks to open, start
    or create a sprint. The tool enforces channel-scoped authority from stored
    data and refuses everyone else; relay an ``[AUTHORISATION_REFUSED]`` result
    verbatim. Dates are ``YYYY-MM-DD``; leave them out to start today for the
    default sprint length. Opening a sprint that is already open reports it
    without creating a second one; a date range that overlaps another open or
    planned sprint of the channel is rejected as a validation error.

    Args:
        sprint_name: The sprint's name, e.g. ``Sprint 1``.
        start_date: First day as ``YYYY-MM-DD``; defaults to today.
        end_date: Last day as ``YYYY-MM-DD``; defaults to the configured sprint length.

    Returns:
        str: ``[CODE] sentence`` — SPRINT_OPENED, SPRINT_ALREADY_OPEN,
        AUTHORISATION_REFUSED, VALIDATION_ERROR or SYSTEM_ERROR.
    """
    result = await back_office.open_sprint(sprint_name, start_date=start_date, end_date=end_date)
    return tool_result(result.code, result.message)


@tool
@guarded_tool
async def list_channel_roles_for_requester() -> str:
    """List the channels the requester has a role in.

    Use this when someone asks which channels they have roles in, or what their role is.
    Superadmins see a notification of their status; everyone else sees
    only the channels they hold a role in.

    Returns:
        str: ``[OK]`` followed by the list, or AUTHORISATION_REFUSED / SYSTEM_ERROR.
    """
    result = await back_office.list_channel_roles_for_requester()
    return tool_result(result.code, result.message)


@tool
@guarded_tool
async def list_channel_members() -> str:
    """List the active members of the current channel and their roles.

    Use this when someone asks who has a role in the current channel or who its tech lead or scrum
    master is. Only members of that channel (any role) and superadmins may see
    the list; the tool enforces that from stored data. Relay an
    ``[AUTHORISATION_REFUSED]`` result verbatim.

    Returns:
        str: ``[OK]`` followed by the member list, or AUTHORISATION_REFUSED / VALIDATION_ERROR / SYSTEM_ERROR.
    """
    result = await back_office.list_channel_members()
    return tool_result(result.code, result.message)


TOOLS: list[BaseTool] = [assign_role, open_sprint, list_channel_roles_for_requester, list_channel_members]

__all__ = ["TOOLS", "assign_role", "list_channel_members", "list_channel_roles_for_requester", "open_sprint"]
