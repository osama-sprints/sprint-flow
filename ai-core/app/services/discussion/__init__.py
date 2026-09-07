"""Reading the human conversation a message arrives in.

The bot keeps its own history of what people said TO it. That is not the same
as the discussion it was dropped into: colleagues talk to each other, agree
things, change their minds, and then ask "so what did we settle on?" — a
question whose evidence the bot has never seen.

This package closes that gap without pulling the conversation into the bot's
context wholesale:

* ``context`` — the trusted facts a read is allowed to use, and the turn's budget.
* ``access`` — whether the REQUESTER may read a channel, and whether what is
  read may be repeated where the reply will land.
* ``retrieval`` — four anchored, paginated ways in, with honest coverage.
* ``agent`` — a bounded sub-agent that reads in its OWN context and hands back
  a digest: findings, who said what, post ids, permalinks, what was missed.

The parent graph sees the digest and nothing else.
"""

from typing import (
    Any,
    Dict,
    List,
)

from app.core.logging import logger
from app.services.discussion.access import (
    ChannelAccess,
    may_disclose,
    resolve_channel,
)
from app.services.discussion.agent import (
    Digest,
    investigate,
    prime_block,
)
from app.services.discussion.context import (
    DiscussionTurn,
    NoDiscussionContext,
    begin_turn,
    current_discussion,
    end_turn,
    require_turn,
)
from app.services.discussion.policy import POLICY
from app.services.discussion.retrieval import (
    RetrievalRefused,
    channel_messages,
    message,
    search,
    thread_messages,
)


async def prime_thread(*, first_turn: bool) -> int:
    """Read a bounded amount of a thread the bot is being drawn into.

    Being mentioned inside a thread that has been running without the bot is
    the one case where waiting for the model to ask would be wrong: the
    question ("what did we agree?") is *about* messages that are, from the
    bot's point of view, missing. So the first time a thread reaches this
    conversation, its recent messages are read once and shown to the model with
    that turn's message — not written into the stored history, and not read
    again on later turns, which have the bot's own record to go on.

    Args:
        first_turn: Whether this conversation has no history yet.

    Returns:
        int: How many messages were read.
    """
    turn = current_discussion.get()
    if turn is None or not first_turn or not turn.root_id:
        return 0
    try:
        page = await thread_messages(limit=POLICY.thread_priming, turn=turn, spend=False)
    except RetrievalRefused as refused:
        logger.info("discussion_priming_refused", session_id=turn.session_id, reason=str(refused))
        return 0
    turn.primed = list(page.records)
    logger.info(
        "discussion_thread_primed",
        session_id=turn.session_id,
        root_id=turn.root_id,
        messages=len(turn.primed),
    )
    return len(turn.primed)


def augment_llm_messages(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Add the primed thread context to this turn's model call only.

    Kept out of the checkpoint deliberately: the stored history would otherwise
    replay ten of other people's messages into every later turn of the
    conversation, and grow by ten more each time the bot is pulled into a new
    thread.

    Args:
        messages: Messages as dumped for the LLM service.

    Returns:
        list[dict]: A copy with the last user message expanded, or the input
        unchanged when nothing was primed.
    """
    turn = current_discussion.get()
    if turn is None or not turn.primed:
        return messages

    index = next(
        (i for i in range(len(messages) - 1, -1, -1) if messages[i].get("role") == "user"),
        None,
    )
    if index is None:
        return messages

    block = prime_block(turn.primed)
    original = messages[index]
    content = original.get("content")
    expanded = dict(original)
    if isinstance(content, list):
        expanded["content"] = [*content, {"type": "text", "text": block}]
    else:
        expanded["content"] = f"{content}\n\n{block}"
    return [*messages[:index], expanded, *messages[index + 1 :]]


def read_other_people() -> bool:
    """Whether this turn took other people's messages back from retrieval.

    Long-term memory is a record of the person the bot is talking to. A turn
    that read a discussion has other people's statements in it, and those are
    not facts about the requester.

    Returns:
        bool: True when any message by someone else was retrieved this turn.
    """
    turn = current_discussion.get()
    if turn is None:
        return False
    return any(
        record.author_id and record.author_id != turn.requester_user_id and not record.is_bot
        for record in turn.records.values()
    )


__all__ = [
    "POLICY",
    "ChannelAccess",
    "Digest",
    "DiscussionTurn",
    "NoDiscussionContext",
    "RetrievalRefused",
    "augment_llm_messages",
    "begin_turn",
    "channel_messages",
    "current_discussion",
    "end_turn",
    "investigate",
    "may_disclose",
    "message",
    "prime_thread",
    "read_other_people",
    "require_turn",
    "resolve_channel",
    "search",
    "thread_messages",
]
