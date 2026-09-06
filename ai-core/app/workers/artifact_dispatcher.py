"""Finishes artifacts whose work outlives their reply.

An image takes tens of seconds. Making the person wait for it would push the
whole turn past the transports' patience, so the reply is published immediately
with a loading card and this worker fills it in: claim, generate, upload,
update the post, update the row.

Every step is written so a crash is survivable:

* the row is claimed with a lease (``FOR UPDATE SKIP LOCKED``), so two workers
  partition the queue rather than paying for the same picture twice;
* ``operation_id`` is unique per (turn, position), so a replayed graph reuses
  the row instead of creating a second job;
* the publication id is recorded BEFORE generation starts — a worker that
  cannot find a post to update stops rather than generating an image nobody
  will ever see;
* a timeout is not retried blindly. The attempt count is on the row, and a
  request that may already have produced (and billed) an image is retried at
  most ``IMAGE_MAX_ATTEMPTS`` times before the artifact is marked failed.
"""

import asyncio
import os
import socket
from contextlib import suppress
from datetime import timedelta
from typing import (
    Any,
    Dict,
    List,
    Optional,
)

from app.core.config import settings
from app.core.logging import logger
from app.core.metrics import rich_media_jobs_total
from app.models import (
    RichArtifact,
    utcnow,
)
from app.services.domain import rich_artifacts as store
from app.services.llm.images import (
    ImageGenerationError,
    extension_for,
    generate_image,
)
from app.services.mattermost import mattermost_client

DEFAULT_POLL_SECONDS = 5
DEFAULT_LEASE_SECONDS = 300
DEFAULT_BATCH = 2


class ArtifactDispatcher:
    """Owns the background generation task."""

    def __init__(
        self,
        worker_id: Optional[str] = None,
        poll_seconds: int = DEFAULT_POLL_SECONDS,
        batch_size: int = DEFAULT_BATCH,
    ) -> None:
        """Create a stopped dispatcher.

        Args:
            worker_id: Identifier stamped on claimed rows; defaults to ``hostname:pid``.
            poll_seconds: Idle interval between claim attempts.
            batch_size: Artifacts claimed per pass — bounded concurrency, since
                each one is a metered model call.
        """
        self.worker_id = worker_id or f"{socket.gethostname()}:{os.getpid()}"
        self.poll_seconds = poll_seconds
        self.batch_size = batch_size
        self._task: Optional[asyncio.Task[None]] = None
        self._wake = asyncio.Event()

    def start(self) -> None:
        """Start the loop, if image generation is switched on."""
        if not (settings.RICH_MEDIA_ENABLED and settings.IMAGE_GENERATION_ENABLED):
            logger.info("artifact_dispatcher_disabled", image_generation=settings.IMAGE_GENERATION_ENABLED)
            return
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._loop())
        logger.info("artifact_dispatcher_started", worker_id=self.worker_id, model=settings.IMAGE_MODEL)

    async def stop(self) -> None:
        """Cancel the loop and wait for it to unwind."""
        if self._task is None:
            return
        self._task.cancel()
        with suppress(asyncio.CancelledError):
            await self._task
        self._task = None
        logger.info("artifact_dispatcher_stopped", worker_id=self.worker_id)

    def wake(self) -> None:
        """Ask the loop to run a pass immediately."""
        self._wake.set()

    async def _loop(self) -> None:
        """Claim and finish artifacts until cancelled."""
        while True:
            try:
                finished = await self.run_once()
                if finished:
                    continue
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.exception("artifact_dispatcher_pass_failed", error=str(e))

            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._wake.wait(), timeout=self.poll_seconds)
            self._wake.clear()

    async def run_once(self) -> int:
        """Claim one batch and finish each artifact in it.

        Returns:
            int: How many artifacts were processed.
        """
        claimed = await store.claim_pending(
            worker_id=self.worker_id,
            lease_seconds=DEFAULT_LEASE_SECONDS,
            kind="image",
            limit=self.batch_size,
        )
        for artifact in claimed:
            await self._finish(artifact)
        return len(claimed)

    async def _finish(self, artifact: RichArtifact) -> None:
        """Generate, upload and publish one image artifact.

        Args:
            artifact: The claimed row.
        """
        if not artifact.post_id:
            # The reply has not been published yet, or never was. Generating now
            # would spend money on a picture with nowhere to go; the next pass
            # picks it up once the publication id is recorded.
            await store.record_failure(
                artifact.id,
                error="no published post to update",
                retry_at=utcnow() + timedelta(seconds=self.poll_seconds * 2),
            )
            return

        prompt = str(artifact.content.get("prompt") or "").strip()
        if not prompt:
            await self._fail(artifact, "the image had no prompt")
            return

        try:
            image = await generate_image(prompt, aspect_ratio=str(artifact.content.get("aspect_ratio") or "1:1"))
        except ImageGenerationError as e:
            # The model answered and refused, or returned something unusable.
            # Retrying will not change that.
            await self._fail(artifact, str(e))
            return
        except Exception as e:
            await self._retry_or_fail(artifact, str(e))
            return

        filename = f"sprintflow-{artifact.id[:8]}{extension_for(image.mime_type)}"
        file_id = await mattermost_client.upload_file(artifact.channel_id, filename, image.content, image.mime_type)
        if not file_id:
            await self._retry_or_fail(artifact, "the upload to Mattermost failed")
            return

        content = dict(artifact.content)
        content["file_id"] = file_id
        await store.update_content(
            artifact.id, status="ready", content=content, file_ids=[file_id], bump_revision=True
        )

        updated = await self._update_post(artifact, file_id)
        rich_media_jobs_total.labels(kind="image", outcome="ready" if updated else "post_update_failed").inc()
        logger.info(
            "artifact_image_published",
            artifact_id=artifact.id,
            post_id=artifact.post_id,
            file_id=file_id,
            attempt=artifact.attempt_count,
        )

    async def _update_post(self, artifact: RichArtifact, file_id: str) -> bool:
        """Merge this artifact's new state into the published post.

        The post is re-read first and only this artifact's reference is
        replaced, so a second image finishing later cannot erase the first
        one's — ``PUT /posts/{id}`` replaces the whole post.

        Args:
            artifact: The finished artifact.
            file_id: The uploaded attachment.

        Returns:
            bool: True when the post was updated.
        """
        post = await mattermost_client.get_post(artifact.post_id or "")
        if not post:
            logger.warning("artifact_post_missing", artifact_id=artifact.id, post_id=artifact.post_id)
            return False

        props: Dict[str, Any] = dict(post.get("props") or {})
        references: List[Dict[str, Any]] = list(props.get("sf_artifacts") or [])
        for reference in references:
            if reference.get("id") == artifact.id:
                reference["status"] = "ready"
                reference["revision"] = artifact.revision + 1
                reference.pop("error", None)
        props["sf_artifacts"] = references

        file_ids = list(post.get("file_ids") or [])
        if file_id not in file_ids:
            file_ids.append(file_id)

        result = await mattermost_client.update_post(artifact.post_id or "", props=props, file_ids=file_ids)
        return result is not None

    async def _fail(self, artifact: RichArtifact, reason: str) -> None:
        """Mark an artifact permanently failed and show that in the post."""
        await store.record_failure(artifact.id, error=reason, retry_at=None)
        await self._mark_post_failed(artifact, reason)
        rich_media_jobs_total.labels(kind="image", outcome="failed").inc()
        logger.warning("artifact_image_failed", artifact_id=artifact.id, reason=reason)

    async def _retry_or_fail(self, artifact: RichArtifact, reason: str) -> None:
        """Schedule another attempt, or give up once the budget is spent."""
        if artifact.attempt_count >= settings.IMAGE_MAX_ATTEMPTS:
            await self._fail(artifact, reason)
            return

        delay = timedelta(seconds=min(120, 10 * (2**artifact.attempt_count)))
        await store.record_failure(artifact.id, error=reason, retry_at=utcnow() + delay)
        rich_media_jobs_total.labels(kind="image", outcome="retry").inc()
        logger.info(
            "artifact_image_retry_scheduled",
            artifact_id=artifact.id,
            attempt=artifact.attempt_count,
            reason=reason,
        )

    async def _mark_post_failed(self, artifact: RichArtifact, reason: str) -> None:
        """Turn the loading card into a failure card in the published post."""
        post = await mattermost_client.get_post(artifact.post_id or "")
        if not post:
            return

        props: Dict[str, Any] = dict(post.get("props") or {})
        references: List[Dict[str, Any]] = list(props.get("sf_artifacts") or [])
        for reference in references:
            if reference.get("id") == artifact.id:
                reference["status"] = "failed"
                reference["error"] = reason[:200]
        props["sf_artifacts"] = references
        await mattermost_client.update_post(artifact.post_id or "", props=props)

    def status(self) -> Dict[str, Any]:
        """Report the loop's state, for the health endpoint.

        Returns:
            dict: Worker id, whether it is running, and the configured model.
        """
        return {
            "running": self._task is not None and not self._task.done(),
            "worker_id": self.worker_id,
            "model": settings.IMAGE_MODEL,
            "enabled": settings.RICH_MEDIA_ENABLED and settings.IMAGE_GENERATION_ENABLED,
        }


artifact_dispatcher = ArtifactDispatcher()
