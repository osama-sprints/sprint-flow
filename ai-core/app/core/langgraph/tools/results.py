"""Uniform tool results and the guard every Sprint 1 tool wears.

A tool returns ``"[CODE] sentence"``: the code is stable for verification
scripts and logs, the sentence is what the agent relays. The guard turns the
two domain exceptions into their codes, converts anything unexpected into a
readable ``SYSTEM_ERROR`` (a tool must never surface a traceback to a person),
and lets LangGraph's interrupt bubble up untouched — swallowing it would break
every confirmation.
"""

import asyncio
import functools
import inspect
from enum import StrEnum
from typing import (
    Any,
    Awaitable,
    Callable,
    TypeVar,
)

from langgraph.errors import GraphBubbleUp

from app.core.logging import logger
from app.services.authorisation import (
    REFUSAL_MESSAGE,
    AuthorisationRefused,
    ValidationFailed,
)

F = TypeVar("F", bound=Callable[..., Any])


class ResultCode(StrEnum):
    """Stable prefixes for tool results."""

    OK = "OK"
    CHANNEL_CREATED = "CHANNEL_CREATED"
    CHANNEL_ALREADY_EXISTS = "CHANNEL_ALREADY_EXISTS"
    ROLE_ASSIGNED = "ROLE_ASSIGNED"
    ROLE_ALREADY_ASSIGNED = "ROLE_ALREADY_ASSIGNED"
    ROLE_CHANGED = "ROLE_CHANGED"
    SPRINT_OPENED = "SPRINT_OPENED"
    SPRINT_ALREADY_OPEN = "SPRINT_ALREADY_OPEN"
    CEREMONY_SCHEDULED = "CEREMONY_SCHEDULED"
    CEREMONY_AMENDED = "CEREMONY_AMENDED"
    CEREMONY_CANCELLED = "CEREMONY_CANCELLED"
    CEREMONY_CONFLICT = "CEREMONY_CONFLICT"
    TIME_CLARIFICATION_REQUIRED = "TIME_CLARIFICATION_REQUIRED"
    CONFIRMATION_DECLINED = "CONFIRMATION_DECLINED"
    AUTHORISATION_REFUSED = "AUTHORISATION_REFUSED"
    VALIDATION_ERROR = "VALIDATION_ERROR"
    SYSTEM_ERROR = "SYSTEM_ERROR"
    ESCALATION_OPENED = "ESCALATION_OPENED"
    ESCALATION_OPENED_NO_HUMAN = "ESCALATION_OPENED_NO_HUMAN"
    ESCALATION_ALREADY_OPEN = "ESCALATION_ALREADY_OPEN"


SYSTEM_ERROR_MESSAGE = (
    "Something went wrong on my side while doing that, so I stopped. "
    "Please try again in a moment; if it keeps failing, tell an administrator."
)


def tool_result(code: ResultCode | str, message: str) -> str:
    """Format a tool result.

    Args:
        code: The stable result code.
        message: The sentence the agent relays.

    Returns:
        str: ``"[CODE] message"``.
    """
    return f"[{code}] {message.strip()}"


def result_code_of(result: str) -> str | None:
    """Parse the code out of a tool result string.

    Args:
        result: A string produced by ``tool_result``.

    Returns:
        str | None: The code, or None when the string carries no code prefix.
    """
    if not result.startswith("["):
        return None
    end = result.find("]")
    return result[1:end] if end > 1 else None


def _handle(exc: BaseException, tool_name: str) -> str:
    """Map an exception raised inside a tool to a result string.

    Args:
        exc: The exception.
        tool_name: For logs.

    Returns:
        str: The tool result.

    Raises:
        GraphBubbleUp: Re-raised so interrupts and parent commands propagate.
    """
    if isinstance(exc, GraphBubbleUp):
        raise exc
    if isinstance(exc, AuthorisationRefused):
        logger.warning("tool_refused", tool=tool_name, reason=exc.reason, action=exc.action, channel_id=exc.channel_id)
        return tool_result(ResultCode.AUTHORISATION_REFUSED, REFUSAL_MESSAGE)
    if isinstance(exc, ValidationFailed):
        logger.info("tool_validation_failed", tool=tool_name, detail=str(exc))
        return tool_result(ResultCode.VALIDATION_ERROR, str(exc))
    logger.exception("tool_failed", tool=tool_name, error=str(exc))
    return tool_result(ResultCode.SYSTEM_ERROR, SYSTEM_ERROR_MESSAGE)


def guarded_tool(func: F) -> F:
    """Wrap a tool body so it never raises past the tool boundary (except to bubble up).

    Works for both ``async def`` and ``def`` bodies and preserves the signature
    LangChain introspects to build the tool's argument schema.

    Args:
        func: The tool body.

    Returns:
        The wrapped body.
    """
    if inspect.iscoroutinefunction(func):
        async_func: Callable[..., Awaitable[Any]] = func

        @functools.wraps(func)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            try:
                return await async_func(*args, **kwargs)
            except BaseException as exc:
                if isinstance(exc, (KeyboardInterrupt, SystemExit, asyncio.CancelledError)):
                    raise
                return _handle(exc, func.__name__)

        return async_wrapper  # type: ignore[return-value]

    @functools.wraps(func)
    def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return func(*args, **kwargs)
        except BaseException as exc:
            if isinstance(exc, (KeyboardInterrupt, SystemExit, asyncio.CancelledError)):
                raise
            return _handle(exc, func.__name__)

    return sync_wrapper  # type: ignore[return-value]
