"""Meeting link generation for ceremony scheduling.

Supports two backends selected by ``MEETING_LINK_PROVIDER``:

* ``jitsi`` (default) — generates a deterministic ``meet.jit.si`` URL with
  no API calls and no credentials. Works immediately with a personal Gmail
  or any Google account.
* ``google_meet`` — uses the Google Calendar API with a service account that
  has domain-wide delegation. Requires a **Google Workspace** account.
  See ``google_meet.py`` for setup instructions.

The public function ``create_meeting_link`` is intentionally defensive:

- Returns ``None`` on any configuration or API error rather than raising.
- A link failure **never blocks ceremony scheduling**: the ceremony is stored
  with ``meet_link=None`` and the user is told the link could not be generated.
"""

import re
from datetime import datetime, timezone

from app.core.config import settings
from app.core.logging import logger


def _jitsi_link(title: str, start: datetime) -> str:
    """Generate a deterministic Jitsi Meet URL.

    The room name is built from the ceremony title and UTC start time so the
    same ceremony always gets the same link. Spaces and special characters are
    replaced with hyphens and the whole thing is lower-cased so the URL is
    easy to type and share.

    Args:
        title: Ceremony title, e.g. ``"Sprint Planning"``.
        start: The ceremony start time (timezone-aware).

    Returns:
        str: e.g. ``"https://meet.jit.si/SprintFlow-Sprint-Planning-20260910-1000"``.
    """
    utc = start.astimezone(timezone.utc)
    date_tag = utc.strftime("%Y%m%d-%H%M")
    # Normalise title: keep alphanumerics, replace anything else with a hyphen.
    safe_title = re.sub(r"[^a-zA-Z0-9]+", "-", title).strip("-")
    room = f"SprintFlow-{safe_title}-{date_tag}"
    link = f"https://meet.jit.si/{room}"
    logger.info("jitsi_meet_link_generated", title=title, scheduled_at=utc.isoformat(), link=link)
    return link


async def create_meeting_link(
    title, start, duration_minutes, description=None, organizer_email=None,
) -> tuple[str | None, str | None]:
    """... Returns (join_url, external_event_id). external_event_id is always
    None for jitsi — there is no real external event, just a URL formula.
    """
    if not settings.GOOGLE_MEET_ENABLED:
        return None, None
    provider = getattr(settings, "MEETING_LINK_PROVIDER", "jitsi").lower().strip()
    if provider == "jitsi":
        try:
            return _jitsi_link(title, start), None
        except Exception as e:
            logger.exception("jitsi_meet_link_failed", title=title, error=str(e))
            return None, None
    if provider == "google_meet":
        from app.services.google_meet import create_meet_event
        return await create_meet_event(title=title, start=start, duration_minutes=duration_minutes,
                                        description=description, organizer_email=organizer_email)
    logger.warning("meeting_link_unknown_provider", provider=provider)
    return None, None


async def update_meeting_link(external_event_id: str | None, *, start, duration_minutes, title=None, description=None) -> bool:
    """No-op (True) when there's nothing to sync: disabled, jitsi, or never created."""
    if not settings.GOOGLE_MEET_ENABLED or external_event_id is None:
        return True
    if getattr(settings, "MEETING_LINK_PROVIDER", "jitsi").lower().strip() != "google_meet":
        return True
    from app.services.google_meet import update_meet_event
    return await update_meet_event(external_event_id, start=start, duration_minutes=duration_minutes, title=title, description=description)


async def cancel_meeting_link(external_event_id: str | None) -> bool:
    if not settings.GOOGLE_MEET_ENABLED or external_event_id is None:
        return True
    if getattr(settings, "MEETING_LINK_PROVIDER", "jitsi").lower().strip() != "google_meet":
        return True
    from app.services.google_meet import cancel_meet_event
    return await cancel_meet_event(external_event_id)