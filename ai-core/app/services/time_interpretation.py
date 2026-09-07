"""Turn a spoken time expression into one unambiguous instant, or into a question.

This module is pure and deterministic: no I/O, no clock of its own (``now`` is
an argument), no settings. It is the only place a phrase such as
``"tomorrow at 2pm"`` becomes a ``datetime``, and it refuses to guess:

- the clock time must be explicit (an hour with am/pm, a two-digit 24-hour
  ``HH:MM``, ``noon``/``midnight``, ``at 13``-``at 23``, or ``2 in the afternoon``);
- the day must be explicit (a date, a weekday, ``tomorrow``...);
- the zone comes from the text when it names one, otherwise from the caller
  (the person's Mattermost profile or the configured default) — never from
  the server's clock;
- a local time that does not exist or exists twice (DST gap or fold) is a
  question, not a pick.

The clock time is tokenised with anchored regular expressions before anything
else looks at the text, so digits inside a year (``2100``) can never be read as
an hour (the review-02 bug). ``dateparser`` is used only for the date part.
"""

import re
from dataclasses import dataclass
from datetime import (
    UTC,
    date,
    datetime,
    time,
    timedelta,
    timezone,
    tzinfo,
)
from functools import lru_cache
from typing import (
    Literal,
    Mapping,
)
from zoneinfo import (
    ZoneInfo,
    available_timezones,
)

import dateparser

InterpretationStatus = Literal["ok", "ambiguous", "unparseable", "past", "too_far", "no_timezone"]


@dataclass(frozen=True)
class TimeInterpretation:
    """What a time expression means, or why it cannot be acted on.

    Attributes:
        status: ``ok`` when ``instant`` is usable; otherwise why not.
        instant: Timezone-aware UTC instant when ``status == "ok"``.
        zone: The zone the expression was interpreted in (IANA name or ``UTC+02:00``).
        local_display: ``"Thursday 4 September 2026, 14:00 (Europe/Berlin)"``.
        utc_display: ``"2026-09-04 12:00 UTC"``.
        question: The clarification to ask when ``status != "ok"``.
        expression: The original text.
    """

    status: InterpretationStatus
    instant: datetime | None
    zone: str | None
    local_display: str | None
    utc_display: str | None
    question: str | None
    expression: str


@dataclass(frozen=True)
class _Zone:
    """A resolved zone: how to convert, how to name it, and how to tell dateparser."""

    tz: tzinfo
    name: str
    iana: str | None
    explicit: bool


@dataclass(frozen=True)
class _Clock:
    """An explicit clock time found in the text."""

    hour: int
    minute: int


@dataclass(frozen=True)
class _Problem:
    """A reason to stop and ask."""

    status: InterpretationStatus
    question: str


# --- Zones -------------------------------------------------------------------

# Fixed-offset abbreviations people actually type. CET is +1 whatever the
# season (CEST is +2): an abbreviation names an offset, not a place, which is
# why the confirmation echoes the instant in UTC as well.
_ZONE_ABBREVIATIONS: Mapping[str, int] = {
    "utc": 0,
    "gmt": 0,
    "wet": 0,
    "west": 60,
    "bst": 60,
    "cet": 60,
    "cest": 120,
    "eet": 120,
    "eest": 180,
    "est": -300,
    "edt": -240,
    "cst": -360,
    "cdt": -300,
    "mst": -420,
    "mdt": -360,
    "pst": -480,
    "pdt": -420,
}

_IANA_RE = re.compile(r"\b([A-Za-z]+(?:/[A-Za-z_+\-]+)+)\b")
_PREFIXED_OFFSET_RE = re.compile(r"\b(?:utc|gmt)\s*([+-])\s*(\d{1,2})(?::?(\d{2}))?\b")
_ISO_OFFSET_RE = re.compile(r"([+-])(\d{2}):(\d{2})\b")
_ABBREVIATION_RE = re.compile(r"\b(" + "|".join(_ZONE_ABBREVIATIONS) + r")\b")
# An all-caps word right after a clock time in the ORIGINAL text ("2pm IST")
# that we do not know: refuse rather than silently fall back to the profile zone.
_UNKNOWN_ABBREVIATION_RE = re.compile(r"(?:\d|[AaPp]\.?[Mm]\.?|noon|midnight)\s+([A-Z]{2,5})\b")

# --- Clock times ---------------------------------------------------------------

_MONTHS = (
    "jan|january|feb|february|mar|march|apr|april|may|jun|june|jul|july|aug|august|"
    "sep|sept|september|oct|october|nov|november|dec|december"
)
_MERIDIEM_RE = re.compile(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm|a\.m\.|p\.m\.)(?![a-z])")
_QUALIFIED_RE = re.compile(r"\b(\d{1,2})(?::(\d{2}))?\s*(?:o'?clock\s+)?in\s+the\s+(morning|afternoon|evening)\b")
_NAMED_RE = re.compile(r"\b(noon|midday|midnight)\b")
_TWENTY_FOUR_RE = re.compile(r"\b([01]\d|2[0-3]):([0-5]\d)(?::[0-5]\d)?\b")
_AT_24_RE = re.compile(rf"\bat\s+(1[3-9]|2[0-3])\b(?!\s*(?:{_MONTHS})\b)(?!:)")
_AT_BARE_RE = re.compile(rf"\bat\s+(\d{{1,2}})\b(?!\s*(?:{_MONTHS})\b)(?!:)")
_SINGLE_DIGIT_HHMM_RE = re.compile(r"(?<![\d:])(\d):([0-5]\d)\b")
_OCLOCK_RE = re.compile(r"\b(\d{1,2})\s*o'?clock\b")

# --- Date remainders -----------------------------------------------------------

_ISO_T_RE = re.compile(r"(\d{4}-\d{2}-\d{2})t(\d)")
_WEEKDAYS = "monday|tuesday|wednesday|thursday|friday|saturday|sunday|mon|tue|tues|wed|thu|thur|thurs|fri|sat|sun"
_WEEKDAY_QUALIFIER_RE = re.compile(rf"\b(?:next|this|coming)\s+({_WEEKDAYS})\b")
_FILLER_RE = re.compile(r"\b(?:at|on)\b")
_SLASH_DATE_RE = re.compile(r"\b\d{1,2}/\d{1,2}(?:/\d{2,4})?\b")
_MONTH_ONLY_RE = re.compile(rf"^(?:{_MONTHS})$")
_YEAR_ONLY_RE = re.compile(r"^\d{4}$")
_ORDINAL_ONLY_RE = re.compile(r"^(?:the\s+)?\d{1,2}(?:st|nd|rd|th)$")
_PERIOD_ONLY_RE = re.compile(r"^(?:next|this|coming)\s+(?:week|month|year)$")

_DATE_HINT = "Please give the day as well, for example 'Thursday 10 September at 14:00' or '2026-09-10 14:00'."
_TIME_HINT = "Please give the clock time explicitly, for example '2pm', '14:00' or 'noon'."


@lru_cache(maxsize=1)
def _iana_lookup() -> Mapping[str, str]:
    """Lower-cased IANA zone names mapped to their canonical spelling.

    Returns:
        Mapping[str, str]: e.g. ``{"europe/berlin": "Europe/Berlin"}``.
    """
    return {name.lower(): name for name in available_timezones()}


def _offset_zone(sign: str, hours: int, minutes: int) -> _Zone:
    """Build a fixed-offset zone.

    Args:
        sign: ``+`` or ``-``.
        hours: Offset hours.
        minutes: Offset minutes.

    Returns:
        _Zone: A fixed-offset zone named like ``UTC+02:00`` (plain ``UTC`` when zero).
    """
    total = hours * 60 + minutes
    if sign == "-":
        total = -total
    if total == 0:
        return _Zone(tz=UTC, name="UTC", iana="UTC", explicit=True)
    label = f"UTC{'+' if total >= 0 else '-'}{abs(total) // 60:02d}:{abs(total) % 60:02d}"
    return _Zone(tz=timezone(timedelta(minutes=total)), name=label, iana=None, explicit=True)


def _abbreviation_zone(abbreviation: str) -> _Zone:
    """Build a zone from a known abbreviation.

    Args:
        abbreviation: Lower-cased abbreviation present in ``_ZONE_ABBREVIATIONS``.

    Returns:
        _Zone: Fixed-offset zone named like ``CET (UTC+01:00)``.
    """
    minutes = _ZONE_ABBREVIATIONS[abbreviation]
    if minutes == 0:
        return _Zone(tz=UTC, name="UTC", iana="UTC", explicit=True)
    sign = "+" if minutes >= 0 else "-"
    base = _offset_zone(sign, abs(minutes) // 60, abs(minutes) % 60)
    return _Zone(tz=base.tz, name=f"{abbreviation.upper()} ({base.name})", iana=None, explicit=True)


def _extract_zone(original: str, lowered: str) -> tuple[_Zone | None, str, _Problem | None]:
    """Find an explicit zone in the text and remove it.

    Args:
        original: The expression with its original case (for unknown-abbreviation detection).
        lowered: The normalised expression.

    Returns:
        tuple: ``(zone or None, remaining text, problem or None)``.
    """
    iana = _IANA_RE.search(original)
    if iana:
        canonical = _iana_lookup().get(iana.group(1).lower())
        if canonical is None:
            return (
                None,
                lowered,
                _Problem(
                    "ambiguous",
                    f"I do not recognise the timezone '{iana.group(1)}'. "
                    "Please use an IANA name such as Europe/Berlin or Africa/Cairo, or an offset such as UTC+02:00.",
                ),
            )
        remainder = (lowered[: iana.start()] + " " + lowered[iana.end() :]).strip()
        return _Zone(tz=ZoneInfo(canonical), name=canonical, iana=canonical, explicit=True), remainder, None

    prefixed = _PREFIXED_OFFSET_RE.search(lowered)
    if prefixed:
        zone = _offset_zone(prefixed.group(1), int(prefixed.group(2)), int(prefixed.group(3) or 0))
        remainder = (lowered[: prefixed.start()] + " " + lowered[prefixed.end() :]).strip()
        return zone, remainder, None

    iso = _ISO_OFFSET_RE.search(lowered)
    if iso:
        zone = _offset_zone(iso.group(1), int(iso.group(2)), int(iso.group(3)))
        remainder = (lowered[: iso.start()] + " " + lowered[iso.end() :]).strip()
        return zone, remainder, None

    unknown = _UNKNOWN_ABBREVIATION_RE.search(original)
    if unknown and unknown.group(1).lower() not in _ZONE_ABBREVIATIONS and unknown.group(1) not in ("AM", "PM"):
        return (
            None,
            lowered,
            _Problem(
                "ambiguous",
                f"I do not recognise the timezone '{unknown.group(1)}'. "
                "Please use an IANA name such as Europe/Berlin, UTC, or an offset such as UTC+02:00.",
            ),
        )

    abbreviation = _ABBREVIATION_RE.search(lowered)
    if abbreviation:
        zone = _abbreviation_zone(abbreviation.group(1))
        remainder = (lowered[: abbreviation.start()] + " " + lowered[abbreviation.end() :]).strip()
        return zone, remainder, None

    return None, lowered, None


def _first_clock(text: str) -> tuple[_Clock | None, re.Match[str] | None, _Problem | None]:
    """Find the first explicit clock time in the text.

    The patterns are anchored so a digit run can only be a time when it wears
    am/pm, a colon with a two-digit hour, a named time, or follows ``at`` with
    an unambiguous 24-hour value.

    Args:
        text: Normalised text with any zone removed.

    Returns:
        tuple: ``(clock, the match to cut out, problem)``; a problem means stop and ask.
    """
    meridiem = _MERIDIEM_RE.search(text)
    if meridiem:
        hour = int(meridiem.group(1))
        minute = int(meridiem.group(2) or 0)
        if not 1 <= hour <= 12:
            return (
                None,
                None,
                _Problem("unparseable", f"'{meridiem.group(0).strip()}' is not a valid time. {_TIME_HINT}"),
            )
        is_pm = meridiem.group(3).startswith("p")
        hour = (hour % 12) + (12 if is_pm else 0)
        return _Clock(hour, minute), meridiem, None

    qualified = _QUALIFIED_RE.search(text)
    if qualified:
        hour = int(qualified.group(1))
        minute = int(qualified.group(2) or 0)
        part = qualified.group(3)
        if not 1 <= hour <= 12 or (hour == 12 and part != "afternoon"):
            return (
                None,
                None,
                _Problem("ambiguous", f"'{qualified.group(0).strip()}' is not a clear time. {_TIME_HINT}"),
            )
        if part == "morning":
            return _Clock(hour, minute), qualified, None
        return _Clock(hour if hour == 12 else hour + 12, minute), qualified, None

    named = _NAMED_RE.search(text)
    if named:
        hour = 0 if named.group(1) == "midnight" else 12
        return _Clock(hour, 0), named, None

    twenty_four = _TWENTY_FOUR_RE.search(text)
    if twenty_four:
        return _Clock(int(twenty_four.group(1)), int(twenty_four.group(2))), twenty_four, None

    at_24 = _AT_24_RE.search(text)
    if at_24:
        return _Clock(int(at_24.group(1)), 0), at_24, None

    for pattern in (_AT_BARE_RE, _SINGLE_DIGIT_HHMM_RE, _OCLOCK_RE):
        bare = pattern.search(text)
        if bare:
            hour = int(bare.group(1))
            shown = bare.group(0).replace("at ", "").strip()
            if hour == 0 or hour > 12:
                return None, None, _Problem("unparseable", f"'{shown}' is not a valid time. {_TIME_HINT}")
            return (
                None,
                None,
                _Problem(
                    "ambiguous",
                    f"Did you mean {shown} in the afternoon or {shown} in the morning? "
                    f"Please say '{shown}pm', '{shown}am' or use 24-hour time.",
                ),
            )
    return None, None, None


def _extract_clock(text: str) -> tuple[_Clock | None, str, _Problem | None]:
    """Find the one explicit clock time and remove it from the text.

    Args:
        text: Normalised text with any zone removed.

    Returns:
        tuple: ``(clock, remaining text, problem)``.
    """
    clock, match, problem = _first_clock(text)
    if problem is not None:
        return None, text, problem
    if clock is None or match is None:
        return None, text, _Problem("ambiguous", f"I could not find a clock time in '{text.strip()}'. {_TIME_HINT}")
    remainder = (text[: match.start()] + " " + text[match.end() :]).strip()
    second, _second_match, second_problem = _first_clock(remainder)
    if second is not None or second_problem is not None:
        return (
            None,
            text,
            _Problem("ambiguous", f"'{text.strip()}' mentions more than one time. Which one do you mean?"),
        )
    return clock, remainder, None


def _clean_date_text(remainder: str) -> str:
    """Strip filler so only the date phrase reaches dateparser.

    Args:
        remainder: Text left after removing the zone and the clock time.

    Returns:
        str: The date phrase (may be empty).
    """
    text = _WEEKDAY_QUALIFIER_RE.sub(r"\1", remainder)
    text = _FILLER_RE.sub(" ", text)
    text = re.sub(r"[,;]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip(" .")
    return text


def _date_problem(phrase: str, original: str) -> _Problem | None:
    """Reject date phrases dateparser would silently misread.

    Args:
        phrase: The cleaned date phrase.
        original: The original expression, for the question.

    Returns:
        _Problem | None: A question, or None when the phrase may be parsed.
    """
    if not phrase:
        return _Problem("ambiguous", f"Which day do you mean for '{original}'? {_DATE_HINT}")
    if _SLASH_DATE_RE.search(phrase):
        return _Problem(
            "ambiguous",
            f"'{phrase}' could be day/month or month/day. Please spell the month, for example '4 September'.",
        )
    if _MONTH_ONLY_RE.match(phrase) or _YEAR_ONLY_RE.match(phrase) or _ORDINAL_ONLY_RE.match(phrase):
        return _Problem("ambiguous", f"'{phrase}' does not name a full day. {_DATE_HINT}")
    if _PERIOD_ONLY_RE.match(phrase):
        return _Problem("ambiguous", f"Which day of '{phrase}' do you mean? {_DATE_HINT}")
    return None


def _parse_date(phrase: str, zone: _Zone, now_local: datetime) -> date | None:
    """Parse the date phrase with dateparser relative to the person's local now.

    Args:
        phrase: Cleaned date phrase.
        zone: The effective zone.
        now_local: ``now`` expressed in that zone.

    Returns:
        date | None: The calendar day, or None when dateparser could not read it.
    """
    parsed = dateparser.parse(
        phrase,
        settings={
            "TIMEZONE": zone.iana or "UTC",
            "RETURN_AS_TIMEZONE_AWARE": True,
            "PREFER_DATES_FROM": "future",
            "RELATIVE_BASE": now_local,
        },
    )
    return parsed.date() if parsed is not None else None


def _localise(day: date, clock: _Clock, zone: _Zone) -> tuple[datetime | None, _Problem | None]:
    """Attach the zone to a wall-clock time, refusing DST gaps and folds.

    Args:
        day: The calendar day.
        clock: The wall-clock time.
        zone: The zone.

    Returns:
        tuple: ``(aware local datetime, problem)``.
    """
    local = datetime.combine(day, time(clock.hour, clock.minute), tzinfo=zone.tz)
    if zone.iana is None or zone.tz is UTC:
        return local, None
    wall = f"{clock.hour:02d}:{clock.minute:02d} on {day.isoformat()}"
    # A gap: the wall time never happens, so converting to UTC and back lands elsewhere.
    round_trip = local.astimezone(UTC).astimezone(zone.tz)
    if round_trip.replace(tzinfo=None) != local.replace(tzinfo=None):
        return None, _Problem(
            "ambiguous",
            f"{wall} does not exist in {zone.name} because the clocks go forward that night. "
            "Please choose another time.",
        )
    # A fold: the wall time happens twice, once per offset.
    if local.utcoffset() != local.replace(fold=1).utcoffset():
        return None, _Problem(
            "ambiguous",
            f"{wall} happens twice in {zone.name} because the clocks go back that night. "
            "Please give the time in UTC or as an offset (for example 02:30 UTC+02:00).",
        )
    return local, None


def format_local(instant: datetime, zone_name: str, tz: tzinfo) -> str:
    """Render an instant as the person sees it.

    Args:
        instant: Any aware datetime.
        zone_name: Display name of the zone.
        tz: The zone to render in.

    Returns:
        str: ``"Thursday 4 September 2026, 14:00 (Europe/Berlin)"``.
    """
    local = instant.astimezone(tz)
    return f"{local.strftime('%A')} {local.day} {local.strftime('%B %Y, %H:%M')} ({zone_name})"


def format_utc(instant: datetime) -> str:
    """Render an instant in UTC.

    Args:
        instant: Any aware datetime.

    Returns:
        str: ``"2026-09-04 12:00 UTC"``.
    """
    return instant.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")


def resolve_zone(name: str | None) -> tzinfo | None:
    """Resolve a zone name the way the interpreter does (IANA, case-insensitive).

    Args:
        name: An IANA zone name, or None.

    Returns:
        tzinfo | None: The zone, or None when unknown or absent.
    """
    if not name:
        return None
    canonical = _iana_lookup().get(name.strip().lower())
    return ZoneInfo(canonical) if canonical else None


def _refuse(
    status: InterpretationStatus, question: str, expression: str, zone: str | None = None
) -> TimeInterpretation:
    """Build a non-ok interpretation.

    Args:
        status: Why the expression cannot be used.
        question: What to ask.
        expression: The original text.
        zone: The zone in play, if any.

    Returns:
        TimeInterpretation: With no instant.
    """
    return TimeInterpretation(
        status=status,
        instant=None,
        zone=zone,
        local_display=None,
        utc_display=None,
        question=question,
        expression=expression,
    )


def interpret_time(
    expression: str,
    *,
    zone: str | None,
    now: datetime,
    min_lead: timedelta,
    max_horizon: timedelta,
) -> TimeInterpretation:
    """Interpret a spoken time expression, or say exactly what is missing.

    Zone precedence: a zone written in the expression beats ``zone``; without
    either, the result is ``no_timezone``.

    Args:
        expression: The person's words, verbatim (``"tomorrow at 2pm"``).
        zone: IANA zone to interpret in when the text names none (profile or default).
        now: The current instant (timezone-aware).
        min_lead: The instant must be at least this far after ``now``.
        max_horizon: And at most this far.

    Returns:
        TimeInterpretation: ``ok`` with a UTC instant, or a status and a question.

    Raises:
        ValueError: If ``now`` is naive.
    """
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    original = expression.strip()
    lowered = _ISO_T_RE.sub(r"\1 \2", re.sub(r"\s+", " ", original.lower()))
    if not lowered:
        return _refuse("unparseable", f"I need a day and a clock time. {_DATE_HINT}", original)

    explicit_zone, remainder, problem = _extract_zone(original, lowered)
    if problem is not None:
        return _refuse(problem.status, problem.question, original)

    effective = explicit_zone
    if effective is None:
        fallback = resolve_zone(zone)
        if fallback is None:
            hint = f" ('{zone}' is not a timezone I know)" if zone else ""
            return _refuse(
                "no_timezone",
                f"I do not know which timezone to use for '{original}'{hint}. "
                "Please tell me your timezone (for example Europe/Berlin or Africa/Cairo), "
                "or include it in the time (for example '2pm UTC').",
                original,
            )
        effective = _Zone(tz=fallback, name=str(fallback), iana=str(fallback), explicit=False)

    clock, remainder, problem = _extract_clock(remainder)
    if problem is not None or clock is None:
        question = problem.question if problem else _TIME_HINT
        status: InterpretationStatus = problem.status if problem else "ambiguous"
        return _refuse(status, question, original, effective.name)

    phrase = _clean_date_text(remainder)
    problem = _date_problem(phrase, original)
    if problem is not None:
        return _refuse(problem.status, problem.question, original, effective.name)

    now_local = now.astimezone(effective.tz)
    day = _parse_date(phrase, effective, now_local)
    if day is None:
        return _refuse(
            "unparseable",
            f"I could not read '{phrase}' as a day. {_DATE_HINT}",
            original,
            effective.name,
        )

    local, problem = _localise(day, clock, effective)
    if problem is not None or local is None:
        question = problem.question if problem else _DATE_HINT
        return _refuse("ambiguous", question, original, effective.name)

    instant = local.astimezone(UTC)
    local_display = format_local(instant, effective.name, effective.tz)
    utc_display = format_utc(instant)
    if instant < now + min_lead:
        return _refuse(
            "past",
            f"{local_display} ({utc_display}) is already in the past or too close to now to schedule. "
            "Which future time do you mean?",
            original,
            effective.name,
        )
    if instant > now + max_horizon:
        return _refuse(
            "too_far",
            f"{local_display} ({utc_display}) is more than {max_horizon.days} days away, "
            "further than I can schedule. Which nearer date do you mean?",
            original,
            effective.name,
        )
    return TimeInterpretation(
        status="ok",
        instant=instant,
        zone=effective.name,
        local_display=local_display,
        utc_display=utc_display,
        question=None,
        expression=original,
    )


_NEGATIVE_RE = re.compile(
    r"\b(?:no|nope|nah|cancel|wrong|incorrect|don'?t|do\s+not|stop|change|abort|never\s*mind|not\s+yet)\b"
)
_AFFIRMATIVE_RE = re.compile(
    r"\b(?:yes|y|yeah|yep|yup|confirm|confirmed|correct|ok|okay|go\s+ahead|do\s+it|book\s+it|sure|"
    r"please\s+do|proceed|affirmative|schedule\s+it)\b"
)


def parse_yes_no(reply: str) -> bool | None:
    """Read a confirmation reply.

    A negative word anywhere wins (``"yes, but change it"`` is not consent);
    otherwise an affirmative word means yes; anything else is unclear.

    Args:
        reply: What the person answered.

    Returns:
        bool | None: True for consent, False for refusal, None when unclear.
    """
    text = re.sub(r"\s+", " ", reply.strip().lower())
    if not text:
        return None
    if _NEGATIVE_RE.search(text):
        return False
    if _AFFIRMATIVE_RE.search(text):
        return True
    return None
