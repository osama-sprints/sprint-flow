import re
from typing import Sequence

from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.tools.base import BaseTool
from langgraph.graph import END
from langgraph.types import Command

from app.core.requester import current_requester
from app.core.logging import logger
from app.services.escalation import EscalationType, open_escalation
from app.services.policy_retrieval import get_grounded_answer_or_refusal
from app.schemas import GraphState
from app.core.langgraph.tools.ceremonies import TOOLS as CEREMONY_TOOLS


def bind_meeting_tools(tool_group: Sequence[BaseTool]) -> list[BaseTool]:
    """Keep ceremony tools available to the meeting specialist and its executor."""
    bound = list(tool_group)
    names = {tool.name for tool in bound}
    bound.extend(tool for tool in CEREMONY_TOOLS if tool.name not in names)
    return bound


def _extract_last_text(messages: list) -> str:
    if not messages:
        return ""
    last_msg = messages[-1]
    if isinstance(last_msg, BaseMessage):
        return str(last_msg.content)
    elif isinstance(last_msg, dict):
        return str(last_msg.get("content", ""))
    elif isinstance(last_msg, str):
        return last_msg

    return str(last_msg)


def _strip_leading_mentions(text: str) -> str:
    return re.sub(r"^(?:\s*@[\w.-]+[,:]?\s*)+", "", text).strip()


async def policy_retrieval_node(state: GraphState) -> Command:
    if isinstance(state, dict):
        messages = state.get("messages", [])
    else:
        messages = getattr(state, "messages", [])
    last_message_text = _strip_leading_mentions(_extract_last_text(messages))

    requester = current_requester.get()

    has_authority = bool(requester and (requester.is_superadmin or requester.has_any_channel_authority()))
    if has_authority:
        audience = None
    else:
        audience = "learner"

    user_role = ""
    if requester:
        if requester.is_superadmin:
            user_role = "superadmin"
        else:
            channel_roles = getattr(requester, "channel_roles", {}) or {}
            channel_id = getattr(requester, "channel_id", "") or ""
            user_role = channel_roles.get(channel_id, "")
    user_role = user_role or "learner"
    if not audience and user_role == "learner":
        audience = "learner"

    logger.info(
        "policy_retrieval_request",
        query=last_message_text,
        audience=audience,
        user_role=user_role,
        requester_user_id=getattr(requester, "mattermost_user_id", None) if requester else None,
        requester_username=getattr(requester, "username", None) if requester else None,
        channel_id=getattr(requester, "channel_id", None) if requester else None,
    )

    status, docs = await get_grounded_answer_or_refusal(query=last_message_text, audience=audience)

    if status == "grounded":
        return Command(
            update={"policy_context": docs},
            goto="policy_support",
        )
    else:
        ticket_result = await open_escalation(
            question=last_message_text,
            ticket_type=EscalationType.OPS,
            requester=requester,
        )
        ai_message = AIMessage(content=ticket_result.message)

        return Command(update={"messages": [ai_message]}, goto=END)
