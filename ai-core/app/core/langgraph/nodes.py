from langgraph.types import Command
from langgraph.graph import END
from app.services.policy_retrieval import get_grounded_answer_or_refusal
from enum import Enum
from typing import Any, Dict

class EscalationType(str, Enum):
    OPS = "ops"
    LEARNER = "learner"

async def open_escalation(question: str, ticket_type: EscalationType, reason: str) -> Dict[str, Any]:
    return {"ticket_id": "ESC-12345", "status": "created"}

async def policy_retrieval_node(state : dict)->Command:
    messages = state.get("messages", [])
    last_message = messages[-1] if messages else ""
    requestor = state.get("current_requestor")
    role = getattr(requestor, "role","learner") if requestor else "learner"
    audience = "learner" if role == "learner" else "internal_operator"
    status, docs = await get_grounded_answer_or_refusal(
        query=last_message, 
        audience=audience
    )
    if status == "grounded":
        context_str = "\n".join([
            f"[Source: {d.get('document_id')}, §{d.get('section_title')}, p.{d.get('page_number')}] {d.get('text')}"
            for d in docs
        ])
        return Command(
            update={"policy_context": context_str},
            goto="policy_support_llm_node"
        )
    else:
        ticket_result = await open_escalation(
            question=last_message,
            ticket_type=EscalationType.OPS,
            reason=status
        )
        ticket_id = ticket_result.get("ticket_id", "N/A")
        msg = f"I couldn't find a definitive policy answer in our documents. An escalation ticket has been created on your behalf (Ticket ID: {ticket_id})."
        return Command(
            update={"messages": [msg]},
            goto=END
        )