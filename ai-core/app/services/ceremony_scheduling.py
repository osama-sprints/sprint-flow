"""Ceremony scheduling: authorise, interpret, check conflicts, confirm, persist.

The flow is split in two on purpose. ``prepare_*`` does every read — resolve
the cohort, authorise the requester from stored data, interpret the time,
look for clashes — and returns a proposal that carries everything needed to
show the person exactly what will be stored. ``commit_*`` performs the single
write, and only the tool layer decides to call it, after the person has
confirmed through LangGraph's ``interrupt()``. Nothing is written before that
confirmation, and a proposal that is declined leaves no trace.

Authorisation is the shared implementation in ``app.services.authorisation``;
the organiser is always the stored requester, never an argument.
"""

from dataclasses import (
    dataclass,
    field,
)
from datetime import (
    datetime,
    timedelta,
    tzinfo,
)
from typing import (
    Any,
    Literal,
    Sequence,
)

from app.core.config import settings
from app.core.logging import logger
from app.core.requester import RequesterContext
from app.models import (
    Ceremony,
    CeremonyAmendment,
    Cohort,
    utcnow,
)
from app.models.enums import (
    CEREMONY_TYPE_LABELS,
    CeremonyStatus,
    CeremonyTypeKey,
    normalise_ceremony_type_key,
)
from app.services.authorisation import (
    ValidationFailed,
    require_active_cohort,
    require_cohort_authority,
    require_cohort_membership,
    require_requester,
)
from app.services.domain import ceremonies as ceremony_repo
from app.services.domain import cohorts as cohort_repo
from app.services.domain import identity as identity_repo
from app.services.domain import sprints as sprint_repo
from app.services.time_interpretation import (
    TimeInterpretation,
    format_local,
    format_utc,
    interpret_time,
    resolve_zone,
)

ConflictPolicy = Literal["refuse", "warn"]
ProblemKind = Literal["clarification", "conflict"]

CEREMONY_TYPE_CHOICES = ", ".join(CEREMONY_TYPE_LABELS[key] for key in CeremonyTypeKey)
PAST_CEREMONY_POLICY = (
    "A ceremony that has already started cannot be moved or cancelled: the record is history. "
    "Its agenda and notes can still be updated, or a new ceremony can be scheduled."
)


@dataclass(frozen=True)
class SchedulingProblem:
    """A reason the request cannot proceed, for the tool to turn into a result code.

    Attributes:
        kind: ``clarification`` (the time needs a question) or ``conflict``.
        message: The sentence to relay to the person.
        status: The interpreter status when ``kind == "clarification"``.
    """

    kind: ProblemKind
    message: str
    status: str | None = None


@dataclass(frozen=True)
class ScheduleProposal:
    """Everything needed to confirm and then persist one ceremony.

    Attributes:
        cohort_id: The cohort.
        cohort_name: Its display name.
        ceremony_type_id: The seeded type row.
        ceremony_type_key: Its machine key.
        ceremony_type_label: Its display label.
        organizer_id: The stored requester (``users.id``).
        scheduled_at: The start instant, UTC.
        duration_minutes: Length.
        agenda: Free text, if given.
        sprint_id: The sprint, if one was named.
        sprint_name: Its name.
        time_expression: What the person typed.
        zone: The zone the expression was interpreted in.
        local_display: The instant as the person sees it.
        utc_display: The instant in UTC.
        conflict_warning: Overlap description under the ``warn`` policy, else None.
        channel_id: Channel reminders should go to, if known.
    """

    cohort_id: int
    cohort_name: str
    ceremony_type_id: int
    ceremony_type_key: str
    ceremony_type_label: str
    organizer_id: int
    scheduled_at: datetime
    duration_minutes: int
    agenda: str | None
    sprint_id: int | None
    sprint_name: str | None
    time_expression: str
    zone: str
    local_display: str
    utc_display: str
    conflict_warning: str | None = None
    channel_id: str | None = None

    def describe(self) -> str:
        """One sentence naming what will be stored.

        Returns:
            str: Type, cohort, both time renderings, duration, sprint and agenda.
        """
        parts = [
            f"{self.ceremony_type_label} for cohort '{self.cohort_name}' on {self.local_display}",
            f"— that is {self.utc_display} — lasting {self.duration_minutes} minutes",
        ]
        if self.sprint_name:
            parts.append(f"in sprint '{self.sprint_name}'")
        parts.append(f"with agenda: {self.agenda}" if self.agenda else "with no agenda yet")
        return " ".join(parts)

    def confirmation_question(self) -> str:
        """The question the tool interrupts with.

        Returns:
            str: States the interpreted instant in both zones and asks for yes/no.
        """
        warning = f"{self.conflict_warning} " if self.conflict_warning else ""
        return (
            f"{warning}Please confirm: schedule {self.describe()}. "
            "Reply 'yes' to book it or 'no' to leave it unscheduled."
        )


@dataclass(frozen=True)
class AmendmentProposal:
    """A validated change to one ceremony, ready to confirm and apply.

    Attributes:
        ceremony_id: The ceremony.
        cohort_id: Its cohort.
        cohort_name: Its cohort's name.
        ceremony_type_label: Its type label.
        amended_by_id: The stored requester.
        changes: ``{column: new_value}`` for ``update_ceremony``.
        reason: Free text stored with each amendment row.
        cancel: Whether this is a cancellation.
        requires_confirmation: True for time changes and cancellations.
        previous_local_display: The current start as the person sees it.
        previous_utc_display: The current start in UTC.
        new_local_display: The new start as the person sees it, when the time changes.
        new_utc_display: The new start in UTC, when the time changes.
        conflict_warning: Overlap description under the ``warn`` policy, else None.
    """

    ceremony_id: int
    cohort_id: int
    cohort_name: str
    ceremony_type_label: str
    amended_by_id: int
    changes: dict[str, Any]
    reason: str | None
    cancel: bool
    requires_confirmation: bool
    previous_local_display: str
    previous_utc_display: str
    new_local_display: str | None = None
    new_utc_display: str | None = None
    conflict_warning: str | None = None

    def describe(self) -> str:
        """One sentence naming the change.

        Returns:
            str: What changes on which ceremony.
        """
        subject = f"ceremony #{self.ceremony_id} ({self.ceremony_type_label}, cohort '{self.cohort_name}')"
        if self.cancel:
            return f"cancel {subject} currently on {self.previous_local_display} ({self.previous_utc_display})"
        pieces: list[str] = []
        if self.new_local_display:
            pieces.append(
                f"move it from {self.previous_local_display} ({self.previous_utc_display}) "
                f"to {self.new_local_display} — that is {self.new_utc_display}"
            )
        if "agenda" in self.changes:
            pieces.append(f"set the agenda to: {self.changes['agenda']}")
        return f"amend {subject}: " + "; ".join(pieces)

    def confirmation_question(self) -> str:
        """The question the tool interrupts with.

        Returns:
            str: States the change in both zones and asks for yes/no.
        """
        warning = f"{self.conflict_warning} " if self.conflict_warning else ""
        return f"{warning}Please confirm: {self.describe()}. Reply 'yes' to apply it or 'no' to leave it as it is."


@dataclass(frozen=True)
class CalendarEntry:
    """One ceremony as shown on the calendar.

    Attributes:
        ceremony: The row.
        type_label: Display label of its type.
        organizer_handle: ``@username`` of the organiser.
    """

    ceremony: Ceremony
    type_label: str
    organizer_handle: str


@dataclass(frozen=True)
class CalendarView:
    """A cohort's calendar plus how to render it.

    Attributes:
        cohort: The cohort.
        entries: Soonest first.
        zone: IANA zone to render local times in, or None for UTC only.
        include_past: Whether past ceremonies were requested.
        include_cancelled: Whether cancelled ceremonies were requested.
    """

    cohort: Cohort
    entries: Sequence[CalendarEntry] = field(default_factory=tuple)
    zone: str | None = None
    include_past: bool = False
    include_cancelled: bool = False


# ---------------------------------------------------------------------------
# Pure helpers (unit-tested without a database)
# ---------------------------------------------------------------------------


def effective_zone(requester: RequesterContext | None) -> str | None:
    """The zone to interpret a person's words in when the text names none.

    Precedence: the person's Mattermost profile zone, then
    ``SCHEDULING_DEFAULT_TIMEZONE``, then None (the interpreter asks).

    Args:
        requester: The bound requester, if any.

    Returns:
        str | None: An IANA zone name, or None.
    """
    profile_zone = (requester.timezone or "").strip() if requester else ""
    return profile_zone or settings.SCHEDULING_DEFAULT_TIMEZONE or None


def normalise_conflict_policy(value: str | None) -> ConflictPolicy:
    """Read the conflict policy setting, defaulting to ``refuse`` for anything unknown.

    Args:
        value: ``SCHEDULING_CONFLICT_POLICY`` as configured.

    Returns:
        ConflictPolicy: ``refuse`` or ``warn``.
    """
    candidate = (value or "").strip().lower()
    if candidate == "warn":
        return "warn"
    if candidate not in ("", "refuse"):
        logger.warning("scheduling_conflict_policy_unknown", value=value, effective="refuse")
    return "refuse"


def describe_clashes(clashes: Sequence[Ceremony], labels: dict[int, str], zone: str | None) -> str:
    """Name the ceremonies a candidate overlaps.

    Args:
        clashes: Overlapping ceremonies.
        labels: ``ceremony_type_id -> label``.
        zone: IANA zone for the local rendering, or None for UTC only.

    Returns:
        str: ``"#12 Daily Standup on ... (... UTC, 15 min)"`` joined with ``;``.
    """
    tz = resolve_zone(zone)
    described: list[str] = []
    for clash in clashes:
        label = labels.get(clash.ceremony_type_id, f"type {clash.ceremony_type_id}")
        when = format_local(clash.scheduled_at, zone or "UTC", tz) if tz and zone else format_utc(clash.scheduled_at)
        utc = f", {format_utc(clash.scheduled_at)}" if tz and zone else ""
        described.append(f"#{clash.id} {label} on {when}{utc}, {clash.duration_minutes} min")
    return "; ".join(described)


def apply_conflict_policy(
    clashes: Sequence[Ceremony],
    *,
    policy: ConflictPolicy,
    labels: dict[int, str],
    zone: str | None,
    candidate_type_id: int | None = None,
    candidate_start: datetime | None = None,
) -> SchedulingProblem | str | None:
    """Decide what an overlap means under the configured policy.

    An exact repeat (same type, same start) is refused under either policy:
    repeating a request must not create a second identical ceremony.

    Args:
        clashes: Overlapping scheduled ceremonies (already excluding the one being amended).
        policy: ``refuse`` or ``warn``.
        labels: ``ceremony_type_id -> label`` for the message.
        zone: Zone for rendering.
        candidate_type_id: The type being scheduled, for duplicate detection.
        candidate_start: The start being scheduled, for duplicate detection.

    Returns:
        SchedulingProblem | str | None: A refusal, a warning sentence, or None when nothing overlaps.
    """
    if not clashes:
        return None
    duplicates = [
        clash
        for clash in clashes
        if candidate_type_id is not None
        and candidate_start is not None
        and clash.ceremony_type_id == candidate_type_id
        and clash.scheduled_at == candidate_start
    ]
    if duplicates:
        return SchedulingProblem(
            "conflict",
            f"That ceremony already exists: {describe_clashes(duplicates, labels, zone)}. Nothing new was created.",
        )
    named = describe_clashes(clashes, labels, zone)
    if policy == "warn":
        return f"Warning: this overlaps {named}."
    return SchedulingProblem(
        "conflict",
        f"That time clashes with {named}. The cohort cannot attend two ceremonies at once; "
        "please choose another time or amend the existing ceremony.",
    )


def validate_amendment_request(
    *,
    ceremony_status: str,
    has_started: bool,
    wants_time: bool,
    wants_agenda: bool,
    wants_cancel: bool,
) -> None:
    """Apply the past-ceremony and lifecycle policy to an amendment request.

    Args:
        ceremony_status: Current ``ceremonies.status``.
        has_started: Whether ``scheduled_at`` is already behind ``now``.
        wants_time: A new time was given.
        wants_agenda: A new agenda was given.
        wants_cancel: Cancellation was requested.

    Raises:
        ValidationFailed: When nothing was asked, or the ceremony is past/cancelled and the change is not allowed.
    """
    if not (wants_time or wants_agenda or wants_cancel):
        raise ValidationFailed("Nothing to change: give a new time, a new agenda, or ask to cancel.")
    if ceremony_status == CeremonyStatus.CANCELLED.value and (wants_time or wants_agenda):
        raise ValidationFailed("That ceremony is cancelled; schedule a new one instead of amending it.")
    if has_started and (wants_time or wants_cancel):
        raise ValidationFailed(PAST_CEREMONY_POLICY)


def render_calendar(view: CalendarView) -> str:
    """Render a calendar as Markdown lines the agent can relay.

    Args:
        view: The calendar.

    Returns:
        str: One line per ceremony with id, type, local time and UTC, duration, organiser, agenda and status.
    """
    tz = resolve_zone(view.zone)
    scope = "all ceremonies" if view.include_past else "upcoming ceremonies"
    if view.include_cancelled:
        scope += " (including cancelled)"
    if not view.entries:
        return f"No {scope} for cohort '{view.cohort.name}'."
    zone_note = f"times shown in {view.zone} and UTC" if tz and view.zone else "times shown in UTC"
    lines = [f"{scope.capitalize()} for cohort '{view.cohort.name}' ({zone_note}):"]
    for entry in view.entries:
        ceremony = entry.ceremony
        when = (
            f"{format_local(ceremony.scheduled_at, view.zone, tz)} / {format_utc(ceremony.scheduled_at)}"
            if tz and view.zone
            else format_utc(ceremony.scheduled_at)
        )
        agenda = ceremony.agenda or "no agenda"
        lines.append(
            f"- #{ceremony.id} {entry.type_label} — {when} — {ceremony.duration_minutes} min — "
            f"organiser {entry.organizer_handle} — agenda: {agenda} — status: {ceremony.status}"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Database-backed service
# ---------------------------------------------------------------------------


async def _resolve_cohort(reference: str) -> Cohort:
    """Turn what the person typed into a cohort row.

    Args:
        reference: Cohort name or numeric id.

    Returns:
        Cohort: The row.

    Raises:
        ValidationFailed: When no cohort matches.
    """
    cohort = await cohort_repo.resolve_cohort(reference)
    if cohort is None or cohort.id is None:
        raise ValidationFailed(f"I do not know a cohort called '{reference.strip()}'.")
    return cohort


async def _type_labels() -> dict[int, str]:
    """Map ceremony type ids to labels for messages.

    Returns:
        dict[int, str]: ``ceremony_type_id -> label``.
    """
    return {row.id: row.label for row in await ceremony_repo.list_ceremony_types() if row.id is not None}


def _interpret(expression: str, requester: RequesterContext | None, now: datetime) -> TimeInterpretation:
    """Interpret a time expression with the configured lead and horizon.

    Args:
        expression: The person's words.
        requester: The bound requester (for the profile zone).
        now: The current instant.

    Returns:
        TimeInterpretation: The outcome.
    """
    return interpret_time(
        expression,
        zone=effective_zone(requester),
        now=now,
        min_lead=timedelta(minutes=settings.SCHEDULING_MIN_LEAD_MINUTES),
        max_horizon=timedelta(days=settings.SCHEDULING_MAX_HORIZON_DAYS),
    )


async def prepare_schedule(
    *,
    cohort: str,
    ceremony_type: str,
    time_expression: str,
    agenda: str | None = None,
    duration_minutes: int | None = None,
    sprint_name: str | None = None,
    requester: RequesterContext | None = None,
    now: datetime | None = None,
    conflict_policy: str | None = None,
) -> ScheduleProposal | SchedulingProblem:
    """Do every read needed to schedule a ceremony, without writing anything.

    Args:
        cohort: Cohort name or id.
        ceremony_type: Type key, label or alias (``retro``).
        time_expression: The person's words, verbatim.
        agenda: Free text.
        duration_minutes: Length; defaults to the type's default.
        sprint_name: Sprint to attach, by name.
        requester: The bound requester; defaults to ``current_requester``.
        now: The current instant; defaults to now.
        conflict_policy: Override of ``SCHEDULING_CONFLICT_POLICY`` (tests).

    Returns:
        ScheduleProposal | SchedulingProblem: What to confirm, or why to stop.

    Raises:
        AuthorisationRefused: When the requester may not administer the cohort.
        ValidationFailed: On an unknown cohort, type or sprint, an inactive cohort, or a bad duration.
    """
    context = requester or require_requester()
    reference = now or utcnow()

    cohort_row = await _resolve_cohort(cohort)
    assert cohort_row.id is not None
    decision = await require_cohort_authority(context, cohort_row.id, action="schedule_ceremony")
    require_active_cohort(cohort_row)
    assert decision.user is not None and decision.user.id is not None

    key = normalise_ceremony_type_key(ceremony_type)
    if key is None:
        raise ValidationFailed(
            f"'{ceremony_type}' is not a ceremony type I know. Choose one of: {CEREMONY_TYPE_CHOICES}."
        )
    type_row = await ceremony_repo.get_ceremony_type_by_key(key)
    if type_row is None or type_row.id is None:
        raise ValidationFailed(f"Ceremony type '{key}' is not seeded; ask an administrator to run the seed.")

    duration = duration_minutes if duration_minutes is not None else type_row.default_duration_minutes
    if duration <= 0 or duration > 24 * 60:
        raise ValidationFailed("Duration must be between 1 and 1440 minutes.")

    sprint_id: int | None = None
    sprint_label: str | None = None
    if sprint_name and sprint_name.strip():
        sprint = await sprint_repo.get_sprint_by_name(cohort_row.id, sprint_name)
        if sprint is None or sprint.id is None:
            raise ValidationFailed(f"Cohort '{cohort_row.name}' has no sprint called '{sprint_name.strip()}'.")
        sprint_id, sprint_label = sprint.id, sprint.name

    interpretation = _interpret(time_expression, context, reference)
    if interpretation.status != "ok" or interpretation.instant is None:
        logger.info(
            "scheduling_time_clarification",
            cohort_id=cohort_row.id,
            status=interpretation.status,
            expression=time_expression,
        )
        return SchedulingProblem("clarification", interpretation.question or "", status=interpretation.status)

    clashes = await ceremony_repo.find_overlapping_ceremonies(cohort_row.id, interpretation.instant, duration)
    verdict = apply_conflict_policy(
        clashes,
        policy=normalise_conflict_policy(conflict_policy or settings.SCHEDULING_CONFLICT_POLICY),
        labels=await _type_labels() if clashes else {},
        zone=effective_zone(context),
        candidate_type_id=type_row.id,
        candidate_start=interpretation.instant,
    )
    if isinstance(verdict, SchedulingProblem):
        logger.info("scheduling_conflict_refused", cohort_id=cohort_row.id, clashes=[c.id for c in clashes])
        return verdict

    assert interpretation.zone and interpretation.local_display and interpretation.utc_display
    return ScheduleProposal(
        cohort_id=cohort_row.id,
        cohort_name=cohort_row.name,
        ceremony_type_id=type_row.id,
        ceremony_type_key=type_row.key,
        ceremony_type_label=type_row.label,
        organizer_id=decision.user.id,
        scheduled_at=interpretation.instant,
        duration_minutes=duration,
        agenda=agenda.strip() if agenda and agenda.strip() else None,
        sprint_id=sprint_id,
        sprint_name=sprint_label,
        time_expression=time_expression.strip(),
        zone=interpretation.zone,
        local_display=interpretation.local_display,
        utc_display=interpretation.utc_display,
        conflict_warning=verdict,
        channel_id=cohort_row.mattermost_channel_id,
    )


async def commit_schedule(
    proposal: ScheduleProposal,
    *,
    requester: RequesterContext | None = None,
    conflict_policy: str | None = None,
) -> Ceremony | SchedulingProblem:
    """Persist a confirmed proposal — the one write in the scheduling flow.

    Authority and overlap are re-checked here so the write is safe even if
    something changed while the person was deciding.

    Args:
        proposal: The confirmed proposal.
        requester: The bound requester; defaults to ``current_requester``.
        conflict_policy: Override of ``SCHEDULING_CONFLICT_POLICY`` (tests).

    Returns:
        Ceremony | SchedulingProblem: The stored row, or a conflict that appeared meanwhile.

    Raises:
        AuthorisationRefused: When the requester may no longer administer the cohort.
    """
    context = requester or require_requester()
    decision = await require_cohort_authority(context, proposal.cohort_id, action="schedule_ceremony")
    assert decision.user is not None and decision.user.id is not None

    clashes = await ceremony_repo.find_overlapping_ceremonies(
        proposal.cohort_id, proposal.scheduled_at, proposal.duration_minutes
    )
    verdict = apply_conflict_policy(
        clashes,
        policy=normalise_conflict_policy(conflict_policy or settings.SCHEDULING_CONFLICT_POLICY),
        labels=await _type_labels() if clashes else {},
        zone=effective_zone(context),
        candidate_type_id=proposal.ceremony_type_id,
        candidate_start=proposal.scheduled_at,
    )
    if isinstance(verdict, SchedulingProblem):
        logger.info("scheduling_conflict_refused_at_commit", cohort_id=proposal.cohort_id)
        return verdict

    ceremony = await ceremony_repo.create_ceremony(
        cohort_id=proposal.cohort_id,
        ceremony_type_id=proposal.ceremony_type_id,
        organizer_id=decision.user.id,
        scheduled_at=proposal.scheduled_at,
        duration_minutes=proposal.duration_minutes,
        agenda=proposal.agenda,
        sprint_id=proposal.sprint_id,
        time_expression=proposal.time_expression,
        time_zone=proposal.zone,
        channel_id=proposal.channel_id,
    )
    logger.info(
        "ceremony_scheduled",
        ceremony_id=ceremony.id,
        cohort_id=ceremony.cohort_id,
        ceremony_type=proposal.ceremony_type_key,
        scheduled_at=ceremony.scheduled_at.isoformat(),
        organizer_id=ceremony.organizer_id,
    )
    return ceremony


def _display_pair(instant: datetime, zone: str | None) -> tuple[str, str]:
    """Render an instant for the person and in UTC.

    Args:
        instant: The instant.
        zone: IANA zone or None.

    Returns:
        tuple[str, str]: ``(local or UTC rendering, UTC rendering)``.
    """
    tz: tzinfo | None = resolve_zone(zone)
    local = format_local(instant, zone, tz) if tz and zone else format_utc(instant)
    return local, format_utc(instant)


async def prepare_amendment(
    *,
    ceremony_id: int,
    new_time_expression: str | None = None,
    new_agenda: str | None = None,
    cancel: bool = False,
    reason: str | None = None,
    requester: RequesterContext | None = None,
    now: datetime | None = None,
    conflict_policy: str | None = None,
) -> AmendmentProposal | SchedulingProblem:
    """Validate an amendment without writing anything.

    Args:
        ceremony_id: The ceremony.
        new_time_expression: The person's words for the new time, if the time changes.
        new_agenda: The new agenda, if it changes.
        cancel: Whether to cancel.
        reason: Free text stored in the amendment trail.
        requester: The bound requester; defaults to ``current_requester``.
        now: The current instant; defaults to now.
        conflict_policy: Override of ``SCHEDULING_CONFLICT_POLICY`` (tests).

    Returns:
        AmendmentProposal | SchedulingProblem: What to confirm/apply, or why to stop.

    Raises:
        AuthorisationRefused: When the requester may not administer the ceremony's cohort.
        ValidationFailed: Unknown ceremony, inactive cohort, nothing to change, or the past-ceremony policy.
    """
    context = requester or require_requester()
    reference = now or utcnow()

    ceremony = await ceremony_repo.get_ceremony(ceremony_id)
    if ceremony is None or ceremony.id is None:
        raise ValidationFailed(f"There is no ceremony #{ceremony_id}.")
    decision = await require_cohort_authority(context, ceremony.cohort_id, action="amend_ceremony")
    assert decision.user is not None and decision.user.id is not None
    cohort_row = await cohort_repo.get_cohort(ceremony.cohort_id)
    if cohort_row is None:
        raise ValidationFailed(f"Ceremony #{ceremony_id} belongs to a cohort that no longer exists.")
    require_active_cohort(cohort_row)

    wants_time = bool(new_time_expression and new_time_expression.strip())
    wants_agenda = new_agenda is not None
    if cancel and ceremony.status == CeremonyStatus.CANCELLED.value:
        return SchedulingProblem("conflict", f"Ceremony #{ceremony_id} is already cancelled; nothing changed.")
    validate_amendment_request(
        ceremony_status=ceremony.status,
        has_started=ceremony.scheduled_at <= reference,
        wants_time=wants_time,
        wants_agenda=wants_agenda,
        wants_cancel=cancel,
    )

    labels = await _type_labels()
    zone = effective_zone(context)
    previous_local, previous_utc = _display_pair(ceremony.scheduled_at, zone)
    changes: dict[str, Any] = {}
    new_local: str | None = None
    new_utc: str | None = None
    warning: str | None = None

    if cancel:
        changes["status"] = CeremonyStatus.CANCELLED.value
    if wants_agenda:
        changes["agenda"] = (new_agenda or "").strip() or None
    if wants_time and new_time_expression:
        interpretation = _interpret(new_time_expression, context, reference)
        if interpretation.status != "ok" or interpretation.instant is None:
            logger.info(
                "amendment_time_clarification",
                ceremony_id=ceremony_id,
                status=interpretation.status,
                expression=new_time_expression,
            )
            return SchedulingProblem("clarification", interpretation.question or "", status=interpretation.status)
        clashes = await ceremony_repo.find_overlapping_ceremonies(
            ceremony.cohort_id, interpretation.instant, ceremony.duration_minutes, exclude_id=ceremony.id
        )
        verdict = apply_conflict_policy(
            clashes,
            policy=normalise_conflict_policy(conflict_policy or settings.SCHEDULING_CONFLICT_POLICY),
            labels=labels,
            zone=zone,
        )
        if isinstance(verdict, SchedulingProblem):
            logger.info("amendment_conflict_refused", ceremony_id=ceremony_id, clashes=[c.id for c in clashes])
            return verdict
        warning = verdict
        changes["scheduled_at"] = interpretation.instant
        changes["time_expression"] = new_time_expression.strip()
        changes["time_zone"] = interpretation.zone
        new_local, new_utc = interpretation.local_display, interpretation.utc_display

    return AmendmentProposal(
        ceremony_id=ceremony.id,
        cohort_id=ceremony.cohort_id,
        cohort_name=cohort_row.name,
        ceremony_type_label=labels.get(ceremony.ceremony_type_id, "ceremony"),
        amended_by_id=decision.user.id,
        changes=changes,
        reason=reason.strip() if reason and reason.strip() else None,
        cancel=cancel,
        requires_confirmation=cancel or wants_time,
        previous_local_display=previous_local,
        previous_utc_display=previous_utc,
        new_local_display=new_local,
        new_utc_display=new_utc,
        conflict_warning=warning,
    )


async def commit_amendment(
    proposal: AmendmentProposal,
    *,
    requester: RequesterContext | None = None,
) -> tuple[Ceremony, list[CeremonyAmendment]]:
    """Apply a confirmed amendment through the audited update.

    Args:
        proposal: The confirmed proposal.
        requester: The bound requester; defaults to ``current_requester``.

    Returns:
        tuple[Ceremony, list[CeremonyAmendment]]: The updated row and its full amendment trail.

    Raises:
        AuthorisationRefused: When the requester may no longer administer the cohort.
        ValidationFailed: When the ceremony vanished meanwhile.
    """
    context = requester or require_requester()
    decision = await require_cohort_authority(context, proposal.cohort_id, action="amend_ceremony")
    assert decision.user is not None and decision.user.id is not None

    updated = await ceremony_repo.update_ceremony(
        proposal.ceremony_id,
        amended_by_id=decision.user.id,
        changes=proposal.changes,
        reason=proposal.reason,
    )
    if updated is None:
        raise ValidationFailed(f"There is no ceremony #{proposal.ceremony_id}.")
    trail = await ceremony_repo.list_amendments(proposal.ceremony_id)
    logger.info(
        "ceremony_amended",
        ceremony_id=proposal.ceremony_id,
        fields=sorted(proposal.changes),
        cancelled=proposal.cancel,
        amended_by_id=decision.user.id,
    )
    return updated, trail


async def list_calendar(
    *,
    cohort: str,
    include_past: bool = False,
    include_cancelled: bool = False,
    requester: RequesterContext | None = None,
    now: datetime | None = None,
) -> CalendarView:
    """A cohort's calendar for any of its members.

    Args:
        cohort: Cohort name or id.
        include_past: Also list ceremonies that already started.
        include_cancelled: Also list cancelled ceremonies.
        requester: The bound requester; defaults to ``current_requester``.
        now: The current instant; defaults to now.

    Returns:
        CalendarView: Rows plus the zone to render them in.

    Raises:
        AuthorisationRefused: When the requester is not a member of the cohort (nor a superadmin).
        ValidationFailed: When the cohort does not exist.
    """
    context = requester or require_requester()
    cohort_row = await _resolve_cohort(cohort)
    assert cohort_row.id is not None
    await require_cohort_membership(context, cohort_row.id, action="list_ceremonies")

    rows = await ceremony_repo.list_ceremonies(
        cohort_row.id, include_past=include_past, include_cancelled=include_cancelled, now=now or utcnow()
    )
    labels = await _type_labels()
    handles: dict[int, str] = {}
    entries: list[CalendarEntry] = []
    for row in rows:
        if row.organizer_id not in handles:
            organiser = await identity_repo.get_user(row.organizer_id)
            handles[row.organizer_id] = f"@{organiser.username}" if organiser else f"user {row.organizer_id}"
        entries.append(
            CalendarEntry(
                ceremony=row,
                type_label=labels.get(row.ceremony_type_id, f"type {row.ceremony_type_id}"),
                organizer_handle=handles[row.organizer_id],
            )
        )
    return CalendarView(
        cohort=cohort_row,
        entries=tuple(entries),
        zone=effective_zone(context),
        include_past=include_past,
        include_cancelled=include_cancelled,
    )
