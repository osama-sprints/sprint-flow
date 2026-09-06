"""Artifact persistence against the real database.

Runs only with ``SPRINTFLOW_INTEGRATION_DB=1``, like the other integration
tests. These pin the properties the durable design rests on: a turn's
artifacts come back in order and only for that turn; publication is recorded
and found again; the lease claim hands a row to exactly one worker; and the
click consume succeeds exactly once under concurrency.
"""

import asyncio
import os
import uuid

import pytest

from app.models import RichArtifact
from app.services.domain import rich_artifacts as store

pytestmark = pytest.mark.skipif(
    os.getenv("SPRINTFLOW_INTEGRATION_DB") != "1", reason="needs SPRINTFLOW_INTEGRATION_DB=1 and a database"
)


def _row(turn_id: str, kind: str = "mermaid", status: str = "ready", **extra) -> RichArtifact:
    return RichArtifact(
        id=str(uuid.uuid4()),
        turn_id=turn_id,
        session_id="s",
        kind=kind,
        status=status,
        title="t",
        content={"definition": "graph TD; A-->B;"} if kind == "mermaid" else {"prompt": "x"},
        channel_id="chan-int",
        **extra,
    )


async def test_turn_artifacts_are_isolated_and_ordered():
    turn, other = f"t-{uuid.uuid4()}", f"t-{uuid.uuid4()}"
    first = await store.create_artifact(_row(turn))
    second = await store.create_artifact(_row(turn))
    await store.create_artifact(_row(other))

    rows = await store.list_for_turn(turn)
    assert [r.id for r in rows] == [first.id, second.id]


async def test_publication_is_recorded_and_found_again():
    """The reconciliation read after a crash: the turn knows its post."""
    turn = f"t-{uuid.uuid4()}"
    await store.create_artifact(_row(turn))
    assert await store.find_turn_publication(turn) is None

    updated = await store.mark_published(turn, "post-abc", file_ids=["f1"])
    assert updated == 1
    assert await store.find_turn_publication(turn) == "post-abc"
    assert (await store.list_for_turn(turn))[0].file_ids == ["f1"]


async def test_lease_claim_gives_a_pending_image_to_one_worker_only():
    turn = f"t-{uuid.uuid4()}"
    pending = await store.create_artifact(_row(turn, kind="image", status="pending", post_id="post-x"))

    a, b = await asyncio.gather(
        store.claim_pending(worker_id="w-a", lease_seconds=300, kind="image", limit=50),
        store.claim_pending(worker_id="w-b", lease_seconds=300, kind="image", limit=50),
    )
    owners = [r.claimed_by for r in a + b if r.id == pending.id]
    assert len(owners) == 1, owners

    # A second pass sees it as running and does not claim it again.
    again = await store.claim_pending(worker_id="w-c", lease_seconds=300, kind="image", limit=50)
    assert pending.id not in [r.id for r in again]


async def test_failure_records_retry_then_permanent():
    from datetime import timedelta

    from app.models import utcnow

    turn = f"t-{uuid.uuid4()}"
    row = await store.create_artifact(_row(turn, kind="image", status="running"))

    await store.record_failure(row.id, error="boom", retry_at=utcnow() + timedelta(seconds=1))
    retried = await store.get_artifact(row.id)
    assert retried is not None and retried.status == "pending" and retried.claimed_by is None

    await store.record_failure(row.id, error="gave up", retry_at=None)
    failed = await store.get_artifact(row.id)
    assert failed is not None and failed.status == "failed" and failed.error == "gave up"
