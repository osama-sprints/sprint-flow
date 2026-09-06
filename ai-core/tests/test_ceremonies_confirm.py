"""The scheduling tools' confirmation helper verifies the answer belongs to its own question.

LangGraph matches resumes to interrupts by POSITION within a task, so ``_confirm``
must raise exactly one interrupt per call: a second one would shift every later
question's answer by a slot and could consume a reply meant for another tool call.
Anything that is not this question's own echoed payload therefore commits nothing.
"""

from datetime import UTC, datetime

import pytest

from app.core.langgraph.tools import ceremonies

INSTANT = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)


def echo(payload, reply):
    """Build the resume value the conversation layer produces (see graph.resume_value)."""
    return {"reply": reply, "interrupt": payload}


def test_matching_echo_is_parsed(monkeypatch: pytest.MonkeyPatch):
    seen: list[object] = []

    def fake_interrupt(payload):
        seen.append(payload)
        return echo(payload, "yes")

    monkeypatch.setattr(ceremonies, "interrupt", fake_interrupt)
    assert ceremonies._confirm("Book it?", scheduled_at=INSTANT) is True
    assert seen == [{"question": "Book it?", "scheduled_at": INSTANT.isoformat()}]


def test_matching_echo_declines(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(ceremonies, "interrupt", lambda payload: echo(payload, "nope"))
    assert ceremonies._confirm("Book it?", scheduled_at=INSTANT) is False


def test_unclear_reply_is_none(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(ceremonies, "interrupt", lambda payload: echo(payload, "maybe later"))
    assert ceremonies._confirm("Book it?", scheduled_at=None) is None


def test_mismatched_instant_commits_nothing_and_does_not_re_ask(monkeypatch: pytest.MonkeyPatch):
    """A 'yes' carrying a different instant (a date rollover) must not commit — and must not interrupt again."""
    calls: list[dict] = []

    def fake_interrupt(payload):
        calls.append(payload)
        stale = dict(payload, scheduled_at="2026-09-03T12:00:00+00:00")
        return echo(stale, "yes")

    monkeypatch.setattr(ceremonies, "interrupt", fake_interrupt)
    assert ceremonies._confirm("Book it?", scheduled_at=INSTANT) is None
    assert len(calls) == 1, "a second interrupt() would shift every later resume in the task"


def test_plain_string_resume_is_not_treated_as_an_answer(monkeypatch: pytest.MonkeyPatch):
    """A bare string is someone else's resume (or a caller bypassing the layer): commit nothing."""
    calls: list[dict] = []

    def fake_interrupt(payload):
        calls.append(payload)
        return "yes"

    monkeypatch.setattr(ceremonies, "interrupt", fake_interrupt)
    assert ceremonies._confirm("Book it?", scheduled_at=INSTANT) is None
    assert len(calls) == 1
