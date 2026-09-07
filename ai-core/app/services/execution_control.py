"""Cancel and retry, from the words a person types.

The transports intercept "cancel", "retry" and their Arabic counterparts
before a message reaches the agent and bring them here. Both operations are
authorised the same way — the person who started the turn, or an allowlisted
admin — from stored data.

A typed word is only treated as a command when there is something for it to
act on: a running turn for "cancel", a failed or stopped one for "retry".
Otherwise the message goes to the agent like any other, so a person answering
"stop" to a question is never swallowed.
"""

import asyncio
import re
from typing import (
    Optional,
    Set,
)

from app.core.logging import logger
from app.services import executions
from app.services.conversation import (
    IncomingMessage,
    answer_and_reply,
)
from app.services.domain import executions as store
from app.services.mattermost import mattermost_client

_CANCEL_WORDS = frozenset(
    {"cancel", "stop", "abort", "cancel it", "stop it", "إلغاء", "الغاء", "ألغ", "الغ", "توقف", "أوقف", "اوقف", "قف"}
)
_RETRY_WORDS = frozenset(
    {
        "retry",
        "try again",
        "again",
        "run it again",
        "أعد المحاولة",
        "اعد المحاولة",
        "حاول مرة أخرى",
        "حاول مرة اخرى",
        "أعد",
        "اعد",
        "كرر",
    }
)
_TRAILING_PUNCTUATION = re.compile(r"[\s.!?؟،,]+$")

# Retries run detached, like turns from the transports; asyncio keeps only
# weak references, so hold them until they finish.
_retries: Set["asyncio.Task[None]"] = set()


def command_of(text: str) -> Optional[str]:
    """Recognise a typed control word.

    Args:
        text: The message text, trigger word and mention already stripped.

    Returns:
        str | None: "cancel", "retry", or None for an ordinary message.
    """
    normalised = _TRAILING_PUNCTUATION.sub("", text.strip().lower())
    normalised = re.sub(r"\s+", " ", normalised)
    if normalised in _CANCEL_WORDS:
        return "cancel"
    if normalised in _RETRY_WORDS:
        return "retry"
    return None


async def _say(message: IncomingMessage, text: str) -> None:
    """Post a one-line answer where the reply would have gone."""
    root = message.root_id or (message.post_id if message.threads_by_default else "")
    if root:
        await mattermost_client.create_post(message.channel_id, text, root_id=root)
    else:
        await mattermost_client.create_post(message.channel_id, text)


async def retry(execution_id: str, *, by_mattermost_user_id: str, source: str = "retry") -> str:
    """Run a failed or stopped turn again from its stored trigger.

    Args:
        execution_id: The turn to repeat.
        by_mattermost_user_id: Who is asking.
        source: Transport label for the new run.

    Returns:
        str: not_found, forbidden, not_retryable or started.
    """
    row = await store.get_execution(execution_id)
    if row is None:
        return "not_found"
    if not await executions.may_control(row, by_mattermost_user_id):
        logger.warning("execution_retry_forbidden", execution_id=execution_id, by=by_mattermost_user_id)
        return "forbidden"
    if row.status not in (store.FAILED, store.CANCELLED) or not row.trigger:
        return "not_retryable"

    message = IncomingMessage.model_validate({**row.trigger, "source": source})
    task = asyncio.create_task(
        answer_and_reply(message, retry_of=row.id, attempt=row.attempt + 1),
        name=f"execution-retry-{row.id}",
    )
    _retries.add(task)
    task.add_done_callback(_retries.discard)

    logger.info(
        "execution_retry_started", execution_id=execution_id, by=by_mattermost_user_id, attempt=row.attempt + 1
    )
    return "started"


async def handle_command(message: IncomingMessage) -> bool:
    """Act on a typed cancel or retry, when there is something to act on.

    Args:
        message: The inbound message, already normalised.

    Returns:
        bool: True when the message was consumed as a command.
    """
    command = command_of(message.text)
    if command is None or message.file_ids:
        return False

    if command == "cancel":
        running = await store.find_running_for_session(message.session_id)
        if not running:
            return False
        outcomes = [await executions.cancel(row.id, by_mattermost_user_id=message.user_id) for row in running]
        logger.info("typed_cancel_handled", session_id=message.session_id, outcomes=outcomes)
        if "cancelling" in outcomes:
            # The stopped turn posts its own "Stopped" reply.
            return True
        if outcomes and all(outcome == "forbidden" for outcome in outcomes):
            await _say(message, "Only the person who asked for that, or an admin, can stop it.")
            return True
        return False

    latest = await store.latest_for_session(message.session_id, (store.FAILED, store.CANCELLED))
    if latest is None:
        return False
    outcome = await retry(latest.id, by_mattermost_user_id=message.user_id)
    logger.info("typed_retry_handled", session_id=message.session_id, execution_id=latest.id, outcome=outcome)
    if outcome == "forbidden":
        await _say(message, "Only the person who asked for that, or an admin, can retry it.")
        return True
    return outcome == "started"
