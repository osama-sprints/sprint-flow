from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from sqlmodel import func, select, update
from sqlmodel.ext.asyncio.session import AsyncSession

from app.core.requester import RequesterContext
from app.models.announcement import Announcement
from app.models.enums import AnnouncementOutcome
from app.services.authorisation import (
    AuthorisationRefused,
    ValidationFailed,
    require_channel_authority,
)
from app.services.domain import channels as channel_repo
from app.services.domain import identity as identity_repo
from app.services.domain import sprints as sprint_repo
from app.services.mattermost import mattermost_client

RATE_LIMIT_MAX_REQUESTS = 3
RATE_LIMIT_WINDOW_MINUTES = 60


async def send_to_mattermost(channel_id: str, message: str) -> str:
    post = await mattermost_client.create_post(channel_id=channel_id, message=message)
    if post is None or not post.get("id"):
        raise RuntimeError(f"Mattermost post failed for channel {channel_id!r}")
    return post["id"]


def format_announcement_text(raw_message: str, delivery_mode: str) -> str:
    if delivery_mode == "dm":
        return f"📢 **[Sprint Announcement]**\n\n{raw_message}"
    return f"📢 **[Announcement]**\n\n{raw_message}"


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
    confirming_user_id: int,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    if now is None:
        now = datetime.now(timezone.utc)
    stmt = (
        update(Announcement)
        .where(
            Announcement.id == announcement_id,
            Announcement.confirmation_status == "pending",
            Announcement.requester_id == confirming_user_id,
        )
        .values(confirmation_status="confirmed", status_changed_at=now)
    )

    result = await session.exec(stmt)
    await session.commit()

    if result.rowcount == 0:
        existing = await session.get(Announcement, announcement_id)
        if existing is not None and existing.requester_id != confirming_user_id:
            return {
                "status": "not_authorized",
                "message": "Only the person who created this announcement may confirm it.",
                "dispatched": False,
            }
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
        
    target_channel_id = announcement.resolved_channel_id or ""
    if announcement.delivery_mode == "dm":
        target_username = getattr(announcement, "target_value", None) or getattr(announcement, "target_username", "learner")
        target_username = target_username.strip().lstrip("@")
        
        mm_user = await mattermost_client.get_user_by_username(target_username)
        if not mm_user:
            return {
                "status": "failed",
                "message": f"Dispatch failed: Recipient Mattermost user '@{target_username}' not found.",
                "dispatched": False,
                "outcome": AnnouncementOutcome.FAILED,
            }
        
        dm_channel = await mattermost_client.create_direct_channel(mm_user["id"])
        if not dm_channel or "id" not in dm_channel:
            return {
                "status": "failed",
                "message": f"Dispatch failed: Could not create direct channel with user {mm_user['id']}.",
                "dispatched": False,
                "outcome": AnnouncementOutcome.FAILED,
            }
        
        target_channel_id = dm_channel["id"]
        announcement.resolved_channel_id = target_channel_id
        session.add(announcement)
        await session.commit()

    if await is_rate_limited(session, target_channel_id, now=now):
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
        formatted_message = format_announcement_text(
            announcement.exact_text or "", 
            announcement.delivery_mode
        )

        post_id = await send_to_mattermost(
            channel_id=target_channel_id,
            message=formatted_message,
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
    target_type: Optional[str] = None,   
    target_value: Optional[str] = None,  
) -> Dict[str, Any]:
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
        target_type=target_type,    
        target_value=target_value,  
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
    sprint = await sprint_repo.get_sprint(cohort_id, session)
    if not sprint:
        raise ValidationFailed(f"Cohort {cohort_id} not found.")
    if sprint.status != "active":
        raise ValidationFailed(f"Cohort {cohort_id} is not active (current status: {sprint.status}).")

    await require_channel_authority(requester, sprint.channel_id, action="send_announcement")
    return sprint.channel_id


async def request_announcement(
    session: AsyncSession,
    requester: RequesterContext,
    cohort_id: int,
    raw_text: str,
    delivery_mode: str,
    created_by_user_id: int,
    target_type: str,
    target_value: Optional[str] = None,
) -> Dict[str, Any]:
    try:
        channel_id = await resolve_announcement_channel(session, requester, cohort_id)

        if target_type == "role" and target_value:
            resolved_audience = await resolve_recipients_by_role(session, channel_id, target_value)
        elif target_type == "usernames" and target_value:
            usernames = [u.strip() for u in target_value.split(",")]
            resolved_audience = await resolve_recipients_by_usernames(session, channel_id, usernames)
        else:
            resolved_audience = []

        return await create_announcement_preview(
            session=session,
            cohort_id=cohort_id,
            raw_text=raw_text,
            delivery_mode=delivery_mode,
            resolved_channel=channel_id,
            resolved_audience=resolved_audience,
            created_by_user_id=created_by_user_id,
            target_type=target_type,    
            target_value=target_value,  
        )

    except AuthorisationRefused:
        await _write_refusal_audit_row(
            session,
            cohort_id=cohort_id,
            requester_id=created_by_user_id,
            raw_text=raw_text,
            delivery_mode=delivery_mode,
            outcome=AnnouncementOutcome.UNAUTHORIZED,
        )
        raise
    except ValidationFailed:
        safe_cohort_id = cohort_id if await sprint_repo.get_sprint(cohort_id, session) is not None else None
        await _write_refusal_audit_row(
            session,
            cohort_id=safe_cohort_id,
            requester_id=created_by_user_id,
            raw_text=raw_text,
            delivery_mode=delivery_mode,
            outcome=AnnouncementOutcome.FAILED,
        )
        raise


async def _write_refusal_audit_row(
    session: AsyncSession,
    *,
    cohort_id: Optional[int],
    requester_id: int,
    raw_text: str,
    delivery_mode: str,
    outcome: AnnouncementOutcome,
) -> None:
    audit_entry = Announcement(
        cohort_id=cohort_id,
        requester_id=requester_id,
        exact_text=raw_text,
        delivery_mode=delivery_mode,
        resolved_channel_id="",
        confirmation_status="refused",
        outcome=outcome,
    )
    session.add(audit_entry)
    await session.commit()


async def resolve_recipients_by_role(
    session: AsyncSession,
    channel_id: str,
    role: str,
) -> List[Dict[str, Any]]:
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
        clean_username = username.strip().lstrip("@")
        user = await identity_repo.get_user_by_username(clean_username, session)
        if not user:
            raise ValidationFailed(f"User '{username}' does not exist.")

        role = await channel_repo.get_role_for_user_in_channel(user.id, channel_id, session=session)
        if role is None:
            raise ValidationFailed(f"User '{username}' is not a member of channel {channel_id}.")

        resolved_recipients.append(
            {"user_id": user.id, "username": user.username, "role": role.key, "channel_id": channel_id}
        )

    return resolved_recipients