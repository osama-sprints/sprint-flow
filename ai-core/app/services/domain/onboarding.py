"""The onboarding outbox: enqueue, claim, and settle delivery steps.

Two properties are enforced here, structurally rather than by convention:

- **Exactly one row per (person, cohort, step kind).** ``enqueue_step`` inserts
  with ``ON CONFLICT DO NOTHING`` on that unique key, so a replayed arrival
  event, a reconnecting WebSocket, or two processes racing each other can
  never create a second welcome.
- **At most one worker delivers a row.** ``claim_due_steps`` selects with
  ``FOR UPDATE SKIP LOCKED`` and stamps a lease in the same transaction, so
  concurrent dispatchers partition the work and a crashed worker's lease
  expires instead of blocking the row forever.
"""

from datetime import (
    datetime,
    timedelta,
)
from typing import Collection

from sqlalchemy import or_
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlmodel import (
    col,
    select,
)
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models import (
    OnboardingStep,
    require_aware,
    utcnow,
)
from app.models.enums import (
    OnboardingStepKind,
    OnboardingStepStatus,
)
from app.services.database import session_scope


async def enqueue_step(
    *,
    user_id: int,
    cohort_id: int | None,
    step_kind: OnboardingStepKind,
    due_at: datetime,
    session: AsyncSession | None = None,
) -> tuple[OnboardingStep, bool]:
    """Schedule a delivery, idempotently.

    Args:
        user_id: The person.
        cohort_id: The cohort the step is about, or None for workspace-level steps.
        step_kind: Which message.
        due_at: Earliest delivery instant (timezone-aware).
        session: Optional session to reuse.

    Returns:
        tuple[OnboardingStep, bool]: The row and whether this call created it.

    Raises:
        ValueError: If ``due_at`` is naive.
    """
    require_aware(due_at, "due_at")
    now = utcnow()
    statement = (
        pg_insert(OnboardingStep)
        .values(
            user_id=user_id,
            cohort_id=cohort_id,
            step_kind=step_kind.value,
            status=OnboardingStepStatus.PENDING.value,
            due_at=due_at,
            attempt_count=0,
            created_at=now,
            updated_at=now,
        )
        .on_conflict_do_nothing(constraint="uq_onboarding_steps_user_cohort_kind")
        .returning(OnboardingStep)
    )
    async with session_scope(session) as s:
        result = await s.exec(statement)
        created = result.scalar_one_or_none()
        if created is not None:
            return created, True
        existing = await get_step_for(user_id, cohort_id, step_kind, session=s)
        assert existing is not None
        return existing, False


async def get_step(step_id: int, session: AsyncSession | None = None) -> OnboardingStep | None:
    """Fetch a step by id.

    Args:
        step_id: ``onboarding_steps.id``.
        session: Optional session to reuse.

    Returns:
        OnboardingStep | None: The row, or None.
    """
    async with session_scope(session) as s:
        return await s.get(OnboardingStep, step_id)


async def get_step_for(
    user_id: int,
    cohort_id: int | None,
    step_kind: OnboardingStepKind,
    session: AsyncSession | None = None,
) -> OnboardingStep | None:
    """Fetch the unique step for a person, cohort and kind.

    Args:
        user_id: The person.
        cohort_id: The cohort, or None for workspace-level steps.
        step_kind: Which message.
        session: Optional session to reuse.

    Returns:
        OnboardingStep | None: The row, or None.
    """
    statement = select(OnboardingStep).where(
        OnboardingStep.user_id == user_id,
        OnboardingStep.step_kind == step_kind.value,
    )
    if cohort_id is None:
        statement = statement.where(col(OnboardingStep.cohort_id).is_(None))
    else:
        statement = statement.where(OnboardingStep.cohort_id == cohort_id)
    async with session_scope(session) as s:
        result = await s.exec(statement)
        return result.first()


async def list_steps_for_user(user_id: int, session: AsyncSession | None = None) -> list[OnboardingStep]:
    """Every step of a person's journey, by due time.

    Args:
        user_id: The person.
        session: Optional session to reuse.

    Returns:
        list[OnboardingStep]: Matching rows.
    """
    async with session_scope(session) as s:
        result = await s.exec(
            select(OnboardingStep)
            .where(OnboardingStep.user_id == user_id)
            .order_by(OnboardingStep.due_at, OnboardingStep.id)  # type: ignore[arg-type]
        )
        return list(result.all())


async def claim_due_steps(
    *,
    worker_id: str,
    lease_seconds: int,
    limit: int = 20,
    now: datetime | None = None,
    user_ids: Collection[int] | None = None,
    session: AsyncSession | None = None,
) -> list[OnboardingStep]:
    """Atomically claim pending steps that are due and not leased by a live worker.

    Args:
        worker_id: Identifier stamped on the rows this worker takes.
        lease_seconds: How long a claim stays exclusive; an older claim is treated as abandoned.
        limit: Maximum rows to claim in one call.
        now: The reference instant (defaults to now, UTC).
        user_ids: Restrict the claim to these people (verification harnesses use this so a
            probe never takes real people's steps); None claims for everyone.
        session: Optional session to reuse.

    Returns:
        list[OnboardingStep]: The claimed rows, soonest first.

    Raises:
        ValueError: If ``now`` is naive.
    """
    reference = require_aware(now, "now") if now is not None else utcnow()
    lease_cutoff = reference - timedelta(seconds=lease_seconds)
    statement = (
        select(OnboardingStep)
        .where(
            OnboardingStep.status == OnboardingStepStatus.PENDING.value,
            OnboardingStep.due_at <= reference,
            or_(col(OnboardingStep.next_attempt_at).is_(None), col(OnboardingStep.next_attempt_at) <= reference),
            or_(col(OnboardingStep.claimed_at).is_(None), col(OnboardingStep.claimed_at) < lease_cutoff),
        )
        .order_by(OnboardingStep.due_at, OnboardingStep.id)  # type: ignore[arg-type]
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    if user_ids is not None:
        if not user_ids:
            return []
        statement = statement.where(col(OnboardingStep.user_id).in_(list(user_ids)))
    async with session_scope(session) as s:
        result = await s.exec(statement)
        claimed = list(result.all())
        for step in claimed:
            step.claimed_at = reference
            step.claimed_by = worker_id
            step.updated_at = reference
            s.add(step)
        await s.flush()
        for step in claimed:
            await s.refresh(step)
        return claimed


async def mark_step_sent(
    step_id: int,
    *,
    mattermost_post_id: str | None,
    role_key: str | None,
    claimed_by: str | None = None,
    require_unclaimed: bool = False,
    lease_seconds: int | None = None,
    session: AsyncSession | None = None,
) -> OnboardingStep | None:
    """Record a successful delivery and release the claim.

    Args:
        step_id: The step.
        mattermost_post_id: The DM post that was created.
        role_key: The role the content was tailored to, or None.
        claimed_by: When given, the row is settled only if this worker still
            holds its claim — a worker whose lease expired and was taken over
            must not overwrite the other worker's outcome.
        require_unclaimed: Settle only when no other worker holds a live claim.
            Used when one delivery covers another row (a welcome carrying a
            cohort's orientation): if a worker is already delivering that row,
            leave it to them rather than clearing their claim.
        lease_seconds: How old a claim may be before it counts as abandoned;
            required with ``require_unclaimed``.
        session: Optional session to reuse.

    Returns:
        OnboardingStep | None: The updated row, or None when absent or when the
        claim now belongs to another worker.
    """
    async with session_scope(session) as s:
        step = await s.get(OnboardingStep, step_id)
        if step is None:
            return None
        if claimed_by is not None and step.claimed_by != claimed_by:
            return None
        if require_unclaimed and step.claimed_at is not None and lease_seconds is not None:
            if step.claimed_at > utcnow() - timedelta(seconds=lease_seconds):
                return None
        now = utcnow()
        step.status = OnboardingStepStatus.SENT.value
        step.sent_at = now
        step.attempt_count += 1
        step.next_attempt_at = None
        step.last_error = None
        step.claimed_at = None
        step.claimed_by = None
        step.role_key_at_delivery = role_key
        step.mattermost_post_id = mattermost_post_id
        step.updated_at = now
        s.add(step)
        await s.flush()
        await s.refresh(step)
        return step


async def mark_step_failed(
    step_id: int,
    *,
    error: str,
    next_attempt_at: datetime,
    max_attempts: int,
    claimed_by: str | None = None,
    session: AsyncSession | None = None,
) -> OnboardingStep | None:
    """Record a failed attempt, schedule the retry, and give up after ``max_attempts``.

    Args:
        step_id: The step.
        error: Readable description of the failure.
        next_attempt_at: When to try again.
        max_attempts: Attempts after which the step is marked ``failed``.
        claimed_by: When given, only the worker still holding the claim may record the failure.
        session: Optional session to reuse.

    Returns:
        OnboardingStep | None: The updated row, or None when absent or claimed by another worker.

    Raises:
        ValueError: If ``next_attempt_at`` is naive.
    """
    require_aware(next_attempt_at, "next_attempt_at")
    async with session_scope(session) as s:
        step = await s.get(OnboardingStep, step_id)
        if step is None:
            return None
        if claimed_by is not None and step.claimed_by != claimed_by:
            return None
        now = utcnow()
        step.attempt_count += 1
        step.last_error = error[:2000]
        step.claimed_at = None
        step.claimed_by = None
        step.updated_at = now
        if step.attempt_count >= max_attempts:
            step.status = OnboardingStepStatus.FAILED.value
            step.next_attempt_at = None
        else:
            step.next_attempt_at = next_attempt_at
        s.add(step)
        await s.flush()
        await s.refresh(step)
        return step


async def release_claim(step_id: int, session: AsyncSession | None = None) -> None:
    """Give a claimed step back without counting an attempt (e.g. its cohort is inactive).

    Args:
        step_id: The step.
        session: Optional session to reuse.
    """
    async with session_scope(session) as s:
        step = await s.get(OnboardingStep, step_id)
        if step is None:
            return
        step.claimed_at = None
        step.claimed_by = None
        step.updated_at = utcnow()
        s.add(step)
        await s.flush()


async def halt_steps_for_cohort(cohort_id: int, session: AsyncSession | None = None) -> int:
    """Mark every pending step of a cohort ``halted`` when the cohort is deactivated.

    Args:
        cohort_id: The cohort.
        session: Optional session to reuse.

    Returns:
        int: How many rows were halted.
    """
    async with session_scope(session) as s:
        result = await s.exec(
            select(OnboardingStep).where(
                OnboardingStep.cohort_id == cohort_id,
                OnboardingStep.status == OnboardingStepStatus.PENDING.value,
            )
        )
        rows = list(result.all())
        now = utcnow()
        for step in rows:
            step.status = OnboardingStepStatus.HALTED.value
            step.claimed_at = None
            step.claimed_by = None
            step.updated_at = now
            s.add(step)
        await s.flush()
        return len(rows)


async def count_steps(
    *,
    status: OnboardingStepStatus | None = None,
    session: AsyncSession | None = None,
) -> int:
    """Count steps, optionally by status — for health and verification output.

    Args:
        status: Only this status, or all when None.
        session: Optional session to reuse.

    Returns:
        int: The count.
    """
    statement = select(OnboardingStep)
    if status is not None:
        statement = statement.where(OnboardingStep.status == status.value)
    async with session_scope(session) as s:
        result = await s.exec(statement)
        return len(list(result.all()))
