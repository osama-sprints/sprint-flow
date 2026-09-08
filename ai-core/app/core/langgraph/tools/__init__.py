"""LangGraph tools and the capability groups each specialist may use.

``tools`` is the union bound for the tool executor's name lookup;
``TOOL_GROUPS`` is what genuinely constrains each specialisation: a
specialist's model only ever sees its group, and its executor only ever runs
tools from its group. Adding a tool to the wrong group widens what a route can
do, so keep the groups honest — the orchestration probe asserts that the
learner group holds no mutating tool and the back-office group no web search
or workspace-administration tool.

Every privileged tool re-checks authorisation in code at call time from the
``current_requester`` ContextVar; group membership is capability scoping, not
the security boundary.
"""

from langchain_core.tools.base import BaseTool
from .escalation import escalate_to_human
from .ask_human import ask_human
from .back_office import TOOLS as BACK_OFFICE_ADMIN_TOOLS
from .back_office import (
    list_cohort_members,
    list_cohorts,
)
from .ceremonies import TOOLS as CEREMONY_TOOLS
from .ceremonies import list_ceremonies
from .duckduckgo_search import duckduckgo_search_tool
from .mattermost_admin import (
    mattermost_add_user_to_team,
    mattermost_find_or_create_team,
    mattermost_send_welcome_dm,
)

# The general route keeps exactly the pre-Sprint-1 tool set, so falling back to
# it is falling back to the behaviour that already worked.
GENERAL_TOOLS: list[BaseTool] = [
    duckduckgo_search_tool,
    ask_human,
    mattermost_find_or_create_team,
    mattermost_add_user_to_team,
    mattermost_send_welcome_dm,
]

# Learner-facing support: clarification, web search and READ-ONLY cohort
# tools (the calendar, the person's own cohorts, their cohort's roster). No
# mutations; each read tool refuses non-members in code.
LEARNER_SUPPORT_TOOLS: list[BaseTool] = [
    ask_human,
    duckduckgo_search_tool,
    list_ceremonies,
    list_cohorts,
    list_cohort_members,
    escalate_to_human, 
]

# Back office: cohort, role, sprint administration (s1e2) and ceremony
# scheduling (s1e4). Every tool here checks stored authority in code.
BACK_OFFICE_TOOLS: list[BaseTool] = [
    ask_human,
    *BACK_OFFICE_ADMIN_TOOLS,
    *CEREMONY_TOOLS,
]

TOOL_GROUPS: dict[str, list[BaseTool]] = {
    "general": GENERAL_TOOLS,
    "learner_support": LEARNER_SUPPORT_TOOLS,
    "back_office": BACK_OFFICE_TOOLS,
}


def _union(*groups: list[BaseTool]) -> list[BaseTool]:
    seen: set[str] = set()
    ordered: list[BaseTool] = []
    for group in groups:
        for candidate in group:
            if candidate.name in seen:
                continue
            seen.add(candidate.name)
            ordered.append(candidate)
    return ordered


tools: list[BaseTool] = _union(GENERAL_TOOLS, LEARNER_SUPPORT_TOOLS, BACK_OFFICE_TOOLS)

__all__ = [
    "BACK_OFFICE_TOOLS",
    "GENERAL_TOOLS",
    "LEARNER_SUPPORT_TOOLS",
    "TOOL_GROUPS",
    "tools",
]
