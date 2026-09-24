"""Pure-logic tests for proactive standups: timezones, parsing, acknowledgement filtering.

No database, no network. DB-backed behaviour lives in
``tests/integration/test_standups_db.py``.
"""

from datetime import (
    UTC,
    date,
    datetime,
)

import pytest

from app.models import User
from app.services.standups import (
    ParsedStandup,
    backoff_seconds,
    day_end_utc,
    dispatch_at_for,
    is_ack_only,
    local_date_of,
    parse_standup_reply,
    render_prompt,
    resolve_timezone,
    starts_with_mention,
)


def make_user(username: str = "sara", timezone: str | None = "UTC") -> User:
    return User(id=1, mattermost_user_id="mm-sara", username=username, timezone=timezone)


# ---------------------------------------------------------------------------
# Timezones
# ---------------------------------------------------------------------------


def test_resolve_timezone_keeps_valid_stored_zone():
    assert resolve_timezone(make_user(timezone="Africa/Cairo")) == "Africa/Cairo"


def test_resolve_timezone_falls_back_to_utc_for_garbage():
    assert resolve_timezone(make_user(timezone="Not/AZone")) == "UTC"
    assert resolve_timezone(make_user(timezone="")) == "UTC"
    assert resolve_timezone(make_user(timezone=None)) == "UTC"


def test_local_date_of_utc():
    instant = datetime(2026, 9, 15, 23, 30, tzinfo=UTC)
    assert local_date_of(instant, "UTC") == date(2026, 9, 15)


def test_local_date_of_goes_back_a_day_west_of_utc():
    instant = datetime(2026, 9, 15, 6, 30, tzinfo=UTC)
    assert local_date_of(instant, "America/New_York") == date(2026, 9, 15)


def test_local_date_of_rolls_forward_a_day_east_of_utc():
    instant = datetime(2026, 9, 15, 20, 0, tzinfo=UTC)
    assert local_date_of(instant, "Asia/Tokyo") == date(2026, 9, 16)


def test_dispatch_at_for_zone_west_of_utc_same_utc_day():
    assert dispatch_at_for(date(2026, 9, 15), "America/Los_Angeles", 9) == datetime(2026, 9, 15, 16, 0, tzinfo=UTC)


def test_dispatch_at_for_eastern_standard():
    assert dispatch_at_for(date(2026, 1, 15), "America/New_York", 9) == datetime(2026, 1, 15, 14, 0, tzinfo=UTC)


def test_dispatch_at_for_eastern_daylight():
    assert dispatch_at_for(date(2026, 7, 15), "America/New_York", 9) == datetime(2026, 7, 15, 13, 0, tzinfo=UTC)


def test_day_end_utc_is_start_of_next_day_in_zone():
    assert day_end_utc(date(2026, 1, 15), "America/New_York") == datetime(2026, 1, 16, 5, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def test_parse_numbered_three_sections():
    parsed = parse_standup_reply("1. Wrote the parser\n2. Fix the catalog bug\n3. Waiting on the DB schema")
    assert parsed == ParsedStandup(
        what_i_did="Wrote the parser",
        what_i_will_do="Fix the catalog bug",
        blockers="Waiting on the DB schema",
    )


def test_parse_numbered_accepts_mixed_separators():
    parsed = parse_standup_reply("1) shipped\n2: fix flaky test\n3. none")
    assert parsed.what_i_did == "shipped"
    assert parsed.what_i_will_do == "fix flaky test"
    assert parsed.blockers == "none"


def test_parse_labeled_sections():
    parsed = parse_standup_reply("done: shipped the collector\nplan: run it on the cluster\nblocked: no permissions")
    assert parsed.what_i_did == "shipped the collector"
    assert parsed.what_i_will_do == "run it on the cluster"
    assert parsed.blockers == "no permissions"


def test_parse_labeled_sections_multiline():
    parsed = parse_standup_reply(
        "Done\n- wrote tests\n- added migration\nPlanned\n- final review\nBlockers:\n- waiting on reviewer"
    )
    assert parsed.what_i_did == "- wrote tests\n- added migration"
    assert parsed.what_i_will_do == "- final review"
    assert parsed.blockers == "- waiting on reviewer"


def test_parse_flat_fallback_when_no_structure():
    parsed = parse_standup_reply("Just kept debugging the reconnect loop all day.")
    assert parsed.what_i_did == "Just kept debugging the reconnect loop all day."
    assert parsed.what_i_will_do == ""
    assert parsed.blockers is None


def test_parse_two_line_numbered_leaves_missing_section_empty():
    parsed = parse_standup_reply("1. Shipped\n2. nothing")
    assert parsed.what_i_did == "Shipped"
    assert parsed.what_i_will_do == "nothing"
    assert parsed.blockers is None


def test_parse_blank_reply():
    assert parse_standup_reply("   ") == ParsedStandup("", "", None)
    assert parse_standup_reply("") == ParsedStandup("", "", None)


# ---------------------------------------------------------------------------
# Parsing — unexpected user input battery
# ---------------------------------------------------------------------------


def test_parse_numbered_items_beyond_the_third_are_ignored():
    parsed = parse_standup_reply("1. shipped\n2. review\n3. none\n4. actually db again")
    assert parsed == ParsedStandup("shipped", "review", "none")


def test_parse_numbered_double_digit_item_keeps_its_digit():
    parsed = parse_standup_reply("1. shipped\n2. review\n10. waiting on CI")
    assert parsed.what_i_did == "shipped"
    assert parsed.what_i_will_do == "review"
    assert parsed.blockers == "waiting on CI"


def test_parse_numbered_tab_indented():
    parsed = parse_standup_reply("\t1. shipped\n\t2. review")
    assert parsed.what_i_did == "shipped"
    assert parsed.what_i_will_do == "review"


def test_parse_numbered_then_blocker_label_starts_the_third_slot():
    parsed = parse_standup_reply("1. shipped\n2. review\nblockers: db down")
    assert parsed.what_i_did == "shipped"
    assert parsed.what_i_will_do == "review"
    assert parsed.blockers == "db down"


def test_parse_numbered_blocked_label_carries_continuation_lines():
    parsed = parse_standup_reply("1. shipped\n2. review\nblocked: api\nretries exhausted")
    assert parsed.what_i_did == "shipped"
    assert parsed.what_i_will_do == "review"
    assert parsed.blockers == "api\nretries exhausted"


def test_parse_markdown_bold_wrapped_numbered():
    parsed = parse_standup_reply("**1. shipped**\n**2. review**\n**3. none**")
    assert parsed == ParsedStandup("shipped", "review", "none")


def test_parse_markdown_bold_wrapped_blocker_text():
    parsed = parse_standup_reply("1. shipped\n2. review\n3. **db down**")
    assert parsed.blockers == "db down"


def test_parse_backtick_wrapped_labels():
    parsed = parse_standup_reply("`done:` fixed flaky test\n`plan:` deploy")
    assert parsed.what_i_did == "fixed flaky test"
    assert parsed.what_i_will_do == "deploy"


def test_parse_uppercase_labels():
    parsed = parse_standup_reply("DONE: x\nPLAN: y\nBLOCKERS: z")
    assert parsed == ParsedStandup("x", "y", "z")


def test_parse_single_numbered_item_falls_back_to_flat():
    parsed = parse_standup_reply("1. progress line")
    assert parsed.what_i_did == "1. progress line"
    assert parsed.what_i_will_do == ""


def test_parse_emoji_heavy_reply_is_not_lost():
    parsed = parse_standup_reply("🚀 shipped the pipeline ✨\ncelebrating")
    assert parsed.what_i_did == "🚀 shipped the pipeline ✨\ncelebrating"
    assert parsed.what_i_will_do == ""


def test_parse_leading_blank_lines_do_not_hide_numbered():
    parsed = parse_standup_reply("\n\n1. x\n2. y\n3. z")
    assert parsed == ParsedStandup("x", "y", "z")


def test_parse_numbered_zero_leading_item_is_tolerated():
    parsed = parse_standup_reply("0. misc\n1. main")
    assert parsed.what_i_did == "misc"
    assert parsed.what_i_will_do == "main"


def test_parse_numbered_empty_first_section_stores_truthfully():
    parsed = parse_standup_reply("1.\n2. y\n3. z")
    assert parsed.what_i_did == ""
    assert parsed.what_i_will_do == "y"
    assert parsed.blockers == "z"


def test_parse_labeled_only_a_blocker():
    parsed = parse_standup_reply("blocked: waiting on CI signoff")
    assert parsed.what_i_did == ""
    assert parsed.what_i_will_do == ""
    assert parsed.blockers == "waiting on CI signoff"


def test_parse_labeled_space_before_colon():
    parsed = parse_standup_reply("done : x\nplan : y")
    assert parsed.what_i_did == "x"
    assert parsed.what_i_will_do == "y"


def test_parse_flat_fallback_keeps_the_raw_markdown():
    parsed = parse_standup_reply("**great progress**")
    assert parsed.what_i_did == "**great progress**"


def test_parse_german_date_word_is_flat_not_an_error():
    parsed = parse_standup_reply("Heute den Collector fertig gebaut")
    assert parsed.what_i_did == "Heute den Collector fertig gebaut"


def test_parse_numbered_with_parenthetical_is_flat():
    parsed = parse_standup_reply("(1) did this\n(2) next")
    assert parsed.what_i_did == "(1) did this\n(2) next"
    assert parsed.what_i_will_do == ""


# ---------------------------------------------------------------------------
# Acknowledgement and mention filtering
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "ok",
        "OK",
        "thanks!",
        "thank you",
        "got it",
        "done",
        "sure thing",
        "K",
        "k.",
        "thx :)",
        "well noted",
        "fine.",
        "okay!",
    ],
)
def test_is_ack_only(text: str):
    assert is_ack_only(text)


@pytest.mark.parametrize(
    "text",
    [
        "1. did onboarding\n2. next: review\n3. none",
        "Some progress",
        "blocked on the api",
        "10/10",
        "done but tired",
        "👍",
    ],
)
def test_is_not_ack_only(text: str):
    assert not is_ack_only(text)


def test_starts_with_mention():
    assert starts_with_mention("@assistant fix the bug", "assistant")
    assert starts_with_mention("@assistant", "assistant")
    assert not starts_with_mention("1. did work\n2. plan\n3. none", "assistant")
    assert not starts_with_mention("thanks @assistant", "assistant")  # mention is not the first word


def test_render_prompt_asks_three_questions():
    body = render_prompt(make_user(username="sara", timezone="UTC"))
    assert body.strip()
    assert "What did you do?" in body
    assert "What will you do next?" in body
    assert "Any blockers?" in body


# ---------------------------------------------------------------------------
# Backoff
# ---------------------------------------------------------------------------


def test_backoff_seconds_geometric_and_capped():
    assert backoff_seconds(0, base_seconds=60, cap_seconds=600) == 60
    assert backoff_seconds(1, base_seconds=60, cap_seconds=600) == 120
    assert backoff_seconds(2, base_seconds=60, cap_seconds=600) == 240
    assert backoff_seconds(9, base_seconds=60, cap_seconds=600) == 600
    assert backoff_seconds(0, base_seconds=1, cap_seconds=600) >= 1
