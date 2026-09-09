"""Proactive ceremony reminder poller.

Sends DMs to every cohort member ahead of each scheduled ceremony:
- 24 hours before (``"24h"`` window)
-  1 hour before  (``"1h"`` window)

The ``CeremonyReminder`` table is the sole idempotency guarantee. A row
existing for ``(ceremony_id, recipient_mm_id, window)`` means the DM was
already sent. A service restart re-runs the query, finds the same due
ceremonies, hits the table and does nothing — restart-safe by construction.

No cron library, no Celery. A plain ``async def reminder_poller()`` loop is
started as an ``asyncio.create_task`` in ``app.main``'s lifespan, exactly
like the ``onboarding_dispatcher``.

Architectural note — cohort_id is gone from Ceremony:
    The distributed branch removed ``cohort_id`` from the ``Ceremony`` model.
    Ceremonies are now scoped by ``channel_id`` + ``team_id``. The link back
    to a cohort (and therefore to its members) is:
        ``ceremony.channel_id == cohort.mattermost_channel_id``
    A ceremony whose channel maps to no active cohort is skipped — there are
    no members to remind.
"""

import asyncio
from datetime import (
    datetime,
    timedelta,
    timezone,
)
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy.exc import IntegrityError
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.core.logging import logger
from app.models import (
    Ceremony,
    Cohort,
    utcnow,
)
from app.models.ceremony_reminder import CeremonyReminder
from app.services.database import session_scope
from app.services.domain import ceremonies as ceremony_repo
from app.services.domain import cohorts as cohort_repo
from app.services.mattermost import mattermost_client

# Poll every 5 minutes.
_POLL_INTERVAL_SECONDS = 300

# ±6 min margin around the exact window boundary covers the 5-minute poll
# interval with a one-minute buffer.
_POLL_MARGIN_MINUTES = 6

# Reminder windows: (hours_before, label)
_WINDOWS: list[tuple[int, str]] = [(24, "24h"), (1, "1h")]

# Human-readable labels for the DM text.
_TIME_LABELS: dict[str, str] = {"24h": "24 hours", "1h": "1 hour"}


# ---------------------------------------------------------------------------
# Internal DB helpers
# ---------------------------------------------------------------------------


async def _get_cohort_by_channel_id(channel_id: str, session: AsyncSession | None = None) -> Cohort | None:
    """Fetch the cohort that owns this Mattermost channel, if any.

    The link between a distributed ceremony and a cohort is
    ``ceremony.channel_id == cohort.mattermost_channel_id``.

    Args:
        channel_id: The Mattermost channel id from the ceremony row.
        session: Optional session to reuse.

    Returns:
        Cohort | None: The cohort, or None when no cohort is linked to the channel.
    """
    async with session_scope(session) as s:
        result = await s.exec(select(Cohort).where(Cohort.mattermost_channel_id == channel_id))
        return result.first()


async def _already_sent(
    ceremony_id: int,
    recipient_mm_id: str,
    window: str,
    session: AsyncSession | None = None,
) -> bool:
    """Check whether a reminder row already exists for this triple.

    Args:
        ceremony_id: The ceremony.
        recipient_mm_id: The recipient's Mattermost user id.
        window: ``"24h"`` or ``"1h"``.
        session: Optional session to reuse.

    Returns:
        bool: True when the DM was already sent (row exists).
    """
    async with session_scope(session) as s:
        result = await s.exec(
            select(CeremonyReminder).where(
                CeremonyReminder.ceremony_id == ceremony_id,
                CeremonyReminder.recipient_mm_id == recipient_mm_id,
                CeremonyReminder.window == window,
            )
        )
        return result.first() is not None


async def _record_sent(
    ceremony_id: int,
    recipient_mm_id: str,
    window: str,
    sent_at: datetime,
    session: AsyncSession | None = None,
) -> bool:
    """Insert a reminder record. Returns False (and ignores) on a duplicate.

    The unique constraint makes a second insert for the same triple raise
    ``IntegrityError``; we catch it so a race between two pollers (e.g. two
    containers) never propagates an exception.

    Args:
        ceremony_id: The ceremony.
        recipient_mm_id: The recipient's Mattermost user id.
        window: ``"24h"`` or ``"1h"``.
        sent_at: When the DM was posted (UTC).
        session: Optional session to reuse.

    Returns:
        bool: True when the row was inserted, False when it already existed.
    """
    row = CeremonyReminder(
        ceremony_id=ceremony_id,
        recipient_mm_id=recipient_mm_id,
        window=window,
        sent_at=sent_at,
    )
    try:
        async with session_scope(session) as s:
            s.add(row)
            await s.flush()
        return True
    except IntegrityError:
        logger.debug(
            "ceremony_reminder_duplicate_skipped",
            ceremony_id=ceremony_id,
            recipient_mm_id=recipient_mm_id,
            window=window,
        )
        return False


# ---------------------------------------------------------------------------
# Public query helpers (tested directly in unit tests)
# ---------------------------------------------------------------------------


async def due_ceremonies(
    window_hours: int,
    *,
    now: datetime | None = None,
    poll_margin_minutes: int = _POLL_MARGIN_MINUTES,
) -> list[Ceremony]:
    """Return scheduled ceremonies whose start falls inside the reminder window.

    The window is centred on ``now + window_hours``:
        ``now + W - margin ≤ scheduled_at ≤ now + W + margin``

    Only non-cancelled ceremonies are returned (the status filter comes from
    the existing ``list_upcoming_ceremonies`` helper).

    Args:
        window_hours: Hours before the ceremony to send this reminder.
        now: Reference instant; defaults to ``utcnow()``.
        poll_margin_minutes: Half-width of the matching band (default 6 min).

    Returns:
        list[Ceremony]: Ceremonies that need this reminder now.
    """
    reference = now or utcnow()
    margin = timedelta(minutes=poll_margin_minutes)
    target = reference + timedelta(hours=window_hours)
    lower = target - margin
    upper = target + margin

    # list_upcoming_ceremonies already filters status == "scheduled".
    # We pass `now=lower` and `within=(upper-lower)` to get everything in band.
    all_upcoming = await ceremony_repo.list_upcoming_ceremonies(
        within=upper - reference,
        now=reference,
    )
    return [c for c in all_upcoming if lower <= c.scheduled_at <= upper]


async def get_channel_members(channel_id: str) -> list[str]:
    """Return Mattermost user ids for every active member of the cohort linked to this channel.

    The distributed branch scopes ceremonies by ``channel_id``, not by
    ``cohort_id``. We resolve the channel to a cohort through
    ``Cohort.mattermost_channel_id``, then list the cohort's active members.
    A ceremony with no matching active cohort produces an empty list (no DMs).

    Args:
        channel_id: The Mattermost channel id from the ceremony row.

    Returns:
        list[str]: Mattermost user ids, skipping members without one.
    """
    cohort = await _get_cohort_by_channel_id(channel_id)
    if cohort is None:
        logger.debug("ceremony_reminder_no_cohort_for_channel", channel_id=channel_id)
        return []
    if not cohort.is_active:
        logger.debug(
            "ceremony_reminder_cohort_inactive",
            channel_id=channel_id,
            cohort_id=cohort.id,
            cohort_name=cohort.name,
        )
        return []

    assert cohort.id is not None
    members = await cohort_repo.list_cohort_members(cohort.id, active_only=True)
    return [m.user.mattermost_user_id for m in members if m.user.mattermost_user_id]


# ---------------------------------------------------------------------------
# Time formatting
# ---------------------------------------------------------------------------


def _format_local(dt: datetime, iana_tz: str) -> tuple[str, str]:
    """Format a UTC datetime in the given IANA timezone.

    Args:
        dt: A timezone-aware datetime (UTC).
        iana_tz: IANA timezone name, e.g. ``"Asia/Cairo"``.

    Returns:
        tuple[str, str]: ``(formatted_string, canonical_tz_name)`` where
        formatted_string is ``"Friday 4 September 2026, 14:00"`` and
        canonical_tz_name is the resolved zone name (or ``"UTC"`` on error).
    """
    try:
        zone = ZoneInfo(iana_tz)
        local = dt.astimezone(zone)
        formatted = local.strftime("%A %-d %B %Y, %H:%M")
        return formatted, iana_tz
    except (ZoneInfoNotFoundError, Exception):
        utc_dt = dt.astimezone(timezone.utc)
        formatted = utc_dt.strftime("%A %-d %B %Y, %H:%M")
        return formatted, "UTC"


# ---------------------------------------------------------------------------
# Organizer name helper
# ---------------------------------------------------------------------------


async def _organizer_display(organizer_id: int) -> str:
    """Return a display name or fallback for the ceremony organizer.

    Args:
        organizer_id: ``users.id`` of the organizer.

    Returns:
        str: Display name, username with ``@``, or ``"unknown"``.
    """
    from app.services.domain import identity as identity_repo  # local to avoid cycles

    user = await identity_repo.get_user(organizer_id)
    if user is None:
        return "unknown"
    if user.display_name:
        return user.display_name
    if user.username:
        return f"@{user.username}"
    return "unknown"


# ---------------------------------------------------------------------------
# Core send function
# ---------------------------------------------------------------------------


async def send_reminder(
    ceremony: Ceremony,
    member_mm_id: str,
    window: str,
    ceremony_type_label: str,
) -> bool:
    """Send one reminder DM and record it. Never raises.

    Steps:
    1. Check ``CeremonyReminder`` — skip if already sent.
    2. Fetch the user's timezone from Mattermost (falls back to UTC).
    3. Format ``scheduled_at`` in local time.
    4. Build the DM text.
    5. Open DM channel → post message.
    6. Insert ``CeremonyReminder`` row.

    Args:
        ceremony: The ceremony whose reminder this is.
        member_mm_id: Mattermost user id of the recipient.
        window: ``"24h"`` or ``"1h"``.
        ceremony_type_label: Human-readable type name, e.g. ``"Sprint Planning"``.

    Returns:
        bool: True on success, False on any failure (never raises).
    """
    assert ceremony.id is not None

    try:
        if await _already_sent(ceremony.id, member_mm_id, window):
            logger.debug(
                "ceremony_reminder_already_sent",
                ceremony_id=ceremony.id,
                recipient_mm_id=member_mm_id,
                window=window,
            )
            return False

        # Fetch user's timezone — falls back to "UTC" on any failure.
        tz_name = await mattermost_client.get_user_timezone(member_mm_id)

        local_time_str, resolved_tz = _format_local(ceremony.scheduled_at, tz_name)
        time_label = _TIME_LABELS.get(window, window)
        organizer = await _organizer_display(ceremony.organizer_id)

        agenda_line = f"\n📋 Agenda: {ceremony.agenda}" if ceremony.agenda else ""
        meet_line = f"\n🔗 Join: {ceremony.meet_link}" if ceremony.meet_link else ""

        message = (
            f"📅 Reminder: **{ceremony_type_label}** in {time_label}\n"
            f"🕐 {local_time_str} ({resolved_tz})\n"
            f"👤 Organised by: {organizer}"
            f"{agenda_line}"
            f"{meet_line}\n\n"
            f"This is an automated reminder from SprintFlow."
        )

        channel = await mattermost_client.create_direct_channel(member_mm_id)
        if not channel or not channel.get("id"):
            logger.warning(
                "ceremony_reminder_dm_channel_failed",
                ceremony_id=ceremony.id,
                recipient_mm_id=member_mm_id,
                window=window,
            )
            return False

        post = await mattermost_client.create_post(str(channel["id"]), message)
        if not post or not post.get("id"):
            logger.warning(
                "ceremony_reminder_post_failed",
                ceremony_id=ceremony.id,
                recipient_mm_id=member_mm_id,
                window=window,
            )
            return False

        sent_at = utcnow()
        inserted = await _record_sent(ceremony.id, member_mm_id, window, sent_at)

        logger.info(
            "ceremony_reminder_sent",
            ceremony_id=ceremony.id,
            recipient_mm_id=member_mm_id,
            window=window,
            timezone=resolved_tz,
            inserted_row=inserted,
        )
        return True

    except Exception as e:
        logger.exception(
            "ceremony_reminder_send_failed",
            ceremony_id=ceremony.id,
            recipient_mm_id=member_mm_id,
            window=window,
            error=str(e),
        )
        return False


# ---------------------------------------------------------------------------
# Ceremony type label helper
# ---------------------------------------------------------------------------


async def _ceremony_type_label(ceremony_type_id: int) -> str:
    """Resolve the human-readable label for a ceremony type.

    Args:
        ceremony_type_id: The ceremony_types.id foreign key.

    Returns:
        str: Display name (e.g. ``"Sprint Planning"``), or ``"Ceremony"`` as a safe fallback.
    """
    ct = await ceremony_repo.get_ceremony_type(ceremony_type_id)
    if ct is None:
        return "Ceremony"
    return ct.label


# ---------------------------------------------------------------------------
# Poller loop
# ---------------------------------------------------------------------------


async def reminder_poller() -> None:
    """Background loop that sends ceremony reminder DMs every 5 minutes.

    Follows the same pattern as ``app.workers.onboarding_dispatcher``:
    a plain ``asyncio.create_task`` started in ``app.main``'s lifespan.

    - Polls every ``_POLL_INTERVAL_SECONDS`` (300 s).
    - Any unhandled exception is logged and the loop continues — one bad
      pass never kills the poller.
    - A missing ``ceremony_reminders`` table (before migrations run) is
      logged as a warning, not an error, so startup is not blocked.
    """
    logger.info("ceremony_reminder_poller_started", poll_interval_seconds=_POLL_INTERVAL_SECONDS)

    while True:
        try:
            await _run_once()
        except asyncio.CancelledError:
            logger.info("ceremony_reminder_poller_cancelled")
            raise
        except Exception:
            logger.exception("ceremony_reminder_poll_failed")

        await asyncio.sleep(_POLL_INTERVAL_SECONDS)


async def _run_once() -> None:
    """One poll pass: check both windows and send any due reminders."""
    for window_hours, label in _WINDOWS:
        ceremonies = await due_ceremonies(window_hours)
        if not ceremonies:
            continue

        for ceremony in ceremonies:
            members = await get_channel_members(ceremony.channel_id)
            if not members:
                continue

            type_label = await _ceremony_type_label(ceremony.ceremony_type_id)

            for mm_id in members:
                await send_reminder(ceremony, mm_id, label, type_label)
