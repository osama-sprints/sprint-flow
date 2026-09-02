"""LangGraph tools for enhanced language model capabilities.

This package contains custom tools that can be used with LangGraph to extend
the capabilities of language models. Includes tools for web search, workspace
administration, authorisation back-office, and Agile ceremony scheduling.
"""

from langchain_core.tools.base import BaseTool

from .admin_tools import assign_role_tool, create_cohort_tool, open_sprint_tool
from .ask_human import ask_human
from .ceremony_scheduler import (
    amend_ceremony,
    read_ceremonies,
    schedule_ceremony,
)
from .duckduckgo_search import duckduckgo_search_tool
from .mattermost_admin import (
    mattermost_add_user_to_team,
    mattermost_find_or_create_team,
    mattermost_send_welcome_dm,
)

# The admin tools are bound for everyone, but each one re-checks authorisation
# against the ADMIN_EMAILS allowlist at call time and refuses otherwise. Binding
# them conditionally is not possible here — the model is tool-bound once at
# startup — and would be weaker anyway: the guard belongs in the tool, not in
# what the model was offered.
tools: list[BaseTool] = [
    duckduckgo_search_tool,
    ask_human,
    mattermost_find_or_create_team,
    mattermost_add_user_to_team,
    mattermost_send_welcome_dm,
    # Authorisation / back-office admin tools (Sprint-1 Authorisation task)
    create_cohort_tool,
    assign_role_tool,
    open_sprint_tool,
    # Ceremony scheduling tools (Ceremony Scheduler task)
    schedule_ceremony,
    amend_ceremony,
    read_ceremonies,
]