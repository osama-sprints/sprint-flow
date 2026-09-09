from langchain_core.messages import AIMessage, BaseMessage
from langgraph.graph import END
from langgraph.types import Command

from app.core.requester import current_requester
from app.services.escalation import EscalationType, open_escalation
from app.services.policy_retrieval import get_grounded_answer_or_refusal


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


async def policy_retrieval_node(state: dict) -> Command:
    messages = state.get("messages", [])
    last_message_text = _extract_last_text(messages)

    requester = current_requester.get()

    if requester and (
        requester.is_superadmin or requester.has_any_cohort_authority()
    ):
        audience = None 
    else:
        audience = "learner"

    status, docs = await get_grounded_answer_or_refusal(
        query=last_message_text, audience=audience
    )

    if status == "grounded":
        context_str = "\n".join([
            f"[Source: {d.get('document_id')}, §{d.get('section_title')}, p.{d.get('page_number')}] {d.get('content')}"
            for d in docs
        ])
        return Command(
            update={"policy_context": context_str},
            goto="policy_support_llm_node",
        )
    else:
        ticket_result = await open_escalation(
            question=last_message_text,
            ticket_type=EscalationType.OPS,
            requester=requester,
        )
        ai_message = AIMessage(content=ticket_result.message)

        return Command(update={"messages": [ai_message]}, goto=END)