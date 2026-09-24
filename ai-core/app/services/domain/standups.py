"""Daily progress entries, their proactive prompts, and the raw replies.

Two structural guarantees are enforced here, in the same way the onboarding
outbox enforces its own:

- **Exactly one prompt per (sprint, learner, local day).** ``ensure_prompt``
  inserts with ``ON CONFLICT DO NOTHING`` on that unique key, so two dispatcher
  passes (or two containers) can never prompt the same person twice for the
  same day.
- **At most one worker dispatches a prompt.** ``claim_due_prompts`` selects
  with ``FOR UPDATE SKIP LOCKED`` and stamps a lease in the same transaction;
  a crashed worker's lease expires instead of blocking the row forever.

Raw replies are recorded append-only in ``standup_replies`` (one row per
Mattermost post id), and the parsed entry lands in ``daily_standups`` with
``ON CONFLICT DO NOTHING`` so a racing redelivery never overwrites or double
creates a day's entry.
"""

from datetime import (
    date,
    datetime,
    timedelta,
)
from typing import (
    Collection,
    NamedTuple,
)
from uuid import UUID

from sqlalchemy import (
    and_,
    or_,
)
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlmodel import (
    col,
    select,
)
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models import (
    DailyStandup,
    DailyStandupPrompt,
    Sprint,
    StandupReply,
    require_aware,
    utcnow,
)
from app.models.enums import (
    CHANNEL_ADMIN_ROLES,
    StandupPromptStatus,
    StandupReplyOutcome,
)
from app.core.requester import current_requester
from app.services import authorisation
from app.services.domain import channels, sprints
from app.services.database import session_scope


async def upsert_daily_standup(
    *,
    sprint_id: int,
    learner_id: int,
    log_date: date,
    what_i_did: str,
    what_i_will_do: str,
    blockers: str | None = None,
    session: AsyncSession | None = None,
) -> DailyStandup:
    """Record (or overwrite) one learner's entry for one day of a sprint.

    Args:
        sprint_id: The sprint.
        learner_id: The person.
        log_date: The day.
        what_i_did: Progress.
        what_i_will_do: Plan.
        blockers: Blockers, or None.
        session: Optional session to reuse.

    Returns:
        DailyStandup: The stored row.
    """
    async with session_scope(session) as s:
        result = await s.exec(
            select(DailyStandup).where(
                DailyStandup.sprint_id == sprint_id,
                DailyStandup.learner_id == learner_id,
                DailyStandup.log_date == log_date,
            )
        )
        entry = result.first()
        if entry is None:
            entry = DailyStandup(
                sprint_id=sprint_id,
                learner_id=learner_id,
                log_date=log_date,
                what_i_did=what_i_did,
                what_i_will_do=what_i_will_do,
                blockers=blockers,
            )
        else:
            entry.what_i_did = what_i_did
            entry.what_i_will_do = what_i_will_do
            entry.blockers = blockers
            entry.updated_at = utcnow()
        s.add(entry)
        await s.flush()
        await s.refresh(entry)
        return entry


async def get_daily_standup(standup_id: int, session: AsyncSession | None = None) -> DailyStandup | None:
    """Fetch one entry by id.

    Args:
        standup_id: ``daily_standups.id``.
        session: Optional session to reuse.

    Returns:
        DailyStandup | None: The row, or None.
    """
    async with session_scope(session) as s:
        return await s.get(DailyStandup, standup_id)


async def list_daily_standups(
    sprint_id: int,
    *,
    learner_id: int | None = None,
    log_date: date | None = None,
    session: AsyncSession | None = None,
) -> list[DailyStandup]:
    """Entries for a sprint, optionally narrowed to one person or one day.

    Args:
        sprint_id: The sprint.
        learner_id: Only this person's entries.
        log_date: Only this day.
        session: Optional session to reuse.

    Returns:
        list[DailyStandup]: Ordered by day then person.
    """
    statement = select(DailyStandup).where(DailyStandup.sprint_id == sprint_id)
    if learner_id is not None:
        statement = statement.where(DailyStandup.learner_id == learner_id)
    if log_date is not None:
        statement = statement.where(DailyStandup.log_date == log_date)
    statement = statement.order_by(DailyStandup.log_date, DailyStandup.learner_id)  # type: ignore[arg-type]
    async with session_scope(session) as s:
        result = await s.exec(statement)
        return list(result.all())


async def list_standups_for_channel(
    channel_id: str,
    *,
    start_date: date,
    end_date: date,
    learner_ids: Collection[int] | None = None,
    session: AsyncSession | None = None,
) -> list[DailyStandup]:
    """Every standup entry written in a cohort's channel within a date range.

    A cohort's home is a Mattermost channel in this branch (see the
    ``refactor_cohort_to_channel`` migration), so the cohort-level read for
    downstream summarisation joins ``daily_standups`` onto ``sprints`` by the
    sprint's ``channel_id`` — callers never reach for raw SQL. Missed days
    produce no ``daily_standups`` row (see ``mark_prompt_missed``), so what this
    returns is exactly the material a summary may quote.

    Args:
        channel_id: The cohort's channel.
        start_date: Inclusive first day.
        end_date: Inclusive last day (may equal ``start_date``).
        learner_ids: Only these people's entries, or everyone when None.
        session: Optional session to reuse.

    Returns:
        list[DailyStandup]: The entries, by day then person.
    """
    statement = (
        select(DailyStandup)
        .join(Sprint, Sprint.id == DailyStandup.sprint_id)  # type: ignore[arg-type]
        .where(
            Sprint.channel_id == channel_id,
            DailyStandup.log_date >= start_date,
            DailyStandup.log_date <= end_date,
        )
        .order_by(DailyStandup.log_date, DailyStandup.learner_id)  # type: ignore[arg-type]
    )
    if learner_ids is not None:
        statement = statement.where(col(DailyStandup.learner_id).in_(list(learner_ids)))
    async with session_scope(session) as s:
        result = await s.exec(statement)
        return list(result.all())


# ---------------------------------------------------------------------------
# Daily standup prompts — the dispatch state machine
# ---------------------------------------------------------------------------


async def ensure_prompt(
    *,
    sprint_id: int,
    learner_id: int,
    channel_id: str,
    local_date: date,
    timezone: str,
    dispatch_at: datetime,
    now: datetime | None = None,
    session: AsyncSession | None = None,
) -> tuple[DailyStandupPrompt, bool]:
    """Record a prompt for (sprint, learner, local day), idempotently.

    The unique key is the dispatch state machine's "exactly once" guarantee: a
    second call for the same day returns the existing row and creates nothing,
    so a replayed pass can never prompt the same person twice.

    Args:
        sprint_id: The sprint the day belongs to.
        learner_id: The person prompted.
        channel_id: The sprint's channel.
        local_date: The calendar day, in the learner's zone.
        timezone: The IANA zone the day was computed in.
        dispatch_at: Earliest eligible dispatch instant (timezone-aware).
        now: Reference instant for the audit columns; defaults to now.
        session: Optional session to reuse.

    Returns:
        tuple[DailyStandupPrompt, bool]: The row and whether this call created it.

    Raises:
        ValueError: If ``dispatch_at`` is naive.
    """
    require_aware(dispatch_at, "dispatch_at")
    reference = require_aware(now, "now") if now is not None else utcnow()
    statement = (
        pg_insert(DailyStandupPrompt)
        .values(
            sprint_id=sprint_id,
            learner_id=learner_id,
            channel_id=channel_id,
            local_date=local_date,
            timezone=timezone,
            dispatch_at=dispatch_at,
            status=StandupPromptStatus.PENDING.value,
            dispatch_count=0,
            created_at=reference,
            updated_at=reference,
        )
        .on_conflict_do_nothing(constraint="uq_daily_standup_prompts_sprint_learner_day")
        .returning(DailyStandupPrompt)
    )
    async with session_scope(session) as s:
        result = await s.exec(statement)
        created = result.scalar_one_or_none()
        if created is not None:
            return created, True
        existing = await get_prompt_for(sprint_id, learner_id, local_date, session=s)
        assert existing is not None
        return existing, False


async def get_prompt_for(
    sprint_id: int,
    learner_id: int,
    local_date: date,
    session: AsyncSession | None = None,
) -> DailyStandupPrompt | None:
    """Fetch the unique prompt for a sprint, learner and local day.

    Args:
        sprint_id: The sprint.
        learner_id: The person.
        local_date: The day.
        session: Optional session to reuse.

    Returns:
        DailyStandupPrompt | None: The row, or None.
    """
    async with session_scope(session) as s:
        result = await s.exec(
            select(DailyStandupPrompt).where(
                DailyStandupPrompt.sprint_id == sprint_id,
                DailyStandupPrompt.learner_id == learner_id,
                DailyStandupPrompt.local_date == local_date,
            )
        )
        return result.first()


async def get_prompt(prompt_id: int, session: AsyncSession | None = None) -> DailyStandupPrompt | None:
    """Fetch a prompt by id.

    Args:
        prompt_id: ``daily_standup_prompts.id``.
        session: Optional session to reuse.

    Returns:
        DailyStandupPrompt | None: The row, or None.
    """
    async with session_scope(session) as s:
        return await s.get(DailyStandupPrompt, prompt_id)


async def get_prompt_by_post_id(post_id: str, session: AsyncSession | None = None) -> DailyStandupPrompt | None:
    """Fetch the prompt whose prompt post carried the given Mattermost post id.

    A reply thread rooted at a prompt's post refers to exactly that prompt, even
    after it has been answered (for duplicate/late classification).

    Args:
        post_id: The Mattermost id of the prompt post.
        session: Optional session to reuse.

    Returns:
        DailyStandupPrompt | None: The row, or None.
    """
    lookup = select(DailyStandupPrompt).where(DailyStandupPrompt.prompt_post_id == post_id)
    lookup = lookup.order_by(DailyStandupPrompt.local_date.desc(), DailyStandupPrompt.id.desc())  # type: ignore[attr-defined]
    async with session_scope(session) as s:
        result = await s.exec(lookup)
        return result.first()


async def claim_due_prompts(
    *,
    worker_id: str,
    lease_seconds: int,
    limit: int = 50,
    now: datetime | None = None,
    user_ids: Collection[int] | None = None,
    session: AsyncSession | None = None,
) -> list[DailyStandupPrompt]:
    """Atomically claim pending prompts that are due and not leased by a live worker.

    Args:
        worker_id: Identifier stamped on the rows this worker takes.
        lease_seconds: How long a claim stays exclusive; an older claim is treated as abandoned.
        limit: Maximum rows to claim in one call.
        now: The reference instant (defaults to now, UTC).
        user_ids: Restrict the claim to these people (verification harnesses use this so a
            probe never takes real people's prompts); None claims for everyone.
        session: Optional session to reuse.

    Returns:
        list[DailyStandupPrompt]: The claimed rows, soonest due first.

    Raises:
        ValueError: If ``now`` is naive.
    """
    reference = require_aware(now, "now") if now is not None else utcnow()
    lease_cutoff = reference - timedelta(seconds=lease_seconds)
    statement = (
        select(DailyStandupPrompt)
        .where(
            DailyStandupPrompt.status == StandupPromptStatus.PENDING.value,
            DailyStandupPrompt.dispatch_at <= reference,
            or_(
                col(DailyStandupPrompt.next_attempt_at).is_(None),
                col(DailyStandupPrompt.next_attempt_at) <= reference,
            ),
            or_(
                col(DailyStandupPrompt.claimed_at).is_(None),
                col(DailyStandupPrompt.claimed_at) < lease_cutoff,
            ),
        )
        .order_by(DailyStandupPrompt.dispatch_at, DailyStandupPrompt.id)  # type: ignore[arg-type]
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    if user_ids is not None:
        if not user_ids:
            return []
        statement = statement.where(col(DailyStandupPrompt.learner_id).in_(list(user_ids)))
    async with session_scope(session) as s:
        result = await s.exec(statement)
        claimed = list(result.all())
        for prompt in claimed:
            prompt.claimed_at = reference
            prompt.claimed_by = worker_id
            prompt.updated_at = reference
            s.add(prompt)
        await s.flush()
        for prompt in claimed:
            await s.refresh(prompt)
        return claimed


async def mark_prompt_sent(
    prompt_id: int,
    *,
    post_id: str,
    dm_channel_id: str,
    claimed_by: str | None = None,
    now: datetime | None = None,
    session: AsyncSession | None = None,
) -> DailyStandupPrompt | None:
    """Record a successful dispatch and release the claim.

    Args:
        prompt_id: The prompt.
        post_id: The Mattermost post id of the DM that went out.
        dm_channel_id: The bot<->learner DM channel the post was created in.
        claimed_by: When given, the row is settled only if this worker still holds its claim.
        now: When it was dispatched; defaults to now.
        session: Optional session to reuse.

    Returns:
        DailyStandupPrompt | None: The updated row, or None when absent or claimed by another worker.
    """
    reference = require_aware(now, "now") if now is not None else utcnow()
    async with session_scope(session) as s:
        prompt = await s.get(DailyStandupPrompt, prompt_id)
        if prompt is None:
            return None
        if claimed_by is not None and prompt.claimed_by != claimed_by:
            return None
        prompt.status = StandupPromptStatus.DISPATCHED.value
        prompt.prompt_post_id = post_id
        prompt.dm_channel_id = dm_channel_id
        prompt.dispatch_count += 1
        prompt.dispatched_at = reference
        prompt.next_attempt_at = None
        prompt.last_error = None
        prompt.claimed_at = None
        prompt.claimed_by = None
        prompt.updated_at = reference
        s.add(prompt)
        await s.flush()
        await s.refresh(prompt)
        return prompt


async def mark_prompt_failed(
    prompt_id: int,
    *,
    error: str,
    next_attempt_at: datetime,
    max_attempts: int,
    claimed_by: str | None = None,
    session: AsyncSession | None = None,
) -> DailyStandupPrompt | None:
    """Record a failed dispatch, schedule the retry, and give up after ``max_attempts``.

    Args:
        prompt_id: The prompt.
        error: Readable description of the failure.
        next_attempt_at: When to try again.
        max_attempts: Attempts after which the prompt is marked ``failed``.
        claimed_by: When given, only the worker still holding the claim may record the failure.
        session: Optional session to reuse.

    Returns:
        DailyStandupPrompt | None: The updated row, or None when absent or claimed by another worker.

    Raises:
        ValueError: If ``next_attempt_at`` is naive.
    """
    require_aware(next_attempt_at, "next_attempt_at")
    async with session_scope(session) as s:
        prompt = await s.get(DailyStandupPrompt, prompt_id)
        if prompt is None:
            return None
        if claimed_by is not None and prompt.claimed_by != claimed_by:
            return None
        prompt.dispatch_count += 1
        prompt.last_error = error[:2000]
        prompt.claimed_at = None
        prompt.claimed_by = None
        prompt.updated_at = utcnow()
        if prompt.dispatch_count >= max_attempts:
            prompt.status = StandupPromptStatus.FAILED.value
            prompt.next_attempt_at = None
        else:
            prompt.next_attempt_at = next_attempt_at
        s.add(prompt)
        await s.flush()
        await s.refresh(prompt)
        return prompt


async def release_prompt_claim(prompt_id: int, session: AsyncSession | None = None) -> None:
    """Give a claimed prompt back without counting an attempt.

    Args:
        prompt_id: The prompt.
        session: Optional session to reuse.
    """
    async with session_scope(session) as s:
        prompt = await s.get(DailyStandupPrompt, prompt_id)
        if prompt is None:
            return
        prompt.claimed_at = None
        prompt.claimed_by = None
        prompt.updated_at = utcnow()
        s.add(prompt)
        await s.flush()


async def mark_prompt_missed(
    prompt_id: int,
    *,
    closed_at: datetime,
    session: AsyncSession | None = None,
) -> DailyStandupPrompt | None:
    """Close a prompt whose local day ended without a reply (or without a dispatch).

    No ``daily_standups`` row is created here: a silent day is a fact about the
    prompt, not a fabricated standup entry.

    Args:
        prompt_id: The prompt.
        closed_at: The instant the day was closed.
        session: Optional session to reuse.

    Returns:
        DailyStandupPrompt | None: The updated row, or None when absent.
    """
    require_aware(closed_at, "closed_at")
    async with session_scope(session) as s:
        prompt = await s.get(DailyStandupPrompt, prompt_id)
        if prompt is None:
            return None
        prompt.status = StandupPromptStatus.MISSED.value
        prompt.closed_at = closed_at
        prompt.claimed_at = None
        prompt.claimed_by = None
        prompt.updated_at = closed_at
        s.add(prompt)
        await s.flush()
        await s.refresh(prompt)
        return prompt


async def list_outstanding_prompts(
    dm_channel_id: str | None = None,
    *,
    session: AsyncSession | None = None,
) -> list[DailyStandupPrompt]:
    """Every prompt still waiting on a reply or a dispatch, oldest day first.

    Args:
        dm_channel_id: When given, only prompts whose DM was posted to that channel.
        session: Optional session to reuse.

    Returns:
        list[DailyStandupPrompt]: Outstanding prompts by local day.
    """
    statement = select(DailyStandupPrompt).where(
        col(DailyStandupPrompt.status).in_([StandupPromptStatus.PENDING.value, StandupPromptStatus.DISPATCHED.value])
    )
    if dm_channel_id is not None:
        statement = statement.where(DailyStandupPrompt.dm_channel_id == dm_channel_id)
    statement = statement.order_by(DailyStandupPrompt.local_date, DailyStandupPrompt.id)  # type: ignore[arg-type]
    async with session_scope(session) as s:
        result = await s.exec(statement)
        return list(result.all())


async def mark_prompt_answered(
    prompt_id: int,
    *,
    answered_at: datetime,
    session: AsyncSession | None = None,
) -> DailyStandupPrompt | None:
    """Close a prompt whose reply was stored as the day's standup entry.

    Args:
        prompt_id: The prompt.
        answered_at: The instant the reply was accepted.
        session: Optional session to reuse.

    Returns:
        DailyStandupPrompt | None: The updated row, or None when absent.
    """
    require_aware(answered_at, "answered_at")
    async with session_scope(session) as s:
        prompt = await s.get(DailyStandupPrompt, prompt_id)
        if prompt is None:
            return None
        prompt.status = StandupPromptStatus.ANSWERED.value
        prompt.answered_at = answered_at
        prompt.closed_at = answered_at
        prompt.claimed_at = None
        prompt.claimed_by = None
        prompt.updated_at = answered_at
        s.add(prompt)
        await s.flush()
        await s.refresh(prompt)
        return prompt


async def create_standup_entry(
    *,
    sprint_id: int,
    learner_id: int,
    log_date: date,
    what_i_did: str,
    what_i_will_do: str,
    blockers: str | None,
    prompt_id: int,
    raw_response: str,
    submitted_at: datetime,
    timezone: str,
    session: AsyncSession | None = None,
) -> DailyStandup | None:
    """Insert the parsed entry for a today prompt, idempotently.

    ``ON CONFLICT DO NOTHING`` on the existing ``(sprint, learner, day)`` key
    means a racing double-answer cannot create a second entry: the loser simply
    receives ``None`` and the caller records a duplicate reply.

    Args:
        sprint_id: The sprint.
        learner_id: The person.
        log_date: The day (the prompt's local day).
        what_i_did: Parsed progress.
        what_i_will_do: Parsed plan.
        blockers: Parsed blockers, or None.
        prompt_id: The prompt the reply answered.
        raw_response: The raw reply text (lossless).
        submitted_at: When the reply arrived.
        timezone: The zone the day belongs to.
        session: Optional session to reuse.

    Returns:
        DailyStandup | None: The row when this call created it, or None on conflict.
    """
    require_aware(submitted_at, "submitted_at")
    statement = (
        pg_insert(DailyStandup)
        .values(
            sprint_id=sprint_id,
            learner_id=learner_id,
            log_date=log_date,
            what_i_did=what_i_did,
            what_i_will_do=what_i_will_do,
            blockers=blockers,
            prompt_id=prompt_id,
            raw_response=raw_response,
            submitted_at=submitted_at,
            timezone=timezone,
            created_at=submitted_at,
            updated_at=submitted_at,
        )
        .on_conflict_do_nothing(constraint="uq_daily_standups_sprint_learner_day")
        .returning(DailyStandup)
    )
    async with session_scope(session) as s:
        result = await s.exec(statement)
        return result.scalar_one_or_none()


async def record_reply(
    *,
    prompt_id: int,
    learner_id: int,
    dm_channel_id: str,
    post_id: str,
    post_root_id: str | None,
    raw_text: str,
    local_date: date | None,
    outcome: StandupReplyOutcome,
    received_at: datetime,
    session: AsyncSession | None = None,
) -> StandupReply | None:
    """Append one raw reply, idempotently.

    The unique ``post_id`` makes a re-delivered WebSocket event a no-op.

    Args:
        prompt_id: The prompt the reply answered for.
        learner_id: The person who wrote it.
        dm_channel_id: The DM channel it arrived in.
        post_id: The Mattermost post id.
        post_root_id: The thread root, when in-thread.
        raw_text: The message verbatim.
        local_date: The day it arrived, in the learner's zone.
        outcome: ``accepted``, ``duplicate`` or ``late``.
        received_at: When it was handled.
        session: Optional session to reuse.

    Returns:
        StandupReply | None: The row when this call created it, or None on conflict.
    """
    require_aware(received_at, "received_at")
    statement = (
        pg_insert(StandupReply)
        .values(
            prompt_id=prompt_id,
            learner_id=learner_id,
            dm_channel_id=dm_channel_id,
            post_id=post_id,
            post_root_id=post_root_id,
            raw_text=raw_text,
            local_date=local_date,
            outcome=outcome.value,
            received_at=received_at,
            created_at=received_at,
            updated_at=received_at,
        )
        .on_conflict_do_nothing(constraint="uq_standup_replies_post_id")
        .returning(StandupReply)
    )
    async with session_scope(session) as s:
        result = await s.exec(statement)
        return result.scalar_one_or_none()


async def count_prompts(
    *,
    status: StandupPromptStatus | None = None,
    session: AsyncSession | None = None,
) -> int:
    """Count prompts, optionally by status — for health and verification output.

    Args:
        status: Only this status, or all when None.
        session: Optional session to reuse.

    Returns:
        int: The count.
    """
    statement = select(DailyStandupPrompt)
    if status is not None:
        statement = statement.where(DailyStandupPrompt.status == status.value)
    async with session_scope(session) as s:
        result = await s.exec(statement)
        return len(list(result.all()))


async def remove_prompts_between(
    learner_id: int,
    local_date_lower: date,
    local_date_upper: date,
    session: AsyncSession | None = None,
) -> int:
    """Delete a learner's prompts within a day range (verification harness only).

    Args:
        learner_id: The person.
        local_date_lower: Inclusive lower bound.
        local_date_upper: Inclusive upper bound.
        session: Optional session to reuse.

    Returns:
        int: How many rows were removed.
    """
    async with session_scope(session) as s:
        result = await s.exec(
            select(DailyStandupPrompt).where(
                DailyStandupPrompt.learner_id == learner_id,
                DailyStandupPrompt.local_date >= local_date_lower,
                DailyStandupPrompt.local_date <= local_date_upper,
                and_(
                    col(DailyStandupPrompt.status).in_(
                        [StandupPromptStatus.PENDING.value, StandupPromptStatus.DISPATCHED.value]
                    )
                ),
            )
        )
        rows = list(result.all())
        for prompt in rows:
            await s.delete(prompt)
        await s.flush()
        return len(rows)


class StandupSummary(NamedTuple):
    """The submitted, blocked, and missing standups for one channel and day."""

    submitted_updates: list[dict[str, object]]
    blockers: list[dict[str, object]]
    missing_members: list[dict[str, object]]
    sprint_info: dict[str, object]
    message: str


async def get_standup_summary_for_channel(
    channel_id: str | UUID,
    target_date: date,
    session: AsyncSession | None = None,
) -> StandupSummary:
    """Summarize active channel members' standups for one sprint day.

    Authorization is evaluated from the stored membership before any summary
    data is read. Channel IDs are strings in the Mattermost schema; accepting a
    UUID here keeps the boundary convenient for callers that already parsed one.

    Args:
        channel_id: Mattermost channel ID.
        target_date: Calendar day to summarize.
        session: Optional session to reuse.

    Returns:
        StandupSummary: Submitted updates, missing active members, and sprint metadata.

    Raises:
        AuthorisationRefused: When the requester is not an active channel administrator.
        ValidationFailed: When there is no active sprint or the date is outside it.
    """
    channel_key = str(channel_id)
    requester = current_requester.get()
    await authorisation.require_channel_authority(
        requester,
        channel_key,
        allowed_roles=CHANNEL_ADMIN_ROLES,
        action="summarize_standups",
    )

    sprint = await sprints.get_active_sprint(channel_key, session=session)
    if sprint is None:
        raise authorisation.ValidationFailed(f"There is no active sprint for channel '{channel_key}'.")
    if not sprint.start_date <= target_date <= sprint.end_date:
        raise authorisation.ValidationFailed(
            f"The requested date {target_date.isoformat()} is outside the active sprint window "
            f"({sprint.start_date.isoformat()} to {sprint.end_date.isoformat()})."
        )

    members = await channels.list_channel_roles(channel_key, active_only=True, session=session)
    active_members = [
        {
            "user_id": member.user.id,
            "username": member.user.username,
            "display_name": member.user.display_name,
        }
        for member in members
        if member.user.id is not None
    ]
    active_member_ids = {member["user_id"] for member in active_members}

    assert sprint.id is not None
    entries = await list_daily_standups(sprint.id, log_date=target_date, session=session)
    submitted_updates = [
        {
            "learner_id": entry.learner_id,
            "what_i_did": entry.what_i_did,
            "what_i_will_do": entry.what_i_will_do,
            "blockers": entry.blockers,
        }
        for entry in entries
        if entry.learner_id in active_member_ids
    ]
    submitted_ids = {update["learner_id"] for update in submitted_updates}
    blockers = [
        {
            "learner_id": update["learner_id"],
            "blockers": update["blockers"],
        }
        for update in submitted_updates
        if isinstance(update["blockers"], str) and update["blockers"].strip()
    ]
    missing_members = [member for member in active_members if member["user_id"] not in submitted_ids]
    message = (
        f"No daily standup updates were submitted for {target_date.isoformat()} in this channel."
        if not submitted_updates
        else f"Daily standup summary for {target_date.isoformat()}."
    )

    return StandupSummary(
        submitted_updates=submitted_updates,
        blockers=blockers,
        missing_members=missing_members,
        sprint_info={
            "id": sprint.id,
            "name": sprint.name,
            "start_date": sprint.start_date.isoformat(),
            "end_date": sprint.end_date.isoformat(),
        },
        message=message,
    )
