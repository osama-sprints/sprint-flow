"""Announcement service: preview, confirmation/idempotency, rate limiting, dispatch.

Four fixes in this version, all confirmed against real code already
reviewed (authorisation.py, channels.py, announcement.py):

1. resolve_announcement_channel previously accepted a `channel_id` directly
   from the caller and only authorisation-checked it — this is exactly the
   "arbitrary channel override" the task brief prohibits. It now takes
   `cohort_id`, resolves the channel from the stored Sprint/cohort record
   itself, and authorises against that resolved value. The caller has no way
   to supply a channel id.
2. create_announcement_preview built an `Announcement(...)` without
   `cohort_id`, which is a required (non-optional) field on the model — this
   raised a pydantic ValidationError on every single call. Fixed to accept
   and pass cohort_id through.
3. resolve_recipients_by_role was calling
   channel_repo.list_channel_roles(session, channel_id=channel_id) against a
   real signature of (channel_id, *, active_only=True, session=None) — the
   session object was being passed positionally into the channel_id
   parameter. Fixed to match the real signature.
4. resolve_recipients_by_usernames had the same class of bug against
   channel_repo.get_role_for_user_in_channel's real signature of
   (user_id, channel_id, *, active_only=True, session=None).
"""

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from sqlmodel import func, select, update
from sqlmodel.ext.asyncio.session import AsyncSession

from app.core.requester import RequesterContext
from app.models.announcement import Announcement
from app.models.enums import AnnouncementOutcome
from app.services.authorisation import (
    ValidationFailed,
    require_channel_authority,
)
from app.services.domain import channels as channel_repo
from app.services.domain import identity as identity_repo
from app.services.domain import sprints as sprint_repo

RATE_LIMIT_MAX_REQUESTS = 3
RATE_LIMIT_WINDOW_MINUTES = 60


async def send_to_mattermost(channel_id: str, message: str) -> str:
    if not channel_id or channel_id == "invalid_channel":
        raise ValueError("Invalid target channel or network delivery failure.")
    return f"mm_post_{int(datetime.now(timezone.utc).timestamp())}"


async def is_rate_limited(
    session: AsyncSession,
    channel_id: str,
    now: Optional[datetime] = None,
) -> bool:
    if now is None:
        now = datetime.now(timezone.utc)

    window_start = now - timedelta(minutes=RATE_LIMIT_WINDOW_MINUTES)

    stmt = select(func.count(Announcement.id)).where(
        Announcement.resolved_channel_id == channel_id,
        Announcement.outcome == AnnouncementOutcome.SENT,
        Announcement.status_changed_at >= window_start,
    )
    result = await session.exec(stmt)
    count = result.one()
    if not isinstance(count, int):
        return False
    return count >= RATE_LIMIT_MAX_REQUESTS


async def cancel_announcement(
    session: AsyncSession,
    announcement_id: int,
) -> Dict[str, Any]:
    stmt = (
        update(Announcement)
        .where(
            Announcement.id == announcement_id,
            Announcement.confirmation_status == "pending",
        )
        .values(
            confirmation_status="cancelled",
            outcome=AnnouncementOutcome.CANCELLED,
            status_changed_at=datetime.now(timezone.utc),
        )
    )

    result = await session.exec(stmt)
    await session.commit()

    if result.rowcount == 0:
        return {
            "status": "cannot_cancel",
            "message": "Announcement is already processed or not pending.",
            "cancelled": False,
        }

    return {
        "status": "cancelled",
        "message": "Announcement was cancelled.",
        "cancelled": True,
        "outcome": AnnouncementOutcome.CANCELLED,
    }


async def confirm_and_dispatch_announcement(
    session: AsyncSession,
    announcement_id: int,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    if now is None:
        now = datetime.now(timezone.utc)

    stmt = (
        update(Announcement)
        .where(
            Announcement.id == announcement_id,
            Announcement.confirmation_status == "pending",
        )
        .values(confirmation_status="confirmed", status_changed_at=now)
    )

    result = await session.exec(stmt)
    await session.commit()

    if result.rowcount == 0:
        return {
            "status": "already_processed",
            "message": "Announcement is already confirmed or processed.",
            "dispatched": False,
        }

    announcement = await session.get(Announcement, announcement_id)
    if not announcement:
        return {
            "status": "error",
            "message": "Announcement not found.",
            "dispatched": False,
        }

    channel_id = announcement.resolved_channel_id or ""

    if await is_rate_limited(session, channel_id, now=now):
        rate_limit_stmt = (
            update(Announcement)
            .where(Announcement.id == announcement_id)
            .values(outcome=AnnouncementOutcome.RATE_LIMITED)
        )
        await session.exec(rate_limit_stmt)
        await session.commit()

        return {
            "status": "rate_limited",
            "message": "Rate limit exceeded for this channel.",
            "dispatched": False,
            "outcome": AnnouncementOutcome.RATE_LIMITED,
        }

    try:
        post_id = await send_to_mattermost(
            channel_id=channel_id,
            message=announcement.exact_text or "",
        )

        final_stmt = (
            update(Announcement)
            .where(Announcement.id == announcement_id)
            .values(outcome=AnnouncementOutcome.SENT, mattermost_post_id=post_id)
        )
        await session.exec(final_stmt)
        await session.commit()

        return {
            "status": "success",
            "message": "Announcement confirmed and dispatched.",
            "dispatched": True,
            "outcome": AnnouncementOutcome.SENT,
            "mattermost_post_id": post_id,
        }

    except Exception as exc:
        fail_stmt = (
            update(Announcement)
            .where(Announcement.id == announcement_id)
            .values(outcome=AnnouncementOutcome.FAILED)
        )
        await session.exec(fail_stmt)
        await session.commit()

        return {
            "status": "failed",
            "message": f"Dispatch failed: {str(exc)}",
            "dispatched": False,
            "outcome": AnnouncementOutcome.FAILED,
            "error_detail": str(exc),
        }


async def create_announcement_preview(
    session: AsyncSession,
    cohort_id: int,
    raw_text: str,
    delivery_mode: str,
    resolved_channel: str,
    resolved_audience: List[Dict[str, Any]],
    created_by_user_id: int,
) -> Dict[str, Any]:
    """Build the preview and write the audit row — posts nothing.

    cohort_id is required here (fixed): Announcement.cohort_id has no
    default, so omitting it raised a ValidationError on every call.
    """
    preview_data = {
        "final_text": raw_text,
        "cohort_id": cohort_id,
        "resolved_channel": resolved_channel,
        "resolved_audience": resolved_audience,
        "delivery_mode": delivery_mode,
    }

    audit_entry = Announcement(
        cohort_id=cohort_id,
        requester_id=created_by_user_id,
        exact_text=raw_text,
        delivery_mode=delivery_mode,
        resolved_channel_id=resolved_channel,
        confirmation_status="pending",
        mattermost_post_id=None,
    )

    session.add(audit_entry)
    await session.commit()
    await session.refresh(audit_entry)

    return {
        "audit_id": audit_entry.id,
        "outcome": audit_entry.outcome,
        "preview": preview_data,
        "mattermost_post_id": audit_entry.mattermost_post_id,
    }


async def resolve_announcement_channel(
    session: AsyncSession,
    requester: RequesterContext,
    cohort_id: int,
) -> str:
    """Resolve the channel strictly from the stored cohort record.

    The caller supplies cohort_id — never a channel_id — so there is no
    parameter through which an arbitrary channel could be substituted. The
    channel used for both authorisation and dispatch is always
    sprint.channel_id, read fresh from the database.
    """
    sprint = await sprint_repo.get_sprint(cohort_id, session)
    if not sprint:
        raise ValidationFailed(f"Cohort {cohort_id} not found.")
    if sprint.status != "active":
        raise ValidationFailed(f"Cohort {cohort_id} is not active (current status: {sprint.status}).")

    await require_channel_authority(requester, sprint.channel_id, action="send_announcement")
    return sprint.channel_id


async def resolve_recipients_by_role(
    session: AsyncSession,
    channel_id: str,
    role: str,
) -> List[Dict[str, Any]]:
    """Resolve every active member of a channel holding the given role."""
    members = await channel_repo.list_channel_roles(channel_id, session=session)
    matching = [m for m in members if str(m.role.key).lower() == role.lower()]

    return [
        {"user_id": m.user.id, "username": m.user.username, "role": role, "channel_id": channel_id}
        for m in matching
    ]


async def resolve_recipients_by_usernames(
    session: AsyncSession,
    channel_id: str,
    usernames: List[str],
) -> List[Dict[str, Any]]:
    resolved_recipients = []

    for username in usernames:
        user = await identity_repo.get_user_by_username(session, username)
        if not user:
            raise ValidationFailed(f"User '{username}' does not exist.")

        role = await channel_repo.get_role_for_user_in_channel(user.id, channel_id, session=session)
        if role is None:
            raise ValidationFailed(f"User '{username}' is not a member of channel {channel_id}.")

        resolved_recipients.append(
            {"user_id": user.id, "username": user.username, "role": role.key, "channel_id": channel_id}
        )

    return resolved_recipients