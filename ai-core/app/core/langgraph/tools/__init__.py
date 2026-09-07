"""LangGraph tools and the capability groups each specialist may use.

``tools`` is the union bound for the tool executor's name lookup;
``TOOL_GROUPS`` is what genuinely constrains each specialisation: a
specialist's model only ever sees its group, and its executor only ever runs
tools from its group. Adding a tool to the wrong group widens what a route can
do, so keep the groups honest — the orchestration probe asserts that the
learner group holds no mutating tool and the back-office group no web search
or workspace-administration tool.

The rich-media tools are the deliberate exception to "groups differ": every
group holds them. They stage visual output onto the turn's reply envelope and
write nothing — no Mattermost call, no database write — so sharing them widens
what a reply can LOOK like without widening what any specialist can DO. The
attachment tools are shared for the same reason: they read back files the
person themselves put into this conversation, and nothing else. ``read_discussion``
is shared on the same grounds and one more: a request that needs the discussion
AND a document, or the discussion AND the cohort calendar, is one request, and
routing it away from its data to reach the conversation would answer half of it.
It reads only what the requester may already read, checked in code, and writes
nothing.

Every privileged tool re-checks authorisation in code at call time from the
``current_requester`` ContextVar; group membership is capability scoping, not
the security boundary.
"""

from langchain_core.tools.base import BaseTool

from .ask_human import ask_human
from .attachments import ATTACHMENT_TOOLS as _ATTACHMENT_READ_TOOLS
from .back_office import TOOLS as BACK_OFFICE_ADMIN_TOOLS
from .back_office import (
    list_cohort_members,
    list_cohorts,
)
from .ceremonies import TOOLS as CEREMONY_TOOLS
from .ceremonies import list_ceremonies
from .discussion import DISCUSSION_TOOLS
from .duckduckgo_search import duckduckgo_search_tool
from .mattermost_admin import (
    mattermost_add_user_to_team,
    mattermost_find_or_create_team,
    mattermost_send_welcome_dm,
)
from .pdf import PDF_TOOLS
from .rich_media import RICH_MEDIA_TOOLS

# Reading back what the person attached: stored text, and PDFs page by page.
ATTACHMENT_TOOLS: list[BaseTool] = [*_ATTACHMENT_READ_TOOLS, *PDF_TOOLS]

# Reading the conversation the message arrived in. Held by every group.
CONTEXT_TOOLS: list[BaseTool] = list(DISCUSSION_TOOLS)

# The general route keeps exactly the pre-Sprint-1 tool set, so falling back to
# it is falling back to the behaviour that already worked.
GENERAL_TOOLS: list[BaseTool] = [
    duckduckgo_search_tool,
    ask_human,
    mattermost_find_or_create_team,
    mattermost_add_user_to_team,
    mattermost_send_welcome_dm,
    *RICH_MEDIA_TOOLS,
    *ATTACHMENT_TOOLS,
    *CONTEXT_TOOLS,
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
    *RICH_MEDIA_TOOLS,
    *ATTACHMENT_TOOLS,
    *CONTEXT_TOOLS,
]

# Back office: cohort, role, sprint administration (s1e2) and ceremony
# scheduling (s1e4). Every tool here checks stored authority in code.
BACK_OFFICE_TOOLS: list[BaseTool] = [
    ask_human,
    *BACK_OFFICE_ADMIN_TOOLS,
    *CEREMONY_TOOLS,
    *RICH_MEDIA_TOOLS,
    *ATTACHMENT_TOOLS,
    *CONTEXT_TOOLS,
]

# The composition specialist: rendering, plus the clarification tool. It holds
# no business tool at all, so routing a turn here can never reach a mutation.
RICH_MEDIA_ONLY_TOOLS: list[BaseTool] = [
    ask_human,
    *RICH_MEDIA_TOOLS,
    *ATTACHMENT_TOOLS,
    *CONTEXT_TOOLS,
]

# Grounding a reply in what people said. Retrieval and the attachment tools,
# so a request that spans the discussion AND a document is answered whole — but
# deliberately NO rendering tools. This specialist's first call is forced, and
# with a drawing tool in reach a model asked to "summarise the discussion as a
# mind map" drew the map and never read the discussion. Where a visual is
# wanted the supervisor plans rich_media after this route, which is the
# specialist that owns drawing anyway.
CONVERSATION_CONTEXT_TOOLS: list[BaseTool] = [
    ask_human,
    *CONTEXT_TOOLS,
    *ATTACHMENT_TOOLS,
]

TOOL_GROUPS: dict[str, list[BaseTool]] = {
    "general": GENERAL_TOOLS,
    "learner_support": LEARNER_SUPPORT_TOOLS,
    "back_office": BACK_OFFICE_TOOLS,
    "rich_media": RICH_MEDIA_ONLY_TOOLS,
    "conversation_context": CONVERSATION_CONTEXT_TOOLS,
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


tools: list[BaseTool] = _union(
    GENERAL_TOOLS,
    LEARNER_SUPPORT_TOOLS,
    BACK_OFFICE_TOOLS,
    RICH_MEDIA_ONLY_TOOLS,
    CONVERSATION_CONTEXT_TOOLS,
)

__all__ = [
    "ATTACHMENT_TOOLS",
    "BACK_OFFICE_TOOLS",
    "CONTEXT_TOOLS",
    "CONVERSATION_CONTEXT_TOOLS",
    "GENERAL_TOOLS",
    "LEARNER_SUPPORT_TOOLS",
    "RICH_MEDIA_ONLY_TOOLS",
    "RICH_MEDIA_TOOLS",
    "TOOL_GROUPS",
    "tools",
]
