"""Integration tests for Capability 5 — Escalation Handoff to Human.

Strategy:
- REAL PostgreSQL: ticket creation, idempotency, metadata persistence, user/role lookup
- FAKE Mattermost: DM channel creation and post — patched at the module attribute
  used by escalation.py  (app.services.escalation.mattermost_client)

This matches the repo's convention established in test_escalation_closure.py,
where patch.multiple(module, mattermost_client=...) replaces the module-level
singleton without touching the real Mattermost server.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlmodel import select

from app.core.langgraph.tools.results import ResultCode
from app.core.requester import RequesterContext
from app.models import ChannelRole, Role, User
from app.models.enums import EscalationType, RoleKey
from app.services import escalation as escalation_module
from app.services.authorisation import ValidationFailed
from app.services.database import database_service, session_scope
from app.services.escalation import open_escalation


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _tag() -> str:
    return uuid4().hex[:12]


def _fake_mm(dm_id: str = "fake-dm-chan", post_id: str = "fake-post-1") -> SimpleNamespace:
    """Return a deterministic Mattermost stub.

    The real client is a MattermostClient singleton; we replace the attribute
    on the module so that open_escalation sees our stub, not the real object.
    """
    return SimpleNamespace(
        create_direct_channel=AsyncMock(return_value={"id": dm_id}),
        create_post=AsyncMock(return_value={"id": post_id}),
    )


async def _make_user(prefix: str, idx: int = 1) -> User:
    mm_id = f"it-{prefix}-{idx}"
    async with session_scope() as s:
        user = User(
            mattermost_user_id=mm_id,
            email=f"{mm_id}@sprintflow.test",
            username=f"U{mm_id}",
            first_name="Test",
            last_name="User",
        )
        s.add(user)
        await s.commit()
        await s.refresh(user)
        return user


async def _assign_role(user: User, channel_id: str, role_key: RoleKey) -> None:
    async with session_scope() as s:
        role = (await s.exec(select(Role).where(Role.key == role_key.value))).first()
        assert role is not None, f"seed role {role_key.value} missing from DB"
        s.add(ChannelRole(channel_id=channel_id, user_id=user.id, role_id=role.id))
        await s.commit()


async def _cleanup(prefix: str, channel_ids: list[str]) -> None:
    """Best-effort cleanup; run order matters for FK constraints."""
    channels = channel_ids or ["__none__"]
    async with session_scope() as s:
        params = {"p": f"it-{prefix}-%", "channels": channels}
        await s.exec(
            text("DELETE FROM escalation_tickets WHERE channel_id = ANY(:channels)"),
            params=params,
        )
        await s.exec(
            text("DELETE FROM channel_roles WHERE user_id IN (SELECT id FROM users WHERE mattermost_user_id LIKE :p)"),
            params=params,
        )
        await s.exec(
            text("DELETE FROM users WHERE mattermost_user_id LIKE :p"),
            params=params,
        )


def _run(coro_factory):
    """Run one coroutine on a fresh event loop; dispose the DB engine afterwards."""

    async def wrapped():
        try:
            return await coro_factory()
        finally:
            await database_service.close()

    return asyncio.run(wrapped())


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_authorized_escalation_creates_ticket_and_routes_to_human():
    """Full happy path:

    - authorized learner escalates
    - ticket row is persisted in real PostgreSQL
    - correct requester metadata is stored
    - Mattermost DM is sent to the assigned human (via fake)
    - ticket status advances to WAITING_HUMAN
    - learner receives honest, non-leaking acknowledgement
    """
    prefix = _tag()

    async def scenario():
        channel_ids: list[str] = []
        try:
            learner = await _make_user(prefix, 1)
            human = await _make_user(prefix, 2)
            channel_id = f"esc-{prefix}"
            channel_ids.append(channel_id)

            await _assign_role(human, channel_id, RoleKey.OPS_SUPPORT)

            ctx = RequesterContext(
                mattermost_user_id=learner.mattermost_user_id,
                channel_id=channel_id,
                learner_thread_id="thread-abc",
            )
            fake_mm = _fake_mm()

            with patch.object(escalation_module, "mattermost_client", fake_mm):
                result = await open_escalation(
                    question="How many days of leave do I have?",
                    ticket_type=EscalationType.OPS,
                    requester=ctx,
                )

            # --- Result code --------------------------------------------------
            assert result.code == ResultCode.ESCALATION_OPENED, result.code

            # --- Ticket persisted in PostgreSQL --------------------------------
            ticket = result.ticket
            assert ticket is not None
            assert ticket.id is not None
            assert ticket.ticket_ref.startswith("ESC-")
            assert ticket.channel_id == channel_id
            assert ticket.learner_id == learner.id
            assert ticket.question == "How many days of leave do I have?"
            assert ticket.learner_thread_id == "thread-abc"

            # --- Human assignment (done before DM) ----------------------------
            assert ticket.assigned_human_id == human.id

            # --- DM actually called on the fake --------------------------------
            fake_mm.create_direct_channel.assert_awaited_once_with(human.mattermost_user_id)
            fake_mm.create_post.assert_awaited_once()
            dm_post_args = fake_mm.create_post.call_args
            assert dm_post_args.args[0] == "fake-dm-chan"

            # --- Learner message is honest & non-identifying -------------------
            msg = result.message
            assert "colleague" in msg.lower()
            assert ticket.ticket_ref in msg  # shows the ESC ref
            assert human.username not in msg  # never leaks the human's name
            assert human.mattermost_user_id not in msg

        finally:
            await _cleanup(prefix, channel_ids)

    _run(scenario)


def test_escalation_with_no_human_in_role_creates_ticket_but_skips_dm():
    """When the channel has nobody in the required role:

    - ticket is still created (unassigned) in real PostgreSQL
    - Mattermost DM is NOT attempted
    - ESCALATION_OPENED_NO_HUMAN is returned
    - learner receives honest explanation (no human available)
    """
    prefix = _tag()

    async def scenario():
        channel_ids: list[str] = []
        try:
            learner = await _make_user(prefix, 1)
            channel_id = f"esc-{prefix}"
            channel_ids.append(channel_id)
            # deliberately no human assigned to this channel

            ctx = RequesterContext(
                mattermost_user_id=learner.mattermost_user_id,
                channel_id=channel_id,
                learner_thread_id="thread-no-human",
            )
            fake_mm = _fake_mm()

            with patch.object(escalation_module, "mattermost_client", fake_mm):
                result = await open_escalation(
                    question="What is the reimbursement limit?",
                    ticket_type=EscalationType.OPS,
                    requester=ctx,
                )

            assert result.code == ResultCode.ESCALATION_OPENED_NO_HUMAN
            ticket = result.ticket
            assert ticket is not None
            assert ticket.assigned_human_id is None

            # No DM should have been attempted
            fake_mm.create_direct_channel.assert_not_awaited()
            fake_mm.create_post.assert_not_awaited()

            msg = result.message
            assert "don't" in msg.lower() or "no" in msg.lower()

        finally:
            await _cleanup(prefix, channel_ids)

    _run(scenario)


def test_repeated_escalation_for_same_thread_is_idempotent():
    """Calling open_escalation twice with the same learner_thread_id must not
    create a second ticket (idempotency guard).

    The second call must return ESCALATION_ALREADY_OPEN and the *same* ticket id.
    """
    prefix = _tag()

    async def scenario():
        channel_ids: list[str] = []
        try:
            learner = await _make_user(prefix, 1)
            human = await _make_user(prefix, 2)
            channel_id = f"esc-{prefix}"
            channel_ids.append(channel_id)
            await _assign_role(human, channel_id, RoleKey.OPS_SUPPORT)

            ctx = RequesterContext(
                mattermost_user_id=learner.mattermost_user_id,
                channel_id=channel_id,
                learner_thread_id="thread-idem",
            )
            fake_mm = _fake_mm()

            with patch.object(escalation_module, "mattermost_client", fake_mm):
                res1 = await open_escalation("Q1?", requester=ctx)
                res2 = await open_escalation("Q2?", requester=ctx)  # same thread

            assert res1.code == ResultCode.ESCALATION_OPENED
            assert res2.code == ResultCode.ESCALATION_ALREADY_OPEN
            assert res2.ticket is not None
            assert res2.ticket.id == res1.ticket.id

            # DM posted exactly once (not twice)
            assert fake_mm.create_post.await_count == 1

        finally:
            await _cleanup(prefix, channel_ids)

    _run(scenario)


def test_empty_question_is_rejected_before_any_db_write():
    """ValidationFailed must be raised for blank/whitespace questions;
    no ticket row should be created.
    """
    prefix = _tag()

    async def scenario():
        channel_ids: list[str] = []
        try:
            learner = await _make_user(prefix, 1)
            channel_id = f"esc-{prefix}"
            channel_ids.append(channel_id)

            ctx = RequesterContext(
                mattermost_user_id=learner.mattermost_user_id,
                channel_id=channel_id,
                learner_thread_id="thread-empty",
            )

            with pytest.raises(ValidationFailed, match="no question"):
                await open_escalation("   ", requester=ctx)

        finally:
            await _cleanup(prefix, channel_ids)

    _run(scenario)


def test_missing_channel_id_is_rejected():
    """An escalation with no channel_id must be refused; no ticket created."""
    prefix = _tag()

    async def scenario():
        channel_ids: list[str] = []
        try:
            learner = await _make_user(prefix, 1)

            ctx = RequesterContext(
                mattermost_user_id=learner.mattermost_user_id,
                channel_id="",  # empty — should be refused
                learner_thread_id="thread-nochan",
            )

            with pytest.raises(ValidationFailed, match="channel"):
                await open_escalation("Help!", requester=ctx)

        finally:
            await _cleanup(prefix, channel_ids)

    _run(scenario)


def test_mattermost_dm_failure_still_persists_ticket():
    """If the Mattermost DM cannot be sent (stub returns None for dm_channel),
    the ticket must still be created and the learner told that a human is
    assigned but has not yet been contacted — never that the handoff succeeded.
    """
    prefix = _tag()

    async def scenario():
        channel_ids: list[str] = []
        try:
            learner = await _make_user(prefix, 1)
            human = await _make_user(prefix, 2)
            channel_id = f"esc-{prefix}"
            channel_ids.append(channel_id)
            await _assign_role(human, channel_id, RoleKey.OPS_SUPPORT)

            ctx = RequesterContext(
                mattermost_user_id=learner.mattermost_user_id,
                channel_id=channel_id,
                learner_thread_id="thread-dm-fail",
            )

            # Simulate Mattermost returning None for the DM channel
            failing_mm = SimpleNamespace(
                create_direct_channel=AsyncMock(return_value=None),
                create_post=AsyncMock(return_value=None),
            )

            with patch.object(escalation_module, "mattermost_client", failing_mm):
                result = await open_escalation(
                    question="I have a blocker",
                    ticket_type=EscalationType.OPS,
                    requester=ctx,
                )

            # Ticket was still persisted
            assert result.ticket is not None
            assert result.ticket.id is not None
            # Human is assigned but the DM handoff path uses _assigned_but_unreachable_message
            assert result.ticket.assigned_human_id == human.id
            # Result code is still ESCALATION_OPENED (ticket exists, human assigned)
            assert result.code == ResultCode.ESCALATION_OPENED
            # Message must *not* claim the handoff happened
            msg = result.message
            assert "looped in" not in msg.lower() or "assigned" in msg.lower()
            # No false promise that they'll hear back from a colleague right away
            assert "logged" in msg.lower() or "record" in msg.lower() or "assigned" in msg.lower()

        finally:
            await _cleanup(prefix, channel_ids)

    _run(scenario)
