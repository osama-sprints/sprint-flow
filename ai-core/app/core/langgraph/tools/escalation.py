"""Escalation tool: hand an ungrounded learner question to a human, quietly.

One tool, one job. It takes no ``ticket_type`` argument on purpose — which
queue a question routes to is a code-level decision
(``app.services.escalation.DEFAULT_TICKET_TYPE``), never one the model makes
by reading the question, so it is not exposed as something the model fills
in. See ``choosing_the_human`` in ``reports/escalation_report.md``.
"""

from langchain_core.tools import tool
from langchain_core.tools.base import BaseTool

from app.core.langgraph.tools.results import (
    guarded_tool,
    tool_result,
)
from app.services import escalation


@tool
@guarded_tool
async def escalate_to_human(question: str) -> str:
    """Hand a learner's question to a human when you have no grounded answer.

    Use this ONLY once you have genuinely found no supported answer for a
    policy or process question — never as a shortcut, and never to avoid a
    question you could otherwise ground. Do not name, guess or imply who will
    receive it: the tool resolves the right person itself from the cohort's
    stored role assignments and contacts them privately. Tell the learner only
    that you don't know and that a colleague has been looped in.

    If the result starts with ``[ESCALATION_OPENED_NO_HUMAN]``, relay that
    sentence as-is too — it already explains, honestly, that nobody is
    assigned yet; do not soften it into implying someone is on it.

    Args:
        question: The learner's question, verbatim.

    Returns:
        str: ``[CODE] sentence`` — ESCALATION_OPENED, ESCALATION_OPENED_NO_HUMAN,
        VALIDATION_ERROR or SYSTEM_ERROR. Relay the sentence to the learner as-is.
    """
    result = await escalation.open_escalation(question)
    return tool_result(result.code, result.message)


TOOLS: list[BaseTool] = [escalate_to_human]

__all__ = ["TOOLS", "escalate_to_human"]
