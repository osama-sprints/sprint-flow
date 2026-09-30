"""Ceremony scheduling tools: thin wrappers over ``app.services.ceremony_scheduling``.

Each tool reads the requester from the ``current_requester`` ContextVar (never
an argument), lets the service authorise and interpret, and — for anything
that writes a time — pauses the graph with ``interrupt()`` so the person sees
the absolute instant in their zone and in UTC before a row exists.

LangGraph replays the whole tool call when the person answers, so everything
before ``interrupt()`` is read-only and the single write happens after it.
``GraphInterrupt`` is never caught here; ``guarded_tool`` re-raises it.
"""

from datetime import datetime

from langchain_core.tools import tool
from langchain_core.tools.base import BaseTool
from langgraph.types import interrupt

from app.core.logging import logger
from app.core.langgraph.tools.results import (
    ResultCode,
    guarded_tool,
    tool_result,
)
from app.services import ceremony_scheduling as scheduling
from app.services.ceremony_scheduling import SchedulingProblem
from app.services.time_interpretation import parse_yes_no

_PROBLEM_CODES: dict[str, ResultCode] = {
    "clarification": ResultCode.TIME_CLARIFICATION_REQUIRED,
    "conflict": ResultCode.CEREMONY_CONFLICT,
}
_UNCLEAR_REPLY = "Your reply was not a clear yes, so I treated it as no."


def _problem(problem: SchedulingProblem) -> str:
    """Turn a service problem into a tool result.

    Args:
        problem: What stopped the request.

    Returns:
        str: ``"[CODE] sentence"``.
    """
    return tool_result(_PROBLEM_CODES[problem.kind], problem.message)


def _confirm(question: str, *, scheduled_at: datetime | None) -> bool | None:
    """Pause the graph with the question and read the person's answer.

    The interrupt carries the exact instant being confirmed. LangGraph replays
    the whole tool call when the person answers, and a relative expression
    ("tomorrow at 2pm") re-interpreted after midnight would silently point at a
    different day — so the conversation layer echoes the payload back with the
    reply, and the replayed proposal must match it exactly. If it does not, the
    person is asked again with the new instant instead of committing something
    they never saw.

    Args:
        question: The confirmation question stating the absolute instant.
        scheduled_at: The instant the question describes, or None when no time changes.

    Returns:
        bool | None: True for yes, False for no, and None when the answer is
        unclear, does not carry this question's payload, or carries a different
        instant. None means "nothing was written"; the model can ask again.
    """
    payload = {"question": question, "scheduled_at": scheduled_at.isoformat() if scheduled_at else None}
    answer = interrupt(payload)

    # LangGraph matches resumes to interrupts BY POSITION within the task, so a
    # second interrupt() raised here would shift every later question's answer
    # by one slot and could consume a reply meant for another tool call. The
    # guard is therefore a plain check with no re-ask: anything that is not this
    # question's own answer commits nothing.
    echoed = answer.get("interrupt") if isinstance(answer, dict) else None
    confirmed = echoed.get("scheduled_at") if isinstance(echoed, dict) else None
    if not isinstance(answer, dict) or payload["scheduled_at"] != confirmed:
        logger.info(
            "ceremony_confirmation_not_matched",
            expected=payload["scheduled_at"],
            echoed=confirmed,
            answer_type=type(answer).__name__,
        )
        return None
    return parse_yes_no(str(answer.get("reply", "")))


@tool
@guarded_tool
async def schedule_ceremony(
    ceremony_type: str,
    time_expression: str,
    agenda: str | None = None,
    duration_minutes: int | None = None,
    with_meet: bool = False,
) -> str:
    """Schedule an agile ceremony (standup, sprint planning, sprint review, retrospective, open Q&A) for the current channel.

    Only a tech lead or scrum master of the channel (or a platform administrator) may
    schedule; the tool checks this itself and answers ``[AUTHORISATION_REFUSED]`` otherwise.

    Pass the person's own words for the time as ``time_expression``, verbatim (for
    example "tomorrow at 2pm", "Monday 09:00", "10 September at 14:00 UTC"). Never
    compute a date or time yourself and never add or drop an am/pm. If the words are
    ambiguous the tool answers ``[TIME_CLARIFICATION_REQUIRED]`` with a question:
    relay that question to the person word for word and wait for their answer.

    When the time is clear the tool asks the person to confirm the exact date and
    time (in their timezone and in UTC) before anything is stored; nothing is
    scheduled until they say yes. Repeating the request after a "no" is fine.

    Pass ``with_meet=True`` when the person asks for a Google Meet link. A join URL
    will be created and included in the confirmation.

    Args:
        ceremony_type: The kind of ceremony: standup, planning, review, retro or q&a (aliases accepted).
        time_expression: The person's words describing when, verbatim.
        agenda: Agenda text if the person gave one.
        duration_minutes: Length in minutes if the person gave one; otherwise the type's default.
        with_meet: Whether to create and attach a Google Meet link.

    Returns:
        str: ``[CEREMONY_SCHEDULED]`` with the ceremony id and the time in both zones, or a
        result code explaining why not (clarification, conflict, refusal, validation).
    """
    outcome = await scheduling.prepare_schedule(
        ceremony_type=ceremony_type,
        time_expression=time_expression,
        agenda=agenda,
        duration_minutes=duration_minutes,
        with_meet=with_meet,
    )
    if isinstance(outcome, SchedulingProblem):
        return _problem(outcome)

    decision = _confirm(outcome.confirmation_question(), scheduled_at=outcome.scheduled_at)
    if decision is None:
        return tool_result(ResultCode.CONFIRMATION_DECLINED, f"{_UNCLEAR_REPLY} Nothing was scheduled.")
    if not decision:
        return tool_result(ResultCode.CONFIRMATION_DECLINED, "Nothing was scheduled.")

    committed = await scheduling.commit_schedule(outcome)
    if isinstance(committed, SchedulingProblem):
        return _problem(committed)
    warning = f" {outcome.conflict_warning}" if outcome.conflict_warning else ""
    meet = f"\n🔗 Join: {committed.meet_link}" if committed.meet_link else ""
    return tool_result(
        ResultCode.CEREMONY_SCHEDULED,
        f"Scheduled ceremony #{committed.id}: {outcome.describe()}.{warning}{meet}",
    )


@tool
@guarded_tool
async def amend_ceremony(
    ceremony_id: int,
    new_time_expression: str | None = None,
    new_agenda: str | None = None,
    cancel: bool = False,
    reason: str | None = None,
) -> str:
    """Change the time or agenda of an existing ceremony, or cancel it.

    Only a tech lead or scrum master of the ceremony's channel (or a platform
    administrator) may amend; the tool checks this itself. Find the ceremony id with
    ``list_ceremonies`` first if the person did not give one.

    For a new time pass the person's words verbatim as ``new_time_expression`` (never
    compute a date yourself). Ambiguous words come back as ``[TIME_CLARIFICATION_REQUIRED]``
    with a question to relay word for word. Time changes and cancellations are confirmed
    with the person (the exact instant in their timezone and in UTC) before anything is
    written; agenda-only changes apply immediately. A ceremony that has already started
    cannot be moved or cancelled, but its agenda can still be updated. Every change is
    recorded in the ceremony's amendment trail.

    Args:
        ceremony_id: The ceremony's id, as shown by ``list_ceremonies``.
        new_time_expression: The person's words for the new time, if the time changes.
        new_agenda: The new agenda text, if it changes.
        cancel: True to cancel the ceremony.
        reason: Why the change is being made, if the person said.

    Returns:
        str: ``[CEREMONY_AMENDED]`` or ``[CEREMONY_CANCELLED]`` with what changed, or a result
        code explaining why not.
    """
    outcome = await scheduling.prepare_amendment(
        ceremony_id=ceremony_id,
        new_time_expression=new_time_expression,
        new_agenda=new_agenda,
        cancel=cancel,
        reason=reason,
    )
    if isinstance(outcome, SchedulingProblem):
        return _problem(outcome)

    if outcome.requires_confirmation:
        new_instant = outcome.changes.get("scheduled_at")
        decision = _confirm(
            outcome.confirmation_question(),
            scheduled_at=new_instant if isinstance(new_instant, datetime) else None,
        )
        if decision is None:
            return tool_result(ResultCode.CONFIRMATION_DECLINED, f"{_UNCLEAR_REPLY} Nothing was changed.")
        if not decision:
            return tool_result(ResultCode.CONFIRMATION_DECLINED, "Nothing was changed.")

    _updated, trail = await scheduling.commit_amendment(outcome)
    code = ResultCode.CEREMONY_CANCELLED if outcome.cancel else ResultCode.CEREMONY_AMENDED
    warning = f" {outcome.conflict_warning}" if outcome.conflict_warning else ""
    return tool_result(
        code,
        f"Done: {outcome.describe()}. The amendment trail now holds {len(trail)} "
        f"{'entry' if len(trail) == 1 else 'entries'}.{warning}",
    )


@tool
@guarded_tool
async def list_ceremonies(include_past: bool = False, include_cancelled: bool = False) -> str:
    """List what is scheduled for the current channel: each ceremony's id, type, time (in the person's timezone and UTC), duration, organiser, agenda and status.

    Any member of the channel may read its calendar; reading is not privileged. Use
    this to answer "what's scheduled", "when is the next retro", and to find a
    ceremony's id before amending it.

    Args:
        include_past: Also include ceremonies that already took place.
        include_cancelled: Also include cancelled ceremonies.

    Returns:
        str: ``[OK]`` followed by one line per ceremony, or a refusal when the person is not a member.
    """
    view = await scheduling.list_calendar(include_past=include_past, include_cancelled=include_cancelled)
    return tool_result(ResultCode.OK, scheduling.render_calendar(view))


TOOLS: list[BaseTool] = [schedule_ceremony, amend_ceremony, list_ceremonies]

__all__ = ["TOOLS", "amend_ceremony", "list_ceremonies", "schedule_ceremony"]
