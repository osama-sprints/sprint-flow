"""Pure-logic tests for channel standup summaries."""

import asyncio
from datetime import date
from types import SimpleNamespace

import pytest

from app.core.requester import RequesterContext, current_requester
from app.models import DailyStandup
from app.services import authorisation
from app.services.domain import standups


@pytest.fixture(autouse=True)
def bound_requester():
    token = current_requester.set(RequesterContext(mattermost_user_id="requester", channel_id="channel-1"))
    yield
    current_requester.reset(token)


def _member(user_id: int, username: str, display_name: str | None = None):
    return SimpleNamespace(
        user=SimpleNamespace(id=user_id, username=username, display_name=display_name),
        role=SimpleNamespace(key="learner"),
    )


def _sprint():
    return SimpleNamespace(
        id=7,
        name="Sprint 1",
        start_date=date(2026, 9, 14),
        end_date=date(2026, 9, 25),
    )


def test_non_member_is_rejected_before_summary_reads(monkeypatch):
    async def reject(*args, **kwargs):
        raise authorisation.AuthorisationRefused("not_a_member", action="summarize_standups")

    async def should_not_read(*args, **kwargs):
        raise AssertionError("summary data must not be read after authorization fails")

    monkeypatch.setattr(authorisation, "require_channel_authority", reject)
    monkeypatch.setattr(standups.sprints, "get_active_sprint", should_not_read)

    with pytest.raises(authorisation.AuthorisationRefused):
        asyncio.run(standups.get_standup_summary_for_channel("channel-1", date(2026, 9, 15)))

def test_summary_reports_submitted_and_missing_active_members(monkeypatch):
    async def allow(*args, **kwargs):
        return SimpleNamespace(allowed=True)

    async def active_sprint(*args, **kwargs):
        return _sprint()

    async def active_members(*args, **kwargs):
        return [_member(1, "alice", "Alice"), _member(2, "bob", "Bob")]

    async def stored_entries(*args, **kwargs):
        return [
            DailyStandup(
                id=10,
                sprint_id=7,
                learner_id=1,
                log_date=date(2026, 9, 15),
                what_i_did="Finished API work",
                what_i_will_do="Add tests",
                blockers=None,
            ),
            DailyStandup(
                id=11,
                sprint_id=7,
                learner_id=99,
                log_date=date(2026, 9, 15),
                what_i_did="Outsider entry",
                what_i_will_do="N/A",
                blockers="N/A",
            ),
        ]

    monkeypatch.setattr(authorisation, "require_channel_authority", allow)
    monkeypatch.setattr(standups.sprints, "get_active_sprint", active_sprint)
    monkeypatch.setattr(standups.channels, "list_channel_roles", active_members)
    monkeypatch.setattr(standups, "list_daily_standups", stored_entries)

    summary = asyncio.run(standups.get_standup_summary_for_channel("channel-1", date(2026, 9, 15)))

    assert summary.submitted_updates == [
        {
            "learner_id": 1,
            "what_i_did": "Finished API work",
            "what_i_will_do": "Add tests",
            "blockers": None,
        }
    ]
    assert summary.missing_members == [{"user_id": 2, "username": "bob", "display_name": "Bob"}]


@pytest.mark.parametrize("target_date", [date(2026, 9, 13), date(2026, 9, 26)])
def test_summary_rejects_date_outside_active_sprint(monkeypatch, target_date):
    async def allow(*args, **kwargs):
        return SimpleNamespace(allowed=True)

    async def active_sprint(*args, **kwargs):
        return _sprint()

    monkeypatch.setattr(authorisation, "require_channel_authority", allow)
    monkeypatch.setattr(standups.sprints, "get_active_sprint", active_sprint)

    with pytest.raises(authorisation.ValidationFailed, match="outside the active sprint window"):
        asyncio.run(standups.get_standup_summary_for_channel("channel-1", target_date))


def test_learner_role_is_rejected(monkeypatch):
    async def reject(*args, **kwargs):
        assert kwargs["allowed_roles"] == authorisation.CHANNEL_ADMIN_ROLES
        raise authorisation.AuthorisationRefused("role_not_permitted:learner", action="summarize_standups")

    monkeypatch.setattr(authorisation, "require_channel_authority", reject)

    with pytest.raises(authorisation.AuthorisationRefused, match="administrative permissions"):
        asyncio.run(standups.get_standup_summary_for_channel("channel-1", date(2026, 9, 15)))


def test_summary_isolated_by_channel_and_sprint(monkeypatch):
    calls: list[str] = []

    async def allow(*args, **kwargs):
        assert kwargs["allowed_roles"] == authorisation.CHANNEL_ADMIN_ROLES
        return SimpleNamespace(allowed=True)

    async def active_sprint(channel_id, **kwargs):
        calls.append(f"sprint:{channel_id}")
        return SimpleNamespace(
            id=7 if channel_id == "channel-1" else 8,
            name=f"Sprint for {channel_id}",
            start_date=date(2026, 9, 14),
            end_date=date(2026, 9, 25),
        )

    async def active_members(channel_id, **kwargs):
        calls.append(f"members:{channel_id}")
        return [_member(1 if channel_id == "channel-1" else 2, channel_id)]

    async def stored_entries(sprint_id, **kwargs):
        calls.append(f"standups:{sprint_id}")
        return [
            DailyStandup(
                id=sprint_id,
                sprint_id=sprint_id,
                learner_id=1 if sprint_id == 7 else 2,
                log_date=date(2026, 9, 15),
                what_i_did=f"Update {sprint_id}",
                what_i_will_do="Next",
                blockers=None,
            )
        ]

    monkeypatch.setattr(authorisation, "require_channel_authority", allow)
    monkeypatch.setattr(standups.sprints, "get_active_sprint", active_sprint)
    monkeypatch.setattr(standups.channels, "list_channel_roles", active_members)
    monkeypatch.setattr(standups, "list_daily_standups", stored_entries)

    first = asyncio.run(standups.get_standup_summary_for_channel("channel-1", date(2026, 9, 15)))
    second = asyncio.run(standups.get_standup_summary_for_channel("channel-2", date(2026, 9, 15)))

    assert first.submitted_updates[0]["learner_id"] == 1
    assert second.submitted_updates[0]["learner_id"] == 2
    assert first.sprint_info["id"] == 7
    assert second.sprint_info["id"] == 8
    assert calls == [
        "sprint:channel-1",
        "members:channel-1",
        "standups:7",
        "sprint:channel-2",
        "members:channel-2",
        "standups:8",
    ]


def test_summary_returns_all_active_members_when_no_one_submitted(monkeypatch):
    async def allow(*args, **kwargs):
        return SimpleNamespace(allowed=True)

    async def active_sprint(*args, **kwargs):
        return _sprint()

    async def active_members(*args, **kwargs):
        return [_member(1, "alice", "Alice"), _member(2, "bob", "Bob")]

    async def no_entries(*args, **kwargs):
        return []

    monkeypatch.setattr(authorisation, "require_channel_authority", allow)
    monkeypatch.setattr(standups.sprints, "get_active_sprint", active_sprint)
    monkeypatch.setattr(standups.channels, "list_channel_roles", active_members)
    monkeypatch.setattr(standups, "list_daily_standups", no_entries)

    summary = asyncio.run(standups.get_standup_summary_for_channel("channel-1", date(2026, 9, 15)))

    assert summary.submitted_updates == []
    assert summary.missing_members == [
        {"user_id": 1, "username": "alice", "display_name": "Alice"},
        {"user_id": 2, "username": "bob", "display_name": "Bob"},
    ]