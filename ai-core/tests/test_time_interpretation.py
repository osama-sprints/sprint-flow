"""Table-driven tests for the pure time interpreter. No database, no network, fixed ``now``."""

from datetime import (
    UTC,
    datetime,
    timedelta,
)
from zoneinfo import ZoneInfo

import pytest

from app.services.time_interpretation import (
    TimeInterpretation,
    interpret_time,
    parse_yes_no,
)

# Thursday 3 September 2026, 10:00 in Berlin (08:00 UTC).
NOW = datetime(2026, 9, 3, 10, 0, tzinfo=ZoneInfo("Europe/Berlin"))
MIN_LEAD = timedelta(minutes=5)
MAX_HORIZON = timedelta(days=365)


def interpret(expression: str, zone: str | None = "Europe/Berlin", **overrides) -> TimeInterpretation:
    kwargs = {"zone": zone, "now": NOW, "min_lead": MIN_LEAD, "max_horizon": MAX_HORIZON}
    kwargs.update(overrides)
    return interpret_time(expression, **kwargs)


def utc(year: int, month: int, day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


# --- ok: exact UTC instants -----------------------------------------------------


@pytest.mark.parametrize(
    ("expression", "zone", "expected"),
    [
        ("tomorrow at 2pm", "Europe/Berlin", utc(2026, 9, 4, 12)),
        ("tomorrow at 2 pm", "Europe/Berlin", utc(2026, 9, 4, 12)),
        ("tomorrow at 2 p.m.", "Africa/Cairo", utc(2026, 9, 4, 11)),
        ("tomorrow 14:00", "Europe/Berlin", utc(2026, 9, 4, 12)),
        ("tomorrow at 14:00", "Africa/Cairo", utc(2026, 9, 4, 11)),
        ("tomorrow 14:00", "America/New_York", utc(2026, 9, 4, 18)),
        ("tomorrow 14:00", "UTC", utc(2026, 9, 4, 14)),
        ("Monday 9am", "Europe/Berlin", utc(2026, 9, 7, 7)),
        ("next monday at 9am", "Europe/Berlin", utc(2026, 9, 7, 7)),
        ("noon tomorrow", "America/New_York", utc(2026, 9, 4, 16)),
        ("tomorrow at midnight", "UTC", utc(2026, 9, 4, 0)),
        ("tomorrow at 12pm", "UTC", utc(2026, 9, 4, 12)),
        ("tomorrow at 12am", "UTC", utc(2026, 9, 4, 0)),
        ("tomorrow at 15", "Africa/Cairo", utc(2026, 9, 4, 12)),
        ("at 21 tomorrow", "UTC", utc(2026, 9, 4, 21)),
        ("2 in the afternoon tomorrow", "Europe/Berlin", utc(2026, 9, 4, 12)),
        ("friday 9 in the morning", "UTC", utc(2026, 9, 4, 9)),
        ("10 September 2026, 16:00", "Africa/Cairo", utc(2026, 9, 10, 13)),
        ("Sept 4 2pm", "UTC", utc(2026, 9, 4, 14)),
        ("tomorrow at 09:30", "UTC", utc(2026, 9, 4, 9, 30)),
        ("today at 10:05", "Europe/Berlin", utc(2026, 9, 3, 8, 5)),
    ],
)
def test_clear_expressions_resolve_to_exact_utc_instants(expression, zone, expected):
    result = interpret(expression, zone)
    assert result.status == "ok", result.question
    assert result.instant == expected
    assert result.instant is not None and result.instant.tzinfo is UTC
    assert result.zone == zone
    assert result.utc_display == expected.strftime("%Y-%m-%d %H:%M UTC")
    assert result.local_display is not None and result.local_display.endswith(f"({zone})")


def test_local_display_names_the_weekday_and_zone():
    result = interpret("tomorrow at 2pm", "Europe/Berlin")
    assert result.local_display == "Friday 4 September 2026, 14:00 (Europe/Berlin)"
    assert result.utc_display == "2026-09-04 12:00 UTC"


# --- explicit zone in the text beats the caller's zone ---------------------------


@pytest.mark.parametrize(
    ("expression", "zone", "expected", "zone_name"),
    [
        ("tomorrow at 2pm UTC", "Europe/Berlin", utc(2026, 9, 4, 14), "UTC"),
        ("tomorrow at 2pm gmt", "Africa/Cairo", utc(2026, 9, 4, 14), "UTC"),
        ("next thursday at 14:00 CET", "Africa/Cairo", utc(2026, 9, 10, 13), "CET (UTC+01:00)"),
        ("3pm +02:00 tomorrow", "UTC", utc(2026, 9, 4, 13), "UTC+02:00"),
        ("tomorrow at 2pm GMT+2", "UTC", utc(2026, 9, 4, 12), "UTC+02:00"),
        ("2026-09-10T14:00:00+02:00", "UTC", utc(2026, 9, 10, 12), "UTC+02:00"),
        ("tomorrow at 2pm Europe/Berlin", "America/New_York", utc(2026, 9, 4, 12), "Europe/Berlin"),
        ("tomorrow at 2pm africa/cairo", None, utc(2026, 9, 4, 11), "Africa/Cairo"),
    ],
)
def test_explicit_zone_in_text_overrides_default(expression, zone, expected, zone_name):
    result = interpret(expression, zone)
    assert result.status == "ok", result.question
    assert result.instant == expected
    assert result.zone == zone_name


def test_year_digits_are_never_read_as_a_24_hour_time():
    # Review 02: "21" inside "2100" was matched as an hour. With a long horizon the
    # instant must be exactly 12:00 UTC on 1 January 2100.
    result = interpret("January 1, 2100 at 12:00 UTC", "Europe/Berlin", max_horizon=timedelta(days=40000))
    assert result.status == "ok", result.question
    assert result.instant == utc(2100, 1, 1, 12)
    assert result.utc_display == "2100-01-01 12:00 UTC"


def test_year_digits_case_is_too_far_under_the_default_horizon():
    result = interpret("January 1, 2100 at 12:00 UTC", "Europe/Berlin")
    assert result.status == "too_far"
    assert "2100-01-01 12:00 UTC" in (result.question or "")


# --- ambiguity is a question, never a guess ---------------------------------------


@pytest.mark.parametrize(
    ("expression", "zone", "fragment"),
    [
        ("tomorrow at 2", "Europe/Berlin", "2 in the afternoon or 2 in the morning"),
        ("tomorrow at 12", "UTC", "12 in the afternoon or 12 in the morning"),
        ("tomorrow at 2:30", "UTC", "2:30 in the afternoon or 2:30 in the morning"),
        ("tomorrow at 2 o'clock", "UTC", "afternoon"),
        ("tomorrow", "Europe/Berlin", "clock time"),
        ("tomorrow morning", "UTC", "clock time"),
        ("in 2 hours", "UTC", "clock time"),
        ("3pm UTC", "Europe/Berlin", "Which day"),
        ("2pm", "UTC", "Which day"),
        ("next week at 2pm", "Europe/Berlin", "Which day of 'next week'"),
        ("4/9 at 2pm", "UTC", "day/month or month/day"),
        ("the 10th at 2pm", "UTC", "does not name a full day"),
        ("sept at 2pm", "UTC", "does not name a full day"),
        ("tomorrow at 2pm and 3pm", "UTC", "more than one time"),
        ("tomorrow at 2pm IST", "UTC", "do not recognise the timezone 'IST'"),
        ("tomorrow at 2pm Mars/Base", "UTC", "do not recognise the timezone 'Mars/Base'"),
    ],
)
def test_ambiguous_expressions_return_a_question(expression, zone, fragment):
    result = interpret(expression, zone)
    assert result.status == "ambiguous", (result.status, result.question)
    assert result.instant is None
    assert result.question and fragment in result.question


@pytest.mark.parametrize(
    ("expression", "zone", "fragment"),
    [
        ("2026-03-29 02:30", "Europe/Berlin", "does not exist"),
        ("29 March 2026 at 2:30am", "Europe/Berlin", "does not exist"),
        ("2026-03-08 02:30", "America/New_York", "does not exist"),
        ("2026-10-25 02:30", "Europe/Berlin", "happens twice"),
        ("2026-11-01 01:30", "America/New_York", "happens twice"),
    ],
)
def test_dst_gaps_and_folds_are_ambiguous(expression, zone, fragment):
    early = datetime(2026, 1, 15, 9, 0, tzinfo=UTC)
    result = interpret(expression, zone, now=early)
    assert result.status == "ambiguous", (result.status, result.question)
    assert result.question and fragment in result.question


def test_dst_edge_times_that_exist_once_are_fine():
    early = datetime(2026, 1, 15, 9, 0, tzinfo=UTC)
    # 03:30 on the spring-forward day exists exactly once (CEST, UTC+2).
    result = interpret("2026-03-29 03:30", "Europe/Berlin", now=early)
    assert result.status == "ok", result.question
    assert result.instant == utc(2026, 3, 29, 1, 30)
    # Cairo is UTC+3 in summer 2026; 14:00 local is 11:00 UTC.
    summer = interpret("2026-07-01 14:00", "Africa/Cairo", now=early)
    assert summer.status == "ok" and summer.instant == utc(2026, 7, 1, 11)


# --- unparseable, past, too far, no timezone ----------------------------------------


@pytest.mark.parametrize(
    ("expression", "zone"),
    [
        ("Feb 30 at 10am", "Europe/Berlin"),
        ("tmrw at 2pm", "UTC"),
        ("tomorrow 2pm blah", "UTC"),
        ("tomorrow at 13pm", "UTC"),
        ("", "UTC"),
    ],
)
def test_unparseable_expressions_return_a_question(expression, zone):
    result = interpret(expression, zone)
    assert result.status == "unparseable"
    assert result.instant is None
    assert result.question


@pytest.mark.parametrize(
    ("expression", "zone"),
    [
        ("yesterday at 3pm", "Europe/Berlin"),
        ("today at 9am", "Europe/Berlin"),
        ("today at 10:04", "Europe/Berlin"),  # inside the 5-minute lead
    ],
)
def test_past_expressions_return_a_question(expression, zone):
    result = interpret(expression, zone)
    assert result.status == "past"
    assert result.instant is None
    assert result.question and "past" in result.question


def test_too_far_expressions_return_a_question():
    result = interpret("in 3 years at 10am", "Europe/Berlin")
    assert result.status == "too_far"
    assert result.question and "365 days" in result.question


def test_no_zone_anywhere_asks_for_one():
    result = interpret("tomorrow at 2pm", None)
    assert result.status == "no_timezone"
    assert result.question and "timezone" in result.question


def test_unknown_caller_zone_asks_for_one():
    result = interpret("tomorrow at 2pm", "Mars/Base")
    assert result.status == "no_timezone"
    assert result.question and "Mars/Base" in result.question


def test_naive_now_is_rejected():
    with pytest.raises(ValueError):
        interpret_time(
            "tomorrow at 2pm", zone="UTC", now=datetime(2026, 9, 3, 10), min_lead=MIN_LEAD, max_horizon=MAX_HORIZON
        )


def test_same_words_same_instant_regardless_of_process_zone(monkeypatch):
    # Review 01: the same phrase produced different instants when the server zone changed.
    monkeypatch.setenv("TZ", "Pacific/Auckland")
    first = interpret("tomorrow at 2pm", "Europe/Berlin")
    monkeypatch.setenv("TZ", "America/Los_Angeles")
    second = interpret("tomorrow at 2pm", "Europe/Berlin")
    assert first.instant == second.instant == utc(2026, 9, 4, 12)


# --- confirmation replies ----------------------------------------------------------


@pytest.mark.parametrize(
    "reply",
    [
        "yes",
        "y",
        "Y.",
        "yeah",
        "Yeah!",
        "yep",
        "yup",
        "confirm",
        "Confirmed.",
        "correct",
        "ok",
        "okay",
        "go ahead",
        "do it",
        "book it",
        "sure",
        "please do",
        "Yes please",
        "proceed",
    ],
)
def test_parse_yes_no_affirmatives(reply):
    assert parse_yes_no(reply) is True


@pytest.mark.parametrize(
    "reply",
    [
        "no",
        "nope",
        "cancel",
        "wrong",
        "don't",
        "do not",
        "stop",
        "change it to 3pm",
        "yes but change the time",
        "No thanks",
        "incorrect",
        "not yet",
        "never mind",
    ],
)
def test_parse_yes_no_negatives(reply):
    assert parse_yes_no(reply) is False


@pytest.mark.parametrize("reply", ["maybe", "", "what?", "tomorrow at 3pm", "hmm"])
def test_parse_yes_no_unclear(reply):
    assert parse_yes_no(reply) is None
