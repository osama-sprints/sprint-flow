"""Attachment records against the real database.

Runs only with ``SPRINTFLOW_INTEGRATION_DB=1``. These pin what the read tools
and the retention sweep rely on: a re-ingested file id replaces its row, a
read is found only from the conversation the file arrived in, and expired
rows are the only ones a sweep deletes.
"""

import os
import uuid
from datetime import timedelta

import pytest

from app.models import (
    Attachment,
    utcnow,
)
from app.services.domain import attachments as store

pytestmark = pytest.mark.skipif(
    os.getenv("SPRINTFLOW_INTEGRATION_DB") != "1", reason="needs SPRINTFLOW_INTEGRATION_DB=1 and a database"
)


def _row(session_id: str, channel_id: str = "chan-int", **extra) -> Attachment:
    base = dict(
        id=f"file-{uuid.uuid4().hex[:20]}",
        post_id="post-int",
        channel_id=channel_id,
        session_id=session_id,
        name="q3.pdf",
        status="accepted",
        kind="pdf",
        extraction="pypdf",
        text="[page 1]\nhello",
        text_chars=15,
        expires_at=utcnow() + timedelta(days=30),
    )
    base.update(extra)
    return Attachment(**base)  # type: ignore[arg-type]


async def test_reingesting_the_same_file_replaces_its_row():
    session = f"s-{uuid.uuid4()}"
    first = _row(session)
    await store.save_attachments([first])
    again = _row(session, id=first.id, text="[page 1]\nhello again", text_chars=21)
    await store.save_attachments([again])

    rows = await store.list_for_conversation(session, "chan-int")
    assert [r.id for r in rows] == [first.id]
    assert rows[0].text == "[page 1]\nhello again"


async def test_reads_are_scoped_to_the_conversation():
    mine, theirs = f"s-{uuid.uuid4()}", f"s-{uuid.uuid4()}"
    row = _row(mine)
    await store.save_attachments([row, _row(theirs)])

    assert (await store.get_for_conversation(row.id, mine, "chan-int")) is not None
    assert await store.get_for_conversation(row.id, theirs, "chan-int") is None
    assert await store.get_for_conversation(row.id, mine, "other-chan") is None
    assert [r.session_id for r in await store.list_for_conversation(mine, "chan-int")] == [mine]


async def test_sweep_deletes_only_expired_rows():
    session = f"s-{uuid.uuid4()}"
    expired = _row(session, expires_at=utcnow() - timedelta(minutes=1))
    live = _row(session)
    await store.save_attachments([expired, live])

    deleted = await store.purge_expired()
    assert deleted >= 1
    remaining = {r.id for r in await store.list_for_conversation(session, "chan-int")}
    assert remaining == {live.id}
