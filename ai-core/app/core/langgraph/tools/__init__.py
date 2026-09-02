"""LangGraph tools for enhanced language model capabilities.

This package contains custom tools that can be used with LangGraph to extend
the capabilities of language models.
"""

from langchain_core.tools.base import BaseTool

from .admin_tools import assign_role_tool, create_cohort_tool, open_sprint_tool
from .ask_human import ask_human
from .duckduckgo_search import duckduckgo_search_tool
from .mattermost_admin import (
    mattermost_add_user_to_team,
    mattermost_find_or_create_team,
    mattermost_send_welcome_dm,
)

tools: list[BaseTool] = [
    duckduckgo_search_tool,
    ask_human,
    mattermost_find_or_create_team,
    mattermost_add_user_to_team,
    mattermost_send_welcome_dm,
    create_cohort_tool,
    assign_role_tool,
    open_sprint_tool,
]