"""Execution tracking: the durable row, cancel and retry for a running turn.

Mattermost and the store are stood in for at their module boundaries; the
behaviour pinned here is the state machine and the authorisation, not the
transport. Nothing here posts progress to the chat: the only messages a turn
produces are its reply, or a single "Stopped" / error reply.
"""

import asyncio
from datetime import datetime
from types import SimpleNamespace

import pytest

from app.core.config import settings
from app.models import Execution
from app.services import (
    execution_control,
    executions,
)
from app.services.domain import executions as store_module


class FakeStore:
    """In-memory stand-in for the executions store."""

    RUNNING = store_module.RUNNING
    SUCCEEDED = store_module.SUCCEEDED
    FAILED = store_module.FAILED
    CANCELLED = store_module.CANCELLED
    FINISHED = store_module.FINISHED

    def __init__(self) -> None:
        self.rows: dict[str, Execution] = {}

    async def create_execution(self, row, *, session=None):
        self.rows[row.id] = row
        return row

    async def get_execution(self, execution_id, *, session=None):
        return self.rows.get(execution_id)

    async def update_execution(self, execution_id, *, session=None, **fields):
        row = self.rows.get(execution_id)
        if row is None:
            return None
        for name, value in fields.items():
            setattr(row, name, value)
        return row

    async def request_cancel(self, execution_id, by, *, session=None):
        return await self.update_execution(execution_id, cancel_requested_at=datetime.now(), cancel_requested_by=by)

    async def find_running_for_session(self, session_id, *, session=None):
        return [r for r in self.rows.values() if r.session_id == session_id and r.status == "running"]

    async def latest_for_session(self, session_id, statuses=store_module.FINISHED, *, session=None):
        rows = [r for r in self.rows.values() if r.session_id == session_id and r.status in statuses]
        return rows[-1] if rows else None


class FakeClient:
    """Records Mattermost post traffic, so a test can assert a turn posts nothing of its own."""

    def __init__(self) -> None:
        self.created: list[dict] = []
        self.updated: list[dict] = []
        self.deleted: list[str] = []

    async def create_post(self, channel_id, message, root_id=None, *, post_type=None, props=None, file_ids=None):
        self.created.append(
            {"channel_id": channel_id, "message": message, "root_id": root_id, "post_type": post_type, "props": props}
        )
        return {"id": f"status-{len(self.created)}"}

    async def update_post(self, post_id, *, message=None, props=None, file_ids=None):
        self.updated.append({"post_id": post_id, "message": message, "props": props})
        return {"id": post_id}

    async def delete_post(self, post_id):
        self.deleted.append(post_id)
        return True


@pytest.fixture
def world(monkeypatch):
    store = FakeStore()
    client = FakeClient()
    monkeypatch.setattr(executions, "store", store)
    monkeypatch.setattr(execution_control, "store", store)
    monkeypatch.setattr(execution_control, "mattermost_client", client)
    monkeypatch.setattr(settings, "EXECUTION_TRACKING_ENABLED", True)
    monkeypatch.setattr(settings, "EXECUTION_CANCEL_POLL_SECONDS", 0.05)

    async def not_admin(_):
        return False

    monkeypatch.setattr(executions, "_is_admin", not_admin)
    executions._active.clear()
    executions.current_execution.set(None)
    return SimpleNamespace(store=store, client=client)


def _begin(world, **overrides):
    fields = dict(
        execution_id="exec-1",
        session_id="chan-1",
        channel_id="chan-1",
        root_id="",
        trigger_post_id="post-1",
        source="websocket",
        mattermost_user_id="user-1",
        requester_user_id=7,
        trigger={
            "channel_id": "chan-1",
            "post_id": "post-1",
            "text": "draw me a chart",
            "user_id": "user-1",
            "channel_type": "D",
        },
    )
    fields.update(overrides)
    return executions.begin(**fields)


# ---------------------------------------------------------------------------
# Lifecycle: record, announce after the delay, settle
# ---------------------------------------------------------------------------


async def test_a_turn_leaves_a_record_and_posts_nothing_of_its_own(world):
    context = await _begin(world)
    assert context is not None and world.store.rows["exec-1"].status == "running"
    await executions.progress("Thinking…")
    await executions.finish("succeeded", reply_post_id="reply-1")

    row = world.store.rows["exec-1"]
    assert (row.status, row.reply_post_id, row.step) == ("succeeded", "reply-1", "Thinking…")
    assert row.finished_at is not None and row.trigger["text"] == "draw me a chart"
    # The chat sees only the reply: no progress or placeholder posts, ever.
    assert world.client.created == [] and world.client.updated == [] and world.client.deleted == []
    assert executions.current_execution.get() is None and executions.status()["active"] == 0


async def test_tracking_off_binds_nothing_and_every_hook_is_a_no_op(world, monkeypatch):
    monkeypatch.setattr(settings, "EXECUTION_TRACKING_ENABLED", False)
    assert await _begin(world) is None
    await executions.progress("x")
    executions.check_cancel()
    assert await executions.run_cancellable(asyncio.sleep(0, result=42)) == 42
    await executions.finish("succeeded")
    assert world.store.rows == {}


# ---------------------------------------------------------------------------
# Cancel: reaches into the running call, from here or from elsewhere
# ---------------------------------------------------------------------------


async def test_cancel_interrupts_the_call_in_flight_and_settles_as_cancelled(world):
    await _begin(world)

    async def slow_model_call():
        await asyncio.sleep(5)
        return "never"

    async def cancel_soon():
        await asyncio.sleep(0.05)
        return await executions.cancel("exec-1", by_mattermost_user_id="user-1")

    started = asyncio.get_running_loop().time()
    outcome_task = asyncio.create_task(cancel_soon())
    with pytest.raises(executions.ExecutionCancelled):
        await executions.run_cancellable(slow_model_call())
    assert asyncio.get_running_loop().time() - started < 1
    assert await outcome_task == "cancelling"
    assert world.store.rows["exec-1"].cancel_requested_by == "user-1"

    await executions.finish("cancelled")
    assert world.store.rows["exec-1"].status == "cancelled"


async def test_cancel_requested_elsewhere_is_picked_up_by_the_watcher(world):
    """Another process recorded the cancel; only the row says so."""
    context = await _begin(world)
    assert context is not None
    await world.store.request_cancel("exec-1", "user-1")
    await asyncio.sleep(0.2)
    assert context.cancel_requested and context.cancelled_by == "user-1"
    with pytest.raises(executions.ExecutionCancelled):
        executions.check_cancel()
    await executions.finish("cancelled")


async def test_cancel_is_refused_for_strangers_and_finished_turns(world):
    await _begin(world)
    assert await executions.cancel("exec-1", by_mattermost_user_id="someone-else") == "forbidden"
    assert await executions.cancel("missing", by_mattermost_user_id="user-1") == "not_found"
    await executions.finish("succeeded")
    assert await executions.cancel("exec-1", by_mattermost_user_id="user-1") == "not_running"


async def test_an_admin_may_cancel_someone_elses_turn(world, monkeypatch):
    await _begin(world)

    async def is_admin(user_id):
        return user_id == "admin-1"

    monkeypatch.setattr(executions, "_is_admin", is_admin)
    assert await executions.cancel("exec-1", by_mattermost_user_id="admin-1") == "cancelling"
    await executions.finish("cancelled")


# ---------------------------------------------------------------------------
# Typed commands and retry
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "command"),
    [
        ("cancel", "cancel"),
        ("Stop!", "cancel"),
        ("إلغاء", "cancel"),
        ("توقف.", "cancel"),
        ("retry", "retry"),
        ("Try again", "retry"),
        ("أعد المحاولة", "retry"),
        ("cancel the meeting on Thursday", None),
        ("please stop sending reminders", None),
        ("", None),
    ],
)
def test_control_words_are_recognised_only_as_whole_messages(text, command):
    assert execution_control.command_of(text) == command


def _message(text: str, user_id: str = "user-1", **overrides):
    from app.services.conversation import IncomingMessage

    fields = dict(
        channel_id="chan-1", post_id="post-9", text=text, user_id=user_id, channel_type="D", source="websocket"
    )
    fields.update(overrides)
    return IncomingMessage(**fields)


async def test_typed_cancel_acts_only_when_something_is_running(world):
    assert await execution_control.handle_command(_message("cancel")) is False  # nothing running: a normal message

    await _begin(world)
    assert await execution_control.handle_command(_message("cancel")) is True
    assert world.store.rows["exec-1"].cancel_requested_by == "user-1"
    await executions.finish("cancelled")


async def test_typed_cancel_by_a_stranger_is_answered_not_honoured(world):
    await _begin(world)
    assert await execution_control.handle_command(_message("stop", user_id="intruder")) is True
    assert world.store.rows["exec-1"].cancel_requested_at is None
    assert "Only the person who asked" in world.client.created[-1]["message"]
    await executions.finish("succeeded")


async def test_retry_reruns_the_stored_trigger_as_a_new_attempt(world, monkeypatch):
    runs: list[tuple] = []

    async def fake_answer(message, *, retry_of=None, attempt=1):
        runs.append((message.text, message.source, retry_of, attempt))

    monkeypatch.setattr(execution_control, "answer_and_reply", fake_answer)

    await _begin(world)
    await executions.finish("cancelled")

    assert await execution_control.retry("exec-1", by_mattermost_user_id="stranger") == "forbidden"
    assert await execution_control.retry("nope", by_mattermost_user_id="user-1") == "not_found"
    assert await execution_control.retry("exec-1", by_mattermost_user_id="user-1") == "started"
    await asyncio.sleep(0)
    assert runs == [("draw me a chart", "retry", "exec-1", 2)]
    assert world.client.updated == []  # a retry rewrites nothing in the chat

    # Typed "retry" finds the latest failed/stopped turn of the conversation.
    assert await execution_control.handle_command(_message("retry")) is True
    await asyncio.sleep(0)
    assert len(runs) == 2

    # A succeeded turn is not retried; the word goes to the agent instead.
    world.store.rows["exec-1"].status = "succeeded"
    assert await execution_control.handle_command(_message("retry")) is False
