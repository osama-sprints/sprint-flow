"""Delivers due daily standup prompts and closes silent days.

One ``StandupDispatcher`` runs per ai-core process as a single asyncio task.
Each pass does three things, in order:

1. **Scope** — registers today's prompt for every active learner in every
   active sprint (``ensure_scope``). Rows are created with ``ON CONFLICT DO
   NOTHING`` on ``(sprint, learner, local day)`` before any DM goes out, so two
   passes (or two containers) can never prompt the same person twice for the
   same day.
2. **Close** — marks prompts whose local day has ended as ``missed`` when they
   never got a reply (or never got sent). A silent day is recorded as a fact,
   never as a fabricated standup entry.
3. **Deliver** — claims a batch of due rows with a lease (``FOR UPDATE SKIP
   LOCKED`` in ``app.services.domain.standups.claim_due_prompts``), hands each
   to ``app.services.standups.deliver_prompt`` and counts the outcomes. A
   second dispatcher therefore partitions the batch instead of duplicating
   DMs.

The loop waits for the poll interval between passes; a pass that fills its
batch runs again immediately.
"""

import asyncio
import os
import socket
from contextlib import suppress
from dataclasses import (
    asdict,
    dataclass,
)
from datetime import datetime
from typing import (
    Collection,
    Any,
    Dict,
)

from psycopg.errors import UndefinedTable
from sqlalchemy.exc import ProgrammingError

from app.core.config import settings
from app.core.logging import logger
from app.core.metrics import standup_prompts_total
from app.models import utcnow
from app.services import standups
from app.services.domain import standups as repo

# Rows claimed per pass; a full batch triggers an immediate next pass.
DEFAULT_CLAIM_BATCH_SIZE = 50


@dataclass
class DispatchSummary:
    """Counts from one ``run_once`` pass."""

    ensured: int = 0
    missed_closed: int = 0
    claimed: int = 0
    sent: int = 0
    retry: int = 0
    failed: int = 0
    halted: int = 0
    skipped: int = 0
    errors: int = 0
    schema_missing: bool = False

    def record(self, outcome: standups.DeliveryOutcome) -> None:
        """Increment the counter for one outcome.

        Args:
            outcome: The delivery outcome.
        """
        setattr(self, outcome.value, getattr(self, outcome.value) + 1)


def _is_missing_table(error: Exception) -> bool:
    """Whether a database error means the prompt tables do not exist yet.

    Args:
        error: The exception raised by the claim.

    Returns:
        bool: True for PostgreSQL ``undefined_table`` (42P01).
    """
    if isinstance(error, UndefinedTable):
        return True
    if isinstance(error, ProgrammingError) and isinstance(error.orig, UndefinedTable):
        return True
    return "does not exist" in str(error) and "relation" in str(error)


class StandupDispatcher:
    """Owns the background standup-collection task."""

    def __init__(
        self,
        worker_id: str | None = None,
        claim_batch_size: int = DEFAULT_CLAIM_BATCH_SIZE,
        only_user_ids: Collection[int] | None = None,
    ) -> None:
        """Create a stopped dispatcher.

        Args:
            worker_id: Identifier stamped on claimed rows; defaults to ``hostname:pid``.
            claim_batch_size: Rows claimed per pass.
            only_user_ids: Restrict every pass to these people. Production leaves this None;
                verification harnesses pass their own learners so a probe can never claim (and
                fake-deliver) a real person's prompt.
        """
        self.worker_id = worker_id or f"{socket.gethostname()}:{os.getpid()}"
        self.claim_batch_size = claim_batch_size
        self.only_user_ids = only_user_ids
        self._task: asyncio.Task[None] | None = None
        self._runs = 0
        self._last_run_at: datetime | None = None
        self._last_summary: DispatchSummary | None = None
        self._schema_missing = False

    @property
    def running(self) -> bool:
        """Whether the background task is alive."""
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        """Start the background task (no-op when standups are disabled or already running)."""
        if not settings.STANDUP_ENABLED:
            logger.info("standup_dispatcher_disabled")
            return
        if self.running:
            logger.info("standup_dispatcher_already_running", worker_id=self.worker_id)
            return
        self._task = asyncio.create_task(self._run_loop(), name="standup-dispatcher")
        logger.info(
            "standup_dispatcher_started",
            worker_id=self.worker_id,
            poll_interval_seconds=settings.STANDUP_POLL_INTERVAL_SECONDS,
            claim_lease_seconds=settings.STANDUP_CLAIM_LEASE_SECONDS,
            prompt_local_hour=settings.STANDUP_PROMPT_LOCAL_HOUR,
            max_attempts=settings.STANDUP_MAX_ATTEMPTS,
        )

    async def stop(self) -> None:
        """Stop the background task; a pass in flight is cancelled at its next await."""
        task = self._task
        self._task = None
        if task is None or task.done():
            logger.info("standup_dispatcher_stopped", worker_id=self.worker_id, was_running=False)
            return
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        logger.info("standup_dispatcher_stopped", worker_id=self.worker_id, was_running=True)

    def status(self) -> Dict[str, Any]:
        """Snapshot for the health endpoint.

        Returns:
            dict: Running state, configuration and the last pass's counts.
        """
        return {
            "enabled": settings.STANDUP_ENABLED,
            "running": self.running,
            "worker_id": self.worker_id,
            "poll_interval_seconds": settings.STANDUP_POLL_INTERVAL_SECONDS,
            "claim_lease_seconds": settings.STANDUP_CLAIM_LEASE_SECONDS,
            "runs": self._runs,
            "last_run_at": self._last_run_at.isoformat() if self._last_run_at else None,
            "last_summary": asdict(self._last_summary) if self._last_summary else None,
            "schema_missing": self._schema_missing,
        }

    async def run_once(self, *, now: datetime | None = None) -> DispatchSummary:
        """Ensure scope, close silent days, then claim and deliver one batch.

        Args:
            now: Reference instant for "due" and for the lease; defaults to now.

        Returns:
            DispatchSummary: Counts for this pass.
        """
        reference = now or utcnow()
        summary = DispatchSummary()
        self._runs += 1
        self._last_run_at = reference

        try:
            summary.ensured = await standups.ensure_scope(
                now=reference, created_count_cap=200 if self.only_user_ids is None else None, user_ids=self.only_user_ids
            )
        except Exception as e:
            summary.errors += 1
            if _is_missing_table(e):
                summary.schema_missing = True
                self._schema_missing = True
                logger.warning("standup_schema_missing", worker_id=self.worker_id, error=str(e))
            else:
                logger.exception("standup_scope_failed", worker_id=self.worker_id, error=str(e))
            self._last_summary = summary
            return summary

        try:
            summary.missed_closed = await standups.close_missed_prompts(now=reference)
        except Exception as e:
            summary.errors += 1
            logger.exception("standup_close_missed_failed", worker_id=self.worker_id, error=str(e))
            self._last_summary = summary
            return summary

        try:
            prompts = await repo.claim_due_prompts(
                worker_id=self.worker_id,
                lease_seconds=settings.STANDUP_CLAIM_LEASE_SECONDS,
                limit=self.claim_batch_size,
                now=reference,
                user_ids=self.only_user_ids,
            )
        except Exception as e:
            summary.errors += 1
            if _is_missing_table(e):
                summary.schema_missing = True
                self._schema_missing = True
                logger.warning("standup_schema_missing", worker_id=self.worker_id, error=str(e))
            else:
                logger.exception("standup_claim_failed", worker_id=self.worker_id, error=str(e))
            self._last_summary = summary
            return summary

        self._schema_missing = False
        summary.claimed = len(prompts)
        for prompt in prompts:
            try:
                result = await standups.deliver_prompt(prompt, now=reference)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                # The lease keeps the row exclusive until it expires, after
                # which another pass retries it; nothing is marked sent here.
                summary.errors += 1
                logger.exception("standup_prompt_delivery_crashed", prompt_id=prompt.id, error=str(e))
                continue
            summary.record(result.outcome)
            standup_prompts_total.labels(outcome=result.outcome.value).inc()

        self._last_summary = summary
        if summary.claimed:
            logger.info("standup_dispatch_pass", worker_id=self.worker_id, **asdict(summary))
        elif summary.ensured or summary.missed_closed:
            logger.debug("standup_dispatch_pass_idle", worker_id=self.worker_id, **asdict(summary))
        return summary

    async def _wait_for_work(self) -> None:
        """Sleep for the poll interval between full passes."""
        try:
            await asyncio.sleep(settings.STANDUP_POLL_INTERVAL_SECONDS)
        except asyncio.CancelledError:
            raise

    async def _run_loop(self) -> None:
        """Run passes forever; any error is logged and the loop continues."""
        while True:
            try:
                summary = await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.exception("standup_dispatch_pass_failed", worker_id=self.worker_id, error=str(e))
                summary = DispatchSummary(errors=1)
            if summary.claimed >= self.claim_batch_size:
                continue
            await self._wait_for_work()


standup_dispatcher = StandupDispatcher()