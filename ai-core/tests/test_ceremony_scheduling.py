"""Pure-logic tests for the scheduling service: conflict policy, amendment policy, renderer. No database."""

from datetime import (
    UTC,
    datetime,
)
from types import MappingProxyType

import pytest

from app.core.config import settings

from app.core.requester import RequesterContext
from app.models import (
    Ceremony,
)
from app.models.enums import CeremonyStatus
from app.services.authorisation import ValidationFailed
from app.services.ceremony_scheduling import (
    PAST_CEREMONY_POLICY,
    AmendmentProposal,
    CalendarEntry,
    CalendarView,
    ScheduleProposal,
    SchedulingProblem,
    apply_conflict_policy,
    describe_clashes,
    effective_zone,
    normalise_conflict_policy,
    render_calendar,
    validate_amendment_request,
)

START = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)
LABELS = {1: "Daily Standup", 2: "Sprint Planning"}


def ceremony(ceremony_id: int, type_id: int = 1, start: datetime = START, minutes: int = 15, **extra) -> Ceremony:
    return Ceremony(
        id=ceremony_id,
        team_id="team_123",
        channel_id="chan_456",
        ceremony_type_id=type_id,
        organizer_id=3,
        scheduled_at=start,
        duration_minutes=minutes,
        **extra,
    )


# --- conflict policy -------------------------------------------------------------


def test_no_clashes_means_no_problem_under_either_policy():
    for policy in ("refuse", "warn"):
        assert apply_conflict_policy([], policy=policy, labels=LABELS, zone="Europe/Berlin") is None


def test_refuse_policy_names_the_clash():
    verdict = apply_conflict_policy(
        [ceremony(12)],
        policy="refuse",
        labels=LABELS,
        zone="Europe/Berlin",
        candidate_type_id=2,
        candidate_start=START,
    )
    assert isinstance(verdict, SchedulingProblem)
    assert verdict.kind == "conflict"
    assert "#12 Daily Standup" in verdict.message
    assert "Friday 4 September 2026, 14:00 (Europe/Berlin)" in verdict.message
    assert "2026-09-04 12:00 UTC" in verdict.message
    assert "cannot attend two ceremonies at once" in verdict.message


def test_warn_policy_returns_a_warning_sentence_and_proceeds():
    verdict = apply_conflict_policy(
        [ceremony(12)], policy="warn", labels=LABELS, zone=None, candidate_type_id=2, candidate_start=START
    )
    assert isinstance(verdict, str)
    assert verdict.startswith("Warning: this overlaps #12 Daily Standup")
    assert "2026-09-04 12:00 UTC" in verdict


def test_exact_duplicate_is_refused_even_under_warn():
    verdict = apply_conflict_policy(
        [ceremony(12, type_id=2)], policy="warn", labels=LABELS, zone=None, candidate_type_id=2, candidate_start=START
    )
    assert isinstance(verdict, SchedulingProblem)
    assert "already exists" in verdict.message and "#12 Sprint Planning" in verdict.message


def test_describe_clashes_falls_back_to_utc_without_a_zone():
    text = describe_clashes([ceremony(5, minutes=60)], LABELS, None)
    assert text == "#5 Daily Standup on 2026-09-04 12:00 UTC, 60 min"


@pytest.mark.parametrize(
    ("value", "expected"),
    [("refuse", "refuse"), ("warn", "warn"), ("WARN ", "warn"), ("", "refuse"), (None, "refuse"), ("allow", "refuse")],
)
def test_conflict_policy_setting_defaults_to_refuse(value, expected):
    assert normalise_conflict_policy(value) == expected


# --- zone precedence -------------------------------------------------------------


def requester(zone: str | None) -> RequesterContext:
    return RequesterContext(
        mattermost_user_id="mm-1",
        username="alice",
        timezone=zone,
        team_id="team_123",
        channel_id="chan_abc",
        channel_roles=MappingProxyType({}),
    )


def test_profile_zone_beats_default(monkeypatch):
    monkeypatch.setattr(settings, "SCHEDULING_DEFAULT_TIMEZONE", "UTC")
    assert effective_zone(requester("Africa/Cairo")) == "Africa/Cairo"
    assert effective_zone(requester(None)) == "UTC"
    assert effective_zone(requester("  ")) == "UTC"
    monkeypatch.setattr(settings, "SCHEDULING_DEFAULT_TIMEZONE", "")
    assert effective_zone(requester(None)) is None
    assert effective_zone(None) is None


# --- amendment policy --------------------------------------------------------------


def test_nothing_to_change_is_a_validation_error():
    with pytest.raises(ValidationFailed, match="Nothing to change"):
        validate_amendment_request(
            ceremony_status="scheduled", has_started=False, wants_time=False, wants_agenda=False, wants_cancel=False
        )


@pytest.mark.parametrize(("wants_time", "wants_cancel"), [(True, False), (False, True), (True, True)])
def test_past_ceremony_cannot_be_moved_or_cancelled(wants_time, wants_cancel):
    with pytest.raises(ValidationFailed) as excinfo:
        validate_amendment_request(
            ceremony_status="scheduled",
            has_started=True,
            wants_time=wants_time,
            wants_agenda=False,
            wants_cancel=wants_cancel,
        )
    assert str(excinfo.value) == PAST_CEREMONY_POLICY


def test_past_ceremony_agenda_may_change():
    validate_amendment_request(
        ceremony_status="scheduled", has_started=True, wants_time=False, wants_agenda=True, wants_cancel=False
    )


def test_cancelled_ceremony_cannot_be_edited():
    with pytest.raises(ValidationFailed, match="cancelled"):
        validate_amendment_request(
            ceremony_status=CeremonyStatus.CANCELLED.value,
            has_started=False,
            wants_time=False,
            wants_agenda=True,
            wants_cancel=False,
        )


def test_future_ceremony_time_change_is_allowed():
    validate_amendment_request(
        ceremony_status="scheduled", has_started=False, wants_time=True, wants_agenda=False, wants_cancel=False
    )


# --- proposals render both zones ------------------------------------------------------


def test_schedule_proposal_question_states_both_renderings_and_asks_yes_no():
    proposal = ScheduleProposal(
        team_id="team_123",
        channel_id="chan_abc",
        ceremony_type_id=2,
        ceremony_type_key="sprint_planning",
        ceremony_type_label="Sprint Planning",
        organizer_id=3,
        scheduled_at=START,
        duration_minutes=90,
        agenda="Plan sprint 2",
        time_expression="tomorrow at 2pm",
        zone="Europe/Berlin",
        local_display="Friday 4 September 2026, 14:00 (Europe/Berlin)",
        utc_display="2026-09-04 12:00 UTC",
    )
    question = proposal.confirmation_question()
    assert "Sprint Planning" in question
    assert "Friday 4 September 2026, 14:00 (Europe/Berlin)" in question
    assert "2026-09-04 12:00 UTC" in question
    assert "90 minutes" in question and "Plan sprint 2" in question
    assert "Reply 'yes'" in question and "'no'" in question


def test_schedule_proposal_question_carries_the_warning():
    proposal = ScheduleProposal(
        team_id="team_123",
        channel_id="chan_abc",
        ceremony_type_id=2,
        ceremony_type_key="sprint_planning",
        ceremony_type_label="Sprint Planning",
        organizer_id=3,
        scheduled_at=START,
        duration_minutes=90,
        agenda=None,
        time_expression="tomorrow at 2pm",
        zone="UTC",
        local_display="Friday 4 September 2026, 12:00 (UTC)",
        utc_display="2026-09-04 12:00 UTC",
        conflict_warning="Warning: this overlaps #12 Daily Standup.",
    )
    assert proposal.confirmation_question().startswith("Warning: this overlaps #12 Daily Standup.")
    assert "no agenda yet" in proposal.describe()


def test_amendment_proposal_describes_move_and_cancel():
    move = AmendmentProposal(
        ceremony_id=9,
        team_id="team_123",
        channel_id="chan_abc",
        ceremony_type_label="Retrospective",
        amended_by_id=3,
        changes={"scheduled_at": START, "agenda": "Lessons"},
        reason=None,
        cancel=False,
        requires_confirmation=True,
        previous_local_display="Thursday 3 September 2026, 14:00 (Europe/Berlin)",
        previous_utc_display="2026-09-03 12:00 UTC",
        new_local_display="Friday 4 September 2026, 14:00 (Europe/Berlin)",
        new_utc_display="2026-09-04 12:00 UTC",
    )
    text = move.confirmation_question()
    assert "ceremony #9 (Retrospective)" in text
    assert "2026-09-03 12:00 UTC" in text and "2026-09-04 12:00 UTC" in text
    assert "set the agenda to: Lessons" in text
    cancel = AmendmentProposal(
        ceremony_id=9,
        team_id="team_123",
        channel_id="chan_abc",
        ceremony_type_label="Retrospective",
        amended_by_id=3,
        changes={"status": "cancelled"},
        reason="moved to next sprint",
        cancel=True,
        requires_confirmation=True,
        previous_local_display="Thursday 3 September 2026, 14:00 (Europe/Berlin)",
        previous_utc_display="2026-09-03 12:00 UTC",
    )
    assert cancel.describe().startswith("cancel ceremony #9")


# --- renderer -----------------------------------------------------------------------


def test_render_calendar_lists_every_field_in_both_zones():
    view = CalendarView(
        team_id="team_123",
        channel_id="chan_abc",
        entries=(
            CalendarEntry(ceremony(1, agenda="Standup"), "Daily Standup", "@alice"),
            CalendarEntry(
                ceremony(2, type_id=2, minutes=90, status=CeremonyStatus.CANCELLED.value), "Sprint Planning", "@bob"
            ),
        ),
        zone="Africa/Cairo",
        include_past=True,
        include_cancelled=True,
    )
    text = render_calendar(view)
    lines = text.splitlines()
    assert lines[0] == "All ceremonies (including cancelled) (times shown in Africa/Cairo and UTC):"
    assert lines[1] == (
        "- #1 Daily Standup — Friday 4 September 2026, 15:00 (Africa/Cairo) / 2026-09-04 12:00 UTC — 15 min — "
        "organiser @alice — agenda: Standup — status: scheduled"
    )
    assert "#2 Sprint Planning" in lines[2] and "90 min" in lines[2] and "status: cancelled" in lines[2]
    assert "agenda: no agenda" in lines[2]


def test_render_calendar_without_zone_shows_utc_only():
    view = CalendarView(
        team_id="team_123", channel_id="chan_abc", entries=(CalendarEntry(ceremony(1), "Daily Standup", "@a"),)
    )
    text = render_calendar(view)
    assert "times shown in UTC" in text
    assert "— 2026-09-04 12:00 UTC — 15 min" in text


def test_render_calendar_empty():
    view = CalendarView(team_id="team_123", channel_id="chan_abc")
    assert render_calendar(view) == "No upcoming ceremonies scheduled here."
