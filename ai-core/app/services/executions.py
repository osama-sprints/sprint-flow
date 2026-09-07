"""Durable state for a running turn: progress, cancel and retry.

A turn is an asyncio task; on its own it is invisible while it runs and
impossible to stop. This module gives it a durable row (``executions``) that
records what it is doing, and honours a request to stop it:

* **progress** — nodes report what they are doing; the row's ``step`` and
  heartbeat follow, so an operator can see where a turn is and a restart can
  tell a live turn from a dead one;
* **cancel** — a request is recorded in the row and, when the turn runs in
  this process, raised straight into it through ``cancel_event``. A watcher
  polls the row so a request made elsewhere (another process, a sibling
  worker handling the typed command) is honoured too. ``run_cancellable``
  races a model or tool call against that event, so a stop takes effect
  mid-call instead of after it;
* **retry** — the stored trigger is re-run as a new execution linked by
  ``retry_of`` (see ``execution_control``).

Nothing here posts to the chat. The person sees the reply when it is ready,
a single "Stopped" reply after a cancel, or a single error reply after a
failure; cancel and retry are the typed words handled by the transports.
"""

import asyncio
import os
import socket
import time
from contextlib import suppress
from contextvars import ContextVar
from dataclasses import (
    dataclass,
    field,
)
from datetime import datetime
from typing import (
    Any,
    Awaitable,
    Dict,
    List,
    Mapping,
    Optional,
    TypeVar,
)

from app.core.config import settings
from app.core.logging import logger
from app.models import (
    Execution,
    utcnow,
)
from app.services import identity
from app.services.domain import executions as store

WORKER = f"{socket.gethostname()}:{os.getpid()}"

STEP_STARTING = "Reading the message"
STEP_THINKING = "Calling the model"
STEP_WRITING = "Writing the reply"

# Operator-facing notes for the row's ``step`` column; never shown in chat.
_TOOL_STEPS: Dict[str, str] = {
    "duckduckgo_search": "Searching the web",
    "generate_and_send_image": "Preparing an image",
    "send_chart": "Drawing a chart",
    "send_mermaid_diagram": "Drawing a diagram",
    "send_react_artifact": "Building an interface",
    "read_attachment": "Reading an attachment",
    "inspect_pdf": "Inspecting a document",
    "search_pdf": "Searching a document",
    "read_pdf_pages": "Reading pages",
    "list_attachments": "Listing attachments",
    "ask_human": "Preparing a question",
    "schedule_ceremony": "Scheduling",
    "amend_ceremony": "Updating the schedule",
    "create_cohort": "Applying a change",
    "assign_role": "Applying a change",
    "open_sprint": "Applying a change",
}

T = TypeVar("T")


class ExecutionCancelled(Exception):
    """The person asked for this turn to stop.

    Attributes:
        cancelled_by: Mattermost id of whoever asked, when known.
    """

    def __init__(self, cancelled_by: str = "") -> None:
        """Create the signal.

        Args:
            cancelled_by: Mattermost id of whoever asked.
        """
        super().__init__(f"turn cancelled{f' by {cancelled_by}' if cancelled_by else ''}")
        self.cancelled_by = cancelled_by


@dataclass
class ExecutionContext:
    """The running turn, as this process sees it.

    Attributes:
        id: The turn id.
        session_id: LangGraph thread id.
        channel_id: Channel of the reply.
        trigger_post_id: The post that started the turn.
        mattermost_user_id: Who asked.
        attempt: 1 for a first run, higher for a retry.
        started_at: Wall-clock start.
        started_monotonic: For elapsed time.
        step: Last progress note.
        cancel_event: Set when a cancel has been requested.
        cancelled_by: Mattermost id of whoever asked, when known.
        finished: Whether ``finish`` ran.
        durable: Whether the row exists; False when the insert failed.
        helpers: Background tasks (the cancel watcher).
    """

    id: str
    session_id: str
    channel_id: str
    trigger_post_id: str
    mattermost_user_id: str
    attempt: int = 1
    started_at: datetime = field(default_factory=utcnow)
    started_monotonic: float = field(default_factory=time.monotonic)
    step: str = STEP_STARTING
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)
    cancelled_by: str = ""
    finished: bool = False
    durable: bool = True
    helpers: List["asyncio.Task[None]"] = field(default_factory=list)

    @property
    def cancel_requested(self) -> bool:
        """Whether a stop has been asked for.

        Returns:
            bool: True once ``cancel_event`` is set.
        """
        return self.cancel_event.is_set()

    @property
    def elapsed_seconds(self) -> int:
        """Seconds since the turn started.

        Returns:
            int: Whole seconds.
        """
        return int(time.monotonic() - self.started_monotonic)


current_execution: ContextVar[Optional[ExecutionContext]] = ContextVar("current_execution", default=None)

# Executions running in THIS process, by id: cancel reaches them directly.
_active: Dict[str, ExecutionContext] = {}


def step_for_tool(tool_name: str, args: Mapping[str, Any] | None = None) -> str:
    """The progress note for a tool call.

    Args:
        tool_name: The tool's name.
        args: Its arguments (unused; kept for tool-specific wording).

    Returns:
        str: A short operator-facing note.
    """
    if tool_name.startswith("list_"):
        return "Looking something up"
    if tool_name.startswith("mattermost_"):
        return "Updating the workspace"
    return _TOOL_STEPS.get(tool_name, "Working")


async def begin(
    *,
    execution_id: str,
    session_id: str,
    channel_id: str,
    root_id: str,
    trigger_post_id: str,
    source: str,
    mattermost_user_id: str,
    requester_user_id: Optional[int],
    trigger: Mapping[str, Any],
    retry_of: Optional[str] = None,
    attempt: int = 1,
) -> Optional[ExecutionContext]:
    """Record the start of a turn and bind its context.

    Args:
        execution_id: The turn id.
        session_id: LangGraph thread id.
        channel_id: Channel of the reply.
        root_id: Thread root of the trigger, when it has one.
        trigger_post_id: The triggering post.
        source: Transport label.
        mattermost_user_id: Who asked.
        requester_user_id: ``users.id`` of that person, when known.
        trigger: The normalised message, stored so a retry can re-run it.
        retry_of: The execution this one repeats, when it is a retry.
        attempt: Attempt number.

    Returns:
        ExecutionContext | None: The bound context, or None when tracking is off.
    """
    if not settings.EXECUTION_TRACKING_ENABLED:
        return None

    context = ExecutionContext(
        id=execution_id,
        session_id=session_id,
        channel_id=channel_id,
        trigger_post_id=trigger_post_id,
        mattermost_user_id=mattermost_user_id,
        attempt=attempt,
    )
    row = Execution(
        id=execution_id,
        session_id=session_id,
        channel_id=channel_id,
        root_id=root_id,
        trigger_post_id=trigger_post_id,
        source=source,
        mattermost_user_id=mattermost_user_id,
        requester_user_id=requester_user_id,
        status=store.RUNNING,
        step=context.step,
        attempt=attempt,
        retry_of=retry_of,
        started_at=context.started_at,
        heartbeat_at=context.started_at,
        trigger=dict(trigger),
        worker=WORKER,
    )
    try:
        await store.create_execution(row)
    except Exception as e:
        # The turn still runs; it just cannot be cancelled from elsewhere or
        # retried after a restart.
        logger.exception("execution_record_failed", execution_id=execution_id, error=str(e))
        context.durable = False

    _active[execution_id] = context
    current_execution.set(context)
    if context.durable:
        context.helpers.append(asyncio.create_task(_watch(context), name=f"execution-watch-{execution_id}"))
    logger.info(
        "execution_started", execution_id=execution_id, session_id=session_id, attempt=attempt, retry_of=retry_of
    )
    return context


async def _watch(context: ExecutionContext) -> None:
    """Heartbeat the row and pick up a cancel requested elsewhere."""
    interval = settings.EXECUTION_CANCEL_POLL_SECONDS
    while not context.finished:
        await asyncio.sleep(interval)
        if context.finished:
            return
        try:
            row = await store.update_execution(context.id, heartbeat_at=utcnow())
        except Exception as e:
            logger.warning("execution_heartbeat_failed", execution_id=context.id, error=str(e))
            continue
        if row is not None and row.cancel_requested_at is not None and not context.cancel_requested:
            context.cancelled_by = row.cancel_requested_by
            context.cancel_event.set()
            logger.info("execution_cancel_observed", execution_id=context.id, by=row.cancel_requested_by)
            return


def check_cancel() -> None:
    """Raise if the running turn has been asked to stop.

    Raises:
        ExecutionCancelled: When a cancel was requested.
    """
    context = current_execution.get()
    if context is not None and context.cancel_requested:
        raise ExecutionCancelled(context.cancelled_by)


async def progress(step: str) -> None:
    """Record what the turn is doing now.

    Also a cancellation point: a turn that keeps reporting keeps checking.

    Args:
        step: A short note for the row.

    Raises:
        ExecutionCancelled: When a cancel was requested.
    """
    context = current_execution.get()
    if context is None or context.finished:
        return
    context.step = step.strip()[:200]
    if context.durable:
        try:
            await store.update_execution(context.id, step=context.step, heartbeat_at=utcnow())
        except Exception as e:
            logger.warning("execution_progress_write_failed", execution_id=context.id, error=str(e))
    check_cancel()


async def run_cancellable(awaitable: Awaitable[T]) -> T:
    """Await something, but stop the moment the turn is cancelled.

    Args:
        awaitable: The model or tool call.

    Returns:
        The awaitable's result.

    Raises:
        ExecutionCancelled: When a cancel arrives first; the call is cancelled.
    """
    context = current_execution.get()
    if context is None:
        return await awaitable
    check_cancel()

    work: asyncio.Task[T] = asyncio.ensure_future(awaitable)
    stop = asyncio.ensure_future(context.cancel_event.wait())
    try:
        done, _ = await asyncio.wait({work, stop}, return_when=asyncio.FIRST_COMPLETED)
    except asyncio.CancelledError:
        work.cancel()
        stop.cancel()
        raise
    if work in done:
        stop.cancel()
        return work.result()
    work.cancel()
    with suppress(asyncio.CancelledError, Exception):
        await work
    raise ExecutionCancelled(context.cancelled_by)


async def finish(status: str, *, error: str | None = None, reply_post_id: str | None = None) -> None:
    """Close the running turn's record.

    Args:
        status: succeeded, failed or cancelled.
        error: Failure reason, when failed.
        reply_post_id: The reply that was posted, when one was.
    """
    context = current_execution.get()
    if context is None or context.finished:
        return
    context.finished = True
    for helper in context.helpers:
        helper.cancel()
    with suppress(Exception):
        await asyncio.gather(*context.helpers, return_exceptions=True)
    _active.pop(context.id, None)
    finished_at = utcnow()

    if context.durable:
        try:
            await store.update_execution(
                context.id,
                status=status,
                error=error,
                reply_post_id=reply_post_id,
                finished_at=finished_at,
                heartbeat_at=finished_at,
            )
        except Exception as e:
            logger.exception("execution_finish_write_failed", execution_id=context.id, error=str(e))

    current_execution.set(None)
    logger.info(
        "execution_finished",
        execution_id=context.id,
        status=status,
        elapsed_seconds=context.elapsed_seconds,
        attempt=context.attempt,
    )


async def _is_admin(mattermost_user_id: str) -> bool:
    profile = await identity.fetch_profile(mattermost_user_id)
    email = str(profile.get("email") or "") if profile else None
    return identity.is_allowlisted_admin(email, profile)


async def may_control(row: Execution, mattermost_user_id: str) -> bool:
    """Whether this person may stop or repeat this turn.

    Args:
        row: The execution.
        mattermost_user_id: Who is asking.

    Returns:
        bool: True for the person who started it and for allowlisted admins.
    """
    if row.mattermost_user_id and row.mattermost_user_id == mattermost_user_id:
        return True
    return await _is_admin(mattermost_user_id)


async def cancel(execution_id: str, *, by_mattermost_user_id: str) -> str:
    """Ask a turn to stop.

    Args:
        execution_id: The turn.
        by_mattermost_user_id: Who is asking.

    Returns:
        str: not_found, forbidden, not_running or cancelling.
    """
    row = await store.get_execution(execution_id)
    if row is None:
        return "not_found"
    if not await may_control(row, by_mattermost_user_id):
        logger.warning("execution_cancel_forbidden", execution_id=execution_id, by=by_mattermost_user_id)
        return "forbidden"
    if row.status != store.RUNNING:
        return "not_running"

    await store.request_cancel(execution_id, by_mattermost_user_id)
    context = _active.get(execution_id)
    if context is not None and not context.cancel_requested:
        context.cancelled_by = by_mattermost_user_id
        context.cancel_event.set()
    logger.info(
        "execution_cancel_requested", execution_id=execution_id, by=by_mattermost_user_id, local=context is not None
    )
    return "cancelling"


async def recover_after_restart() -> int:
    """Fail the running rows a dead process left behind, so they can be retried.

    A row is dead when its heartbeat is stale, or when it was written by this
    very host: one container runs one process, so a running row from this
    hostname cannot still be alive at start-up.

    Returns:
        int: Rows marked failed.
    """
    if not settings.EXECUTION_TRACKING_ENABLED:
        return 0
    try:
        stale = await store.mark_stale_running_failed(stale_after_seconds=settings.EXECUTION_STALE_SECONDS)
        own = await store.find_running_for_host(WORKER.split(":")[0])
    except Exception as e:
        logger.exception("execution_recovery_failed", error=str(e))
        return 0
    recovered = {row.id for row in stale}
    for row in own:
        if row.id in recovered:
            continue
        with suppress(Exception):
            await store.update_execution(
                row.id,
                status=store.FAILED,
                error="The assistant restarted while working on this.",
                finished_at=utcnow(),
            )
            recovered.add(row.id)
    if recovered:
        logger.warning("executions_recovered_as_failed", count=len(recovered))
    return len(recovered)


def status() -> Dict[str, Any]:
    """Operator view for the health endpoint.

    Returns:
        dict: Active executions in this process and the worker id.
    """
    return {"active": len(_active), "worker": WORKER, "enabled": settings.EXECUTION_TRACKING_ENABLED}
