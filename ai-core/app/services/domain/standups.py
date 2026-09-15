"""Daily progress entries and channel-scoped summaries."""

from datetime import date
from typing import NamedTuple
from uuid import UUID

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models import (
    DailyStandup,
    utcnow,
)
from app.models.enums import CHANNEL_ADMIN_ROLES
from app.core.requester import current_requester
from app.services import authorisation
from app.services.domain import channels, sprints
from app.services.database import session_scope


class StandupSummary(NamedTuple):
    """The submitted and missing standups for one channel and calendar day."""

    submitted_updates: list[dict[str, object]]
    missing_members: list[dict[str, object]]
    sprint_info: dict[str, object]


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
    missing_members = [member for member in active_members if member["user_id"] not in submitted_ids]

    return StandupSummary(
        submitted_updates=submitted_updates,
        missing_members=missing_members,
        sprint_info={
            "id": sprint.id,
            "name": sprint.name,
            "start_date": sprint.start_date.isoformat(),
            "end_date": sprint.end_date.isoformat(),
        },
    )
