"""The one door between the assistant and the conversation around it.

The assistant asks a question about the discussion; a retrieval sub-agent
reads the messages in a context of its own and answers with findings, sources
and coverage. Neither the pages it fetched nor its intermediate steps come
back here, so a summary of two hundred messages costs the conversation a
paragraph rather than two hundred messages.

Everything that decides WHAT may be read — who is asking, which channel, which
post, where the reply will land — comes from the turn context, never from the
model's arguments. The only argument is the question.
"""

from langchain_core.tools import tool

from app.core.langgraph.tools.results import (
    ResultCode,
    guarded_tool,
    tool_result,
)
from app.core.logging import logger
from app.services import discussion
from app.services.discussion.policy import POLICY

TOO_MANY_RUNS = (
    "You have already read this discussion as much as one reply allows. "
    "Answer from what you have, and say plainly what you could not check."
)
NO_CONTEXT = "There is no conversation to read here."


@tool
@guarded_tool
async def read_discussion(question: str) -> str:
    """Read the surrounding Mattermost conversation and answer a question about it.

    Use this whenever the request depends on what was said in the conversation
    rather than on what you know: "summarise the discussion above", "what did we
    agree?", "who was going to do that?", "what did she mean by it?", or a
    follow-up whose subject was named by somebody else. It reads the messages
    around the person's message — the channel, the thread, a referenced post, a
    search — and reports what they say, who said it, and links to them.

    Do not use it for a question that stands on its own, or for anything you
    have already been told in this conversation.

    Ask for exactly what you need. A narrow question ("what did the team decide
    about the release date?") is answered from fewer messages, and more
    accurately, than "summarise everything".

    Args:
        question: What you need to know about the discussion, in the language of
            the person's message.

    Returns:
        str: Findings, the messages they rest on with permalinks, and an honest
        account of what was and was not read.
    """
    turn = discussion.current_discussion.get()
    if turn is None:
        return tool_result(ResultCode.DISCUSSION_UNAVAILABLE, NO_CONTEXT)
    if turn.runs >= POLICY.max_runs_per_turn:
        logger.info("discussion_run_budget_spent", session_id=turn.session_id, runs=turn.runs)
        return tool_result(ResultCode.DISCUSSION_BUDGET, TOO_MANY_RUNS)

    digest = await discussion.investigate(question, turn=turn)
    if digest.empty:
        return tool_result(
            ResultCode.DISCUSSION_EMPTY,
            "Nothing in the messages around this one answers that. "
            + (digest.coverage or "")
            + (" " + " ".join(digest.refusals) if digest.refusals else ""),
        )
    return tool_result(ResultCode.DISCUSSION_READ, digest.render())


DISCUSSION_TOOLS = [read_discussion]

__all__ = ["DISCUSSION_TOOLS", "read_discussion"]
