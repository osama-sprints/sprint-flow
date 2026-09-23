"""Admin-only document ingestion triggered by Mattermost file attachments."""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING

from app.core.config import settings
from app.core.logging import logger
from app.services.document_ingestion.pipeline import IngestionPipeline
from app.services.mattermost import mattermost_client

if TYPE_CHECKING:
    from app.services.conversation import IncomingMessage

_ADMIN_PROMPTS = re.compile(
    r"\b(?:study|ingest|add\s+document|new\s+file|pdf|doc)\b",
    re.IGNORECASE,
)
_ALLOWED_SUFFIXES = {".pdf", ".docx"}


def is_ingestion_request(text: str, file_ids: list[str]) -> bool:
    """Return true for any attachment, so files never fall through to the LLM."""
    return bool(file_ids)


def extract_file_ids(post: dict[str, object] | None, fallback: list[str] | None = None) -> list[str]:
    """Extract attachment ids from Mattermost post fields and attachment props."""
    post = post or {}
    raw_file_ids = post.get("file_ids")
    values: list[object] = list(raw_file_ids) if isinstance(raw_file_ids, list) else []
    props = post.get("props")
    if isinstance(props, dict):
        attachments = props.get("attachments") or []
        if isinstance(attachments, list):
            for attachment in attachments:
                if isinstance(attachment, dict):
                    values.append(attachment.get("file_id") or attachment.get("id"))
        prop_file_ids = props.get("file_ids") or []
        values.extend(prop_file_ids if isinstance(prop_file_ids, list) else [prop_file_ids])
    values.extend(fallback or [])
    strings = [str(value) for value in values if value]
    return list(dict.fromkeys(strings))


def _is_authorized_admin(user: dict[str, object]) -> bool:
    raw_roles = user.get("roles") or ""
    roles = {str(role).strip() for role in raw_roles} if isinstance(raw_roles, list) else set(str(raw_roles).split())
    email = str(user.get("email") or "").strip().lower()
    return bool({"system_admin", "channel_admin"} & roles) or email in settings.ADMIN_EMAILS


def _safe_filename(filename: str, file_id: str) -> str:
    name = Path(filename).name
    suffix = Path(name).suffix.lower()
    if suffix not in _ALLOWED_SUFFIXES:
        raise ValueError(f"unsupported attachment type: {suffix or 'unknown'}")
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(name).stem).strip("._") or f"document-{file_id}"
    return f"{stem}{suffix}"


async def ingest_attached_documents(message: IncomingMessage) -> str | None:
    """Authorize and ingest explicit PDF/DOCX requests, returning a reply."""
    if not is_ingestion_request(message.text, message.file_ids):
        return None

    user = await mattermost_client.get_user(message.user_id)
    if not user or not _is_authorized_admin(user):
        return "⚠️ Authorization Error: Only workspace admins can submit new training documents."

    target_dir = Path("/tmp/sprintflow_uploads")
    target_dir.mkdir(parents=True, exist_ok=True)
    pipeline = IngestionPipeline()
    completed: list[str] = []

    for file_id in message.file_ids:
        downloaded = await mattermost_client.download_file(file_id)
        if downloaded is None:
            raise RuntimeError(f"could not download Mattermost file {file_id}")
        content, filename = downloaded
        safe_name = _safe_filename(filename, file_id)
        path = target_dir / safe_name
        path.write_bytes(content)
        document_id = path.stem.lower().replace(" ", "_")
        await run_ingestion_pipeline(pipeline, str(path), document_id, "learner")
        completed.append(safe_name)

    logger.info("mattermost_documents_ingested", user_id=message.user_id, filenames=completed)
    if len(completed) == 1:
        return f"✅ Ingested '{completed[0]}'. You can now ask questions about this document."
    return (
        f"✅ Ingested {', '.join(repr(name) for name in completed)}. You can now ask questions about these documents."
    )


async def run_ingestion_pipeline(
    pipeline: IngestionPipeline,
    file_path: str,
    document_id: str,
    audience: str,
) -> int:
    """Run the standard document loader, chunker, embedding, and vector store pipeline."""
    return await pipeline.ingest_document(file_path, document_id, audience)
