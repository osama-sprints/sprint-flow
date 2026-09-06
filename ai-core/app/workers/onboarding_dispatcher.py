"""Delivers due onboarding steps from the outbox.

One ``OnboardingDispatcher`` runs per ai-core process as a single asyncio
task. Each pass claims a batch of due rows with a lease (``FOR UPDATE SKIP
LOCKED`` in ``app.services.domain.onboarding.claim_due_steps``), hands every
row to ``app.services.onboarding.deliver_step`` and counts the outcomes. A
second dispatcher — another container, or ``--workers`` raised by mistake —
partitions the rows instead of duplicating deliveries, because a row is
claimed by exactly one transaction and marked sent before its lease expires.

The loop waits for either the poll interval or a wake-up (``wake``), which the
journey policy triggers whenever it enqueues new work, so a welcome goes out
within milliseconds of the arrival while a follow-up due in three days is
found by polling after any number of restarts.
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
from app.core.metrics import onboarding_steps_total
from app.models import utcnow
from app.services import onboarding
from app.services.domain import onboarding as outbox

# Rows claimed per pass. When a pass fills the batch the loop runs again at
# once instead of waiting for the poll interval.
DEFAULT_CLAIM_BATCH_SIZE = 50


@dataclass
class DispatchSummary:
    """Counts from one ``run_once`` pass."""

    claimed: int = 0
    sent: int = 0
    retry: int = 0
    failed: int = 0
    halted: int = 0
    skipped: int = 0
    errors: int = 0
    schema_missing: bool = False

    def record(self, outcome: onboarding.DeliveryOutcome) -> None:
        """Increment the counter for one outcome.

        Args:
            outcome: The delivery outcome.
        """
        setattr(self, outcome.value, getattr(self, outcome.value) + 1)


def _is_missing_table(error: Exception) -> bool:
    """Whether a database error means the outbox table does not exist yet.

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


class OnboardingDispatcher:
    """Owns the background delivery task."""

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
                verification harnesses pass their own users so a probe can never claim (and
                fake-deliver) a real person's step.
        """
        self.worker_id = worker_id or f"{socket.gethostname()}:{os.getpid()}"
        self.claim_batch_size = claim_batch_size
        self.only_user_ids = only_user_ids
        self._task: asyncio.Task[None] | None = None
        self._wake = asyncio.Event()
        self._runs = 0
        self._last_run_at: datetime | None = None
        self._last_summary: DispatchSummary | None = None
        self._schema_missing = False

    @property
    def running(self) -> bool:
        """Whether the background task is alive."""
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        """Start the background task (no-op when onboarding is disabled or already running)."""
        if not settings.ONBOARDING_ENABLED:
            logger.info("onboarding_dispatcher_disabled")
            return
        if self.running:
            logger.info("onboarding_dispatcher_already_running", worker_id=self.worker_id)
            return
        onboarding.register_wake_listener(self.wake)
        self._task = asyncio.create_task(self._run_loop(), name="onboarding-dispatcher")
        logger.info(
            "onboarding_dispatcher_started",
            worker_id=self.worker_id,
            poll_interval_seconds=settings.ONBOARDING_POLL_INTERVAL_SECONDS,
            claim_lease_seconds=settings.ONBOARDING_CLAIM_LEASE_SECONDS,
            max_attempts=settings.ONBOARDING_MAX_ATTEMPTS,
        )

    async def stop(self) -> None:
        """Stop the background task; a pass in flight is cancelled at its next await."""
        task = self._task
        self._task = None
        if task is None or task.done():
            logger.info("onboarding_dispatcher_stopped", worker_id=self.worker_id, was_running=False)
            return
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        logger.info("onboarding_dispatcher_stopped", worker_id=self.worker_id, was_running=True)

    def wake(self) -> None:
        """Ask the loop to run a delivery pass now instead of at the next poll."""
        self._wake.set()

    def status(self) -> Dict[str, Any]:
        """Snapshot for the health endpoint.

        Returns:
            dict: Running state, configuration and the last pass's counts.
        """
        return {
            "enabled": settings.ONBOARDING_ENABLED,
            "running": self.running,
            "worker_id": self.worker_id,
            "poll_interval_seconds": settings.ONBOARDING_POLL_INTERVAL_SECONDS,
            "claim_lease_seconds": settings.ONBOARDING_CLAIM_LEASE_SECONDS,
            "runs": self._runs,
            "last_run_at": self._last_run_at.isoformat() if self._last_run_at else None,
            "last_summary": asdict(self._last_summary) if self._last_summary else None,
            "schema_missing": self._schema_missing,
        }

    async def run_once(self, *, now: datetime | None = None) -> DispatchSummary:
        """Claim one batch of due steps and deliver each.

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
            steps = await outbox.claim_due_steps(
                worker_id=self.worker_id,
                lease_seconds=settings.ONBOARDING_CLAIM_LEASE_SECONDS,
                limit=self.claim_batch_size,
                now=reference,
                user_ids=self.only_user_ids,
            )
        except Exception as e:
            summary.errors += 1
            if _is_missing_table(e):
                summary.schema_missing = True
                self._schema_missing = True
                logger.warning("onboarding_schema_missing", worker_id=self.worker_id, error=str(e))
            else:
                logger.exception("onboarding_claim_failed", worker_id=self.worker_id, error=str(e))
            self._last_summary = summary
            return summary

        self._schema_missing = False
        summary.claimed = len(steps)
        for step in steps:
            try:
                result = await onboarding.deliver_step(step, now=reference)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                # The lease keeps the row exclusive until it expires, after
                # which another pass retries it; nothing is marked sent here.
                summary.errors += 1
                logger.exception("onboarding_step_delivery_crashed", step_id=step.id, error=str(e))
                continue
            summary.record(result.outcome)
            onboarding_steps_total.labels(step_kind=step.step_kind, outcome=result.outcome.value).inc()

        self._last_summary = summary
        if summary.claimed:
            logger.info("onboarding_dispatch_pass", worker_id=self.worker_id, **asdict(summary))
        return summary

    async def _wait_for_work(self) -> None:
        """Sleep until woken or until the poll interval elapses."""
        try:
            await asyncio.wait_for(self._wake.wait(), timeout=settings.ONBOARDING_POLL_INTERVAL_SECONDS)
        except TimeoutError:
            return
        finally:
            self._wake.clear()

    async def _run_loop(self) -> None:
        """Run passes forever; any error is logged and the loop continues."""
        while True:
            try:
                summary = await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.exception("onboarding_dispatch_pass_failed", worker_id=self.worker_id, error=str(e))
                summary = DispatchSummary(errors=1)
            if summary.claimed >= self.claim_batch_size:
                continue
            await self._wait_for_work()


onboarding_dispatcher = OnboardingDispatcher()
