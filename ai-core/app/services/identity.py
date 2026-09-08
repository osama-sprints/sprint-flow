"""Mattermost identity → stored ``users`` row → ``RequesterContext``.

Every inbound event carries a Mattermost user id. This module reads the
profile back from Mattermost (cached), upserts the ``users`` row, computes the
superadmin flag from the ``ADMIN_EMAILS`` allowlist, and snapshots the person's
active cohort roles for routing. The result is bound to ``current_requester``
by the conversation layer before the graph runs.

The profile read is cached per user for ``IDENTITY_PROFILE_CACHE_TTL`` seconds
so the common path costs one indexed upsert and one membership query, never a
Mattermost round trip per message.
"""

import json
from types import MappingProxyType
from typing import (
    Any,
    Mapping,
)

from app.core.cache import (
    cache_key,
    cache_service,
)
from app.core.config import settings
from app.core.logging import logger
from app.core.requester import RequesterContext
from app.models import User
from app.services.domain import cohorts as cohort_repo
from app.services.domain import identity as identity_repo
from app.services.mattermost import mattermost_client


def profile_timezone(profile: Mapping[str, Any]) -> str | None:
    """Extract the IANA zone a Mattermost profile declares.

    Mattermost stores ``{"useAutomaticTimezone": "true", "automaticTimezone":
    "Europe/Berlin", "manualTimezone": ""}``; the flag is a string in most
    responses and a bool in some.

    Args:
        profile: The ``/users/{id}`` response.

    Returns:
        str | None: The zone name, or None when the profile declares none.
    """
    zone = profile.get("timezone") or {}
    if not isinstance(zone, dict):
        return None
    automatic = str(zone.get("useAutomaticTimezone", "")).lower() in ("true", "1")
    candidate = zone.get("automaticTimezone") if automatic else zone.get("manualTimezone")
    candidate = (candidate or zone.get("automaticTimezone") or zone.get("manualTimezone") or "").strip()
    return candidate or None


def display_name_of(profile: Mapping[str, Any]) -> str | None:
    """Pick the friendliest name a profile offers.

    Args:
        profile: The ``/users/{id}`` response.

    Returns:
        str | None: First name, full name, nickname or username — whichever exists first.
    """
    first = str(profile.get("first_name") or "").strip()
    last = str(profile.get("last_name") or "").strip()
    full = " ".join(part for part in (first, last) if part)
    return full or str(profile.get("nickname") or "").strip() or str(profile.get("username") or "").strip() or None


def is_allowlisted_admin(email: str | None, profile: Mapping[str, Any] | None = None) -> bool:
    """Whether a Mattermost account qualifies as a platform administrator.

    The email must be on the ``ADMIN_EMAILS`` allowlist AND the account must be
    trustworthy for that address: either Mattermost marks the email verified or
    the account holds the ``system_admin`` role. On an open server a person can
    change their own profile email, so the allowlist alone is not enough.

    Args:
        email: Lower-cased email, or None.
        profile: The Mattermost profile (``email_verified``, ``roles``), or None.

    Returns:
        bool: True when the account is a superadmin.
    """
    if not email or email not in settings.ADMIN_EMAILS:
        return False
    if profile is None:
        logger.warning("identity_allowlisted_email_not_trusted", email=email, reason="no_profile")
        return False
    verified = str(profile.get("email_verified", "")).lower() in ("true", "1")
    roles = str(profile.get("roles") or "").split()
    if verified or "system_admin" in roles:
        return True
    logger.warning(
        "identity_allowlisted_email_not_trusted",
        email=email,
        mattermost_user_id=profile.get("id"),
        reason="email_unverified_and_not_system_admin",
    )
    return False


async def fetch_profile(mattermost_user_id: str) -> dict[str, Any] | None:
    """Read a Mattermost profile, caching successful reads.

    Args:
        mattermost_user_id: The Mattermost user id.

    Returns:
        dict | None: The profile, or None when Mattermost could not supply it.
    """
    key = cache_key("mm_user_profile", mattermost_user_id)
    cached = await cache_service.get(key)
    if cached:
        try:
            return json.loads(cached)
        except ValueError:
            logger.warning("identity_profile_cache_corrupt", mattermost_user_id=mattermost_user_id)

    profile = await mattermost_client.get_user(mattermost_user_id)
    if not profile or not profile.get("id"):
        return None
    await cache_service.set(key, json.dumps(profile), ttl=settings.IDENTITY_PROFILE_CACHE_TTL)
    return profile


async def sync_mattermost_user(mattermost_user_id: str, profile: Mapping[str, Any] | None = None) -> User | None:
    """Create or refresh the ``users`` row for a Mattermost account.

    Args:
        mattermost_user_id: The Mattermost user id.
        profile: An already-fetched profile, or None to fetch (cached).

    Returns:
        User | None: The stored row, or None when the profile could not be read.
    """
    data = dict(profile) if profile is not None else await fetch_profile(mattermost_user_id)
    if not data:
        logger.warning("identity_sync_no_profile", mattermost_user_id=mattermost_user_id)
        return None

    email = str(data.get("email") or "").strip().lower() or None
    username = str(data.get("username") or "").strip() or mattermost_user_id
    user = await identity_repo.upsert_mattermost_user(
        mattermost_user_id=mattermost_user_id,
        username=username,
        email=email,
        display_name=display_name_of(data),
        timezone=profile_timezone(data),
        is_superadmin=is_allowlisted_admin(email, data),
    )
    logger.debug("identity_synced", user_id=user.id, username=username, is_superadmin=user.is_superadmin)
    return user


async def resolve_person(reference: str) -> User | None:
    """Resolve what a person typed — ``@handle`` or an email — to a stored user, syncing from Mattermost if needed.

    Args:
        reference: ``@alice``, ``alice`` or ``alice@example.com``.

    Returns:
        User | None: The stored row, or None when Mattermost knows no such account.
    """
    text = reference.strip()
    if not text:
        return None
    if "@" in text and not text.startswith("@"):
        profile = await mattermost_client.get_user_by_email(text.lower())
    else:
        profile = await mattermost_client.get_user_by_username(text.lstrip("@"))
    if profile and profile.get("id"):
        return await sync_mattermost_user(str(profile["id"]), profile)
    # Mattermost may be unreachable; fall back to what is already stored.
    if "@" in text and not text.startswith("@"):
        return await identity_repo.get_user_by_email(text)
    return await identity_repo.get_user_by_username(text)


async def resolve_requester(
    *,
    mattermost_user_id: str,
    username: str = "",
    channel_id: str = "",
    channel_type: str = "",
    thread_root_id: str = "",
) -> RequesterContext:
    """Build the turn's requester context from stored data. Never raises.

    Args:
        mattermost_user_id: The id on the Mattermost event.
        username: The handle the transport supplied, if any.
        channel_id: Where the message arrived.
        channel_type: O, P, D or G.
        learner_thread_id: The post id a proactive reply to this turn must be
        threaded under (mirrors ``IncomingMessage`` in ``app.services.conversation``:
        the trigger's ``root_id`` when already in a thread, else its
        ``post_id``). Used only by tools that must correlate a later,
        out-of-band reply back to this conversation (escalation).

    Returns:
        RequesterContext: Always returned; degraded (no ``user_id``) when the sync failed.
    """
    base = RequesterContext(
        mattermost_user_id=mattermost_user_id,
        username=username,
        channel_id=channel_id,
        channel_type=channel_type,
        learner_thread_id=thread_root_id,
    )
    if not mattermost_user_id:
        return base

    try:
        user = await sync_mattermost_user(mattermost_user_id)
    except Exception as e:
        logger.exception("identity_sync_failed", mattermost_user_id=mattermost_user_id, error=str(e))
        return base
    if user is None or user.id is None:
        return base

    try:
        memberships = await cohort_repo.list_memberships_for_user(user.id, active_only=True)
    except Exception as e:
        logger.exception("identity_memberships_lookup_failed", user_id=user.id, error=str(e))
        memberships = []
    roles: dict[int, str] = {}
    for _membership, cohort, role in memberships:
        if cohort.id is not None:
            roles[cohort.id] = role.key

    return RequesterContext(
        mattermost_user_id=mattermost_user_id,
        username=user.username or username,
        email=user.email,
        channel_id=channel_id,
        channel_type=channel_type,
        user_id=user.id,
        is_superadmin=user.is_superadmin,
        timezone=user.timezone,
        cohort_roles=MappingProxyType(roles),
        learner_thread_id=thread_root_id,
    )
