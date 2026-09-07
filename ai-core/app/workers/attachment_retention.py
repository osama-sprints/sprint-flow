"""Deletes attachment records whose retention has lapsed.

Intake stamps every row with ``expires_at``; this sweep is what honours it.
It runs shortly after start-up and then on a fixed interval, and it only ever
deletes — a failed sweep is logged and retried on the next tick.
"""

import asyncio
from typing import (
    Any,
    Dict,
    Optional,
)

from app.core.config import settings
from app.core.logging import logger
from app.services.domain import attachments as store

_INITIAL_DELAY_SECONDS = 30


class AttachmentRetention:
    """Background sweep for expired attachment rows."""

    def __init__(self) -> None:
        """Create the worker without starting it."""
        self._task: Optional[asyncio.Task[None]] = None
        self._sweeps = 0
        self._deleted = 0
        self._last_error: Optional[str] = None

    def start(self) -> None:
        """Start the sweep loop, unless attachments are switched off."""
        if not settings.FILE_INPUT_ENABLED or self._task is not None:
            return
        self._task = asyncio.create_task(self._run(), name="attachment-retention")
        logger.info(
            "attachment_retention_started",
            retention_days=settings.FILE_INPUT_RETENTION_DAYS,
            interval_seconds=settings.FILE_INPUT_RETENTION_SWEEP_SECONDS,
        )

    async def stop(self) -> None:
        """Cancel the loop and wait for it to finish."""
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except (asyncio.CancelledError, Exception):
            pass
        self._task = None

    def status(self) -> Dict[str, Any]:
        """Operator view for the health endpoint.

        Returns:
            dict: Whether the loop runs, sweeps so far, rows deleted, last error.
        """
        return {
            "running": self._task is not None and not self._task.done(),
            "sweeps": self._sweeps,
            "deleted": self._deleted,
            "last_error": self._last_error,
        }

    async def sweep_once(self) -> int:
        """Delete expired rows once.

        Returns:
            int: Rows deleted.
        """
        deleted = await store.purge_expired()
        self._sweeps += 1
        self._deleted += deleted
        if deleted:
            logger.info("attachment_retention_purged", deleted=deleted)
        return deleted

    async def _run(self) -> None:
        await asyncio.sleep(_INITIAL_DELAY_SECONDS)
        while True:
            try:
                await self.sweep_once()
                self._last_error = None
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self._last_error = str(e)
                logger.exception("attachment_retention_sweep_failed", error=str(e))
            await asyncio.sleep(settings.FILE_INPUT_RETENTION_SWEEP_SECONDS)


attachment_retention = AttachmentRetention()
