"""Execution records against the real database.

Runs only with ``SPRINTFLOW_INTEGRATION_DB=1``. Pins what cancel-from-elsewhere
and restart recovery rely on: a cancel request lands on the row, the running
and latest lookups are scoped to the conversation, and only stale rows are
failed by recovery.
"""

import os
import uuid
from datetime import timedelta

import pytest

from app.models import (
    Execution,
    utcnow,
)
from app.services.domain import executions as store

pytestmark = pytest.mark.skipif(
    os.getenv("SPRINTFLOW_INTEGRATION_DB") != "1", reason="needs SPRINTFLOW_INTEGRATION_DB=1 and a database"
)


def _row(session_id: str, **extra) -> Execution:
    base = dict(
        id=f"exec-{uuid.uuid4().hex[:20]}",
        session_id=session_id,
        channel_id="chan-int",
        trigger_post_id="post-int",
        mattermost_user_id="user-int",
        status=store.RUNNING,
        started_at=utcnow(),
        heartbeat_at=utcnow(),
        trigger={"text": "hello"},
        worker="host-int:1",
    )
    base.update(extra)
    return Execution(**base)  # type: ignore[arg-type]


async def test_cancel_request_lands_on_the_row_and_running_lookup_is_scoped():
    mine, theirs = f"s-{uuid.uuid4()}", f"s-{uuid.uuid4()}"
    row = await store.create_execution(_row(mine))
    await store.create_execution(_row(theirs))

    assert [r.id for r in await store.find_running_for_session(mine)] == [row.id]
    updated = await store.request_cancel(row.id, "user-int")
    assert updated is not None and updated.cancel_requested_by == "user-int" and updated.cancel_requested_at

    await store.update_execution(row.id, status=store.CANCELLED, finished_at=utcnow())
    assert await store.find_running_for_session(mine) == []
    latest = await store.latest_for_session(mine, (store.FAILED, store.CANCELLED))
    assert latest is not None and latest.id == row.id


async def test_recovery_fails_only_stale_rows_and_finds_a_hosts_rows():
    session = f"s-{uuid.uuid4()}"
    host = f"host-{uuid.uuid4().hex[:8]}"
    stale = await store.create_execution(_row(session, heartbeat_at=utcnow() - timedelta(hours=1)))
    fresh = await store.create_execution(_row(session, worker=f"{host}:42"))

    failed = await store.mark_stale_running_failed(stale_after_seconds=600)
    assert stale.id in {r.id for r in failed} and fresh.id not in {r.id for r in failed}
    assert (await store.get_execution(stale.id)).status == store.FAILED  # type: ignore[union-attr]
    assert [r.id for r in await store.find_running_for_host(host)] == [fresh.id]
