import asyncio
from unittest.mock import AsyncMock

from app.services.conversation import IncomingMessage
from app.services import mattermost_ingestion


def test_ingestion_request_requires_explicit_prompt_and_file():
    assert mattermost_ingestion.is_ingestion_request("Study this file", ["file-1"])
    assert mattermost_ingestion.is_ingestion_request("Ingest this document", ["file-1"])
    assert not mattermost_ingestion.is_ingestion_request("Study this file", [])
    assert mattermost_ingestion.is_ingestion_request("Please summarize this", ["file-1"])


def test_extract_file_ids_reads_post_and_attachment_properties():
    post = {
        "file_ids": ["file-1"],
        "props": {
            "file_ids": ["file-2"],
            "attachments": [{"file_id": "file-3"}, {"id": "file-4"}],
        },
    }

    assert mattermost_ingestion.extract_file_ids(post, ["file-4", "file-5"]) == [
        "file-1",
        "file-3",
        "file-4",
        "file-2",
        "file-5",
    ]


def test_non_admin_is_rejected_before_download(monkeypatch):
    client = mattermost_ingestion.mattermost_client
    monkeypatch.setattr(
        client, "get_user", AsyncMock(return_value={"roles": "system_user", "email": "learner@example.com"})
    )
    download = AsyncMock()
    monkeypatch.setattr(client, "download_file", download)

    message = IncomingMessage(
        channel_id="channel-1",
        user_id="user-1",
        text="Study this file",
        file_ids=["file-1"],
    )
    reply = asyncio.run(mattermost_ingestion.ingest_attached_documents(message))

    assert reply == "⚠️ Authorization Error: Only workspace admins can submit new training documents."
    download.assert_not_awaited()


def test_system_admin_role_is_accepted():
    for roles in ("system_admin", ["system_admin", "system_user"]):
        assert mattermost_ingestion._is_authorized_admin({"roles": roles, "email": "admin@example.com"})
