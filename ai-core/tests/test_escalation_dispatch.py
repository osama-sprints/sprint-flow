"""Tests for open_escalation (Task 0/1): channel-scoped routing, thread-aware
idempotency, and honest handling when a human is missing or unreachable.

Follows tests/test_escalation_closure.py's convention: no live database or
Mattermost connection, collaborators mocked at the module boundary,
asyncio.run() inside plain sync test functions.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.exc import IntegrityError

from app.core.langgraph.tools.results import ResultCode
from app.core.requester import RequesterContext
from app.models.enums import EscalationStatus, EscalationType, RoleKey
from app.services import escalation
from app.services.authorisation import ValidationFailed


def _ctx(**overrides) -> RequesterContext:
    defaults = dict(mattermost_user_id="learner-1", channel_id="channel-a", learner_thread_id="thread-1")
    defaults.update(overrides)
    return RequesterContext(**defaults)


def _learner(user_id: int = 1) -> SimpleNamespace:
    return SimpleNamespace(id=user_id, display_name="Nora", username="nora")


def _member(user_id: int, mm_id: str, role_key: str) -> SimpleNamespace:
    return SimpleNamespace(
        user=SimpleNamespace(id=user_id, mattermost_user_id=mm_id),
        role=SimpleNamespace(key=role_key),
    )


def _ticket(**overrides) -> SimpleNamespace:
    defaults = dict(
        ticket_ref="ESC-000001", status="open", assigned_human_id=None, ticket_type=EscalationType.TECH.value
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _mocks(**overrides) -> dict:
    """Safe defaults for every collaborator; override only what one test cares about."""
    base = dict(
        identity_repo=SimpleNamespace(get_user_by_mattermost_id=AsyncMock(return_value=_learner())),
        escalation_repo=SimpleNamespace(
            get_open_escalation_ticket_for_learner_thread=AsyncMock(return_value=None),
            create_escalation_ticket=AsyncMock(return_value=_ticket()),
            set_escalation_status=AsyncMock(),
        ),
        channel_repo=SimpleNamespace(list_channel_roles=AsyncMock(return_value=[])),
        mattermost_client=SimpleNamespace(
            create_direct_channel=AsyncMock(return_value={"id": "dm-1"}),
            create_post=AsyncMock(return_value={"id": "post-1"}),
        ),
    )
    base.update(overrides)
    return base


def _patched(**mocks):
    return patch.multiple(escalation, **mocks)


def _run(coro):
    return asyncio.run(coro)


def test_happy_path_routes_to_the_channels_tech_lead_and_hides_identity():
    lead = _member(2, "lead-mm", RoleKey.TECH_LEAD.value)
    mocks = _mocks(
        channel_repo=SimpleNamespace(list_channel_roles=AsyncMock(return_value=[lead])),
        escalation_repo=SimpleNamespace(
            get_open_escalation_ticket_for_learner_thread=AsyncMock(return_value=None),
            create_escalation_ticket=AsyncMock(return_value=_ticket(assigned_human_id=2)),
            set_escalation_status=AsyncMock(),
        ),
    )
    with _patched(**mocks):
        result = _run(
            escalation.open_escalation(
                "How many late days do I have left?", ticket_type=EscalationType.TECH, requester=_ctx()
            )
        )

    assert result.code == ResultCode.ESCALATION_OPENED
    assert result.ticket.ticket_ref in result.message
    assert "lead" not in result.message.lower() and "nora" not in result.message.lower()
    mocks["mattermost_client"].create_direct_channel.assert_awaited_once_with("lead-mm")
    mocks["escalation_repo"].set_escalation_status.assert_awaited_once()


def test_routes_only_to_the_requesting_channels_lead_not_another_channels():
    lead_a = _member(2, "lead-a-mm", RoleKey.TECH_LEAD.value)
    lead_b = _member(3, "lead-b-mm", RoleKey.TECH_LEAD.value)

    def roles_for(channel_id, *, active_only=True):
        return {"channel-a": [lead_a], "channel-b": [lead_b]}[channel_id]

    mocks = _mocks(
        channel_repo=SimpleNamespace(list_channel_roles=AsyncMock(side_effect=roles_for)),
        escalation_repo=SimpleNamespace(
            get_open_escalation_ticket_for_learner_thread=AsyncMock(return_value=None),
            create_escalation_ticket=AsyncMock(return_value=_ticket(assigned_human_id=2)),
            set_escalation_status=AsyncMock(),
        ),
    )
    with _patched(**mocks):
        _run(escalation.open_escalation("q", ticket_type=EscalationType.TECH, requester=_ctx(channel_id="channel-a")))

    assert mocks["escalation_repo"].create_escalation_ticket.call_args.kwargs["assigned_human_id"] == 2
    mocks["mattermost_client"].create_direct_channel.assert_awaited_once_with("lead-a-mm")


def test_no_human_in_role_still_opens_a_ticket_and_sends_no_dm():
    mocks = _mocks(
        escalation_repo=SimpleNamespace(
            get_open_escalation_ticket_for_learner_thread=AsyncMock(return_value=None),
            create_escalation_ticket=AsyncMock(return_value=_ticket(assigned_human_id=None)),
            set_escalation_status=AsyncMock(),
        ),
    )
    with _patched(**mocks):
        result = _run(escalation.open_escalation("q", requester=_ctx()))

    assert result.code == ResultCode.ESCALATION_OPENED_NO_HUMAN
    mocks["mattermost_client"].create_direct_channel.assert_not_called()
    mocks["escalation_repo"].set_escalation_status.assert_not_called()


def test_dm_failure_leaves_the_ticket_open_and_assigned_not_lost():
    mocks = _mocks(
        channel_repo=SimpleNamespace(
            list_channel_roles=AsyncMock(return_value=[_member(2, "lead-mm", RoleKey.TECH_LEAD.value)])
        ),
        escalation_repo=SimpleNamespace(
            get_open_escalation_ticket_for_learner_thread=AsyncMock(return_value=None),
            create_escalation_ticket=AsyncMock(return_value=_ticket(assigned_human_id=2)),
            set_escalation_status=AsyncMock(),
        ),
        mattermost_client=SimpleNamespace(create_direct_channel=AsyncMock(return_value=None), create_post=AsyncMock()),
    )
    with _patched(**mocks):
        result = _run(escalation.open_escalation("q", ticket_type=EscalationType.TECH, requester=_ctx()))

    assert result.code == ResultCode.ESCALATION_OPENED
    assert "couldn't reach" in result.message
    mocks["mattermost_client"].create_post.assert_not_called()
    mocks["escalation_repo"].set_escalation_status.assert_not_called()  # never claims "handed off" if it wasn't


def test_idempotent_replay_on_the_same_thread_does_not_reopen_or_re_dm():
    existing = _ticket(
        status=EscalationStatus.WAITING_HUMAN.value, assigned_human_id=2, ticket_type=EscalationType.TECH.value
    )
    mocks = _mocks(
        escalation_repo=SimpleNamespace(
            get_open_escalation_ticket_for_learner_thread=AsyncMock(return_value=existing),
            create_escalation_ticket=AsyncMock(),
            set_escalation_status=AsyncMock(),
        )
    )
    with _patched(**mocks):
        result = _run(
            escalation.open_escalation("same question again", ticket_type=EscalationType.TECH, requester=_ctx())
        )

    assert result.code == ResultCode.ESCALATION_ALREADY_OPEN
    assert existing.ticket_ref in result.message
    mocks["escalation_repo"].create_escalation_ticket.assert_not_called()
    mocks["mattermost_client"].create_direct_channel.assert_not_called()


def test_concurrent_trigger_loses_the_insert_race_and_reports_the_winner():
    winner = _ticket(status=EscalationStatus.WAITING_HUMAN.value, ticket_ref="ESC-000099")
    mocks = _mocks(
        escalation_repo=SimpleNamespace(
            get_open_escalation_ticket_for_learner_thread=AsyncMock(side_effect=[None, winner]),
            create_escalation_ticket=AsyncMock(side_effect=IntegrityError("insert", {}, Exception("dup key"))),
            set_escalation_status=AsyncMock(),
        ),
        channel_repo=SimpleNamespace(
            list_channel_roles=AsyncMock(return_value=[_member(2, "lead-mm", RoleKey.TECH_LEAD.value)])
        ),
    )
    with _patched(**mocks):
        result = _run(escalation.open_escalation("q", ticket_type=EscalationType.TECH, requester=_ctx()))

    assert result.code == ResultCode.ESCALATION_ALREADY_OPEN
    assert winner.ticket_ref in result.message
    mocks["mattermost_client"].create_direct_channel.assert_not_called()  # the race loser never double-DMs


def test_empty_question_is_rejected_before_any_side_effect():
    mocks = _mocks()
    with _patched(**mocks), pytest.raises(ValidationFailed):
        _run(escalation.open_escalation("   ", requester=_ctx()))
    mocks["identity_repo"].get_user_by_mattermost_id.assert_not_called()


def test_missing_channel_id_is_rejected():
    mocks = _mocks()
    with _patched(**mocks), pytest.raises(ValidationFailed):
        _run(escalation.open_escalation("q", requester=_ctx(channel_id="")))


def test_unsynced_learner_raises_instead_of_silently_escalating():
    mocks = _mocks(identity_repo=SimpleNamespace(get_user_by_mattermost_id=AsyncMock(return_value=None)))
    with _patched(**mocks), pytest.raises(RuntimeError):
        _run(escalation.open_escalation("q", requester=_ctx()))
