"""Google Meet event creation via the Google Calendar API.

This module creates Google Calendar events with Google Meet conferencing
attached and returns the Meet join URL. It uses a **service account** with
domain-wide delegation to act on behalf of a real Google user, because
service accounts cannot create Meet links on their own.

The public function ``create_meet_event`` is intentionally defensive:

- Returns ``None`` on any configuration or API error rather than raising.
- A Meet failure **never blocks ceremony scheduling**: the ceremony is created
  with ``meet_link=None`` and the user is told the link could not be generated.

Setup instructions:
--------------------------------------------------------------------------
1. Go to https://console.cloud.google.com/ and create or select a project.
2. Enable the **Google Calendar API**:
   APIs & Services → Library → "Google Calendar API" → Enable.
3. Create a Service Account:
   IAM & Admin → Service Accounts → Create.
4. Enable **domain-wide delegation** on the service account:
   Service Accounts → your account → Edit → Enable G Suite Domain-wide Delegation.
   Note the numeric Client ID shown.
5. Grant the delegation in Google Workspace Admin Console
   (https://admin.google.com):
   Security → API Controls → Domain-wide Delegation → Add new → paste the
   Client ID, scope: https://www.googleapis.com/auth/calendar
   NOTE: If you do not have Google Workspace, use a shared Google Calendar
   instead — share it with the service account email and set
   GOOGLE_CALENDAR_ID to that calendar's ID (skip steps 4-5).
6. Create a JSON key for the service account:
   Service Accounts → your account → Keys → Add Key → JSON.
7. In your ``.env`` set:
   ```
   GOOGLE_MEET_ENABLED=true
   GOOGLE_IMPERSONATE_EMAIL=you@yourdomain.com   # a real Google user to impersonate
   GOOGLE_CALENDAR_ID=primary                    # or a shared calendar ID
   GOOGLE_SERVICE_ACCOUNT_CREDENTIALS=<paste the entire JSON key file here>
   ```
--------------------------------------------------------------------------
"""

import json
from datetime import (
    datetime,
    timezone,
)
from typing import Any

from app.core.config import settings
from app.core.logging import logger

# These are optional dependencies — only imported when Google Meet is enabled.
# The try/except prevents ImportError at startup when the packages are not
# installed (the feature is gated behind GOOGLE_MEET_ENABLED anyway).
try:
    from google.oauth2 import service_account  # type: ignore[import-untyped]
    from googleapiclient.discovery import build  # type: ignore[import-untyped]

    _GOOGLE_LIBS_AVAILABLE = True
except ImportError:
    _GOOGLE_LIBS_AVAILABLE = False
    service_account = None
    build = None

_SCOPES = ["https://www.googleapis.com/auth/calendar"]


def _build_service() -> Any | None:
    """Build an authenticated Google Calendar API service client.

    When ``settings.GOOGLE_IMPERSONATE_EMAIL`` is set the service account uses
    domain-wide delegation to act as that user. This is **required** to create
    Google Meet links — service accounts do not hold Meet licences on their own.

    Returns:
        googleapiclient Resource | None: The service, or None on any error.
    """
    if not _GOOGLE_LIBS_AVAILABLE:
        logger.warning(
            "google_meet_libs_missing",
            hint="Install google-api-python-client google-auth to enable Meet integration",
        )
        return None

    raw = settings.GOOGLE_SERVICE_ACCOUNT_CREDENTIALS.strip()
    if not raw:
        logger.warning("google_meet_no_credentials", hint="Set GOOGLE_SERVICE_ACCOUNT_CREDENTIALS in .env")
        return None

    try:
        info = json.loads(raw)
        creds = service_account.Credentials.from_service_account_info(info, scopes=_SCOPES)  # type: ignore[union-attr]

        # Impersonate a real user so the service account can create Meet links.
        # Without this, the API returns "Invalid conference type value" because
        # service accounts do not have Google Meet licences.
        impersonate = settings.GOOGLE_IMPERSONATE_EMAIL
        if impersonate:
            creds = creds.with_subject(impersonate)
            logger.debug("google_meet_impersonating", subject=impersonate)
        else:
            logger.warning(
                "google_meet_no_impersonate_email",
                hint="Set GOOGLE_IMPERSONATE_EMAIL in .env to a real Google account email. "
                "Meet link creation will fail without domain-wide delegation.",
            )

        return build("calendar", "v3", credentials=creds, cache_discovery=False)  # type: ignore[misc]
    except Exception as e:
        logger.exception("google_meet_build_service_failed", error=str(e))
        return None


def _format_rfc3339(dt: datetime) -> str:
    """Format a datetime as RFC 3339 (required by Google Calendar API).

    Args:
        dt: A timezone-aware datetime (any zone; converted to UTC).

    Returns:
        str: e.g. ``"2026-09-08T14:00:00Z"``.
    """
    utc = dt.astimezone(timezone.utc)
    return utc.strftime("%Y-%m-%dT%H:%M:%SZ")


async def create_meet_event(
    title: str,
    start: datetime,
    duration_minutes: int,
    description: str | None = None,
    organizer_email: str | None = None,
) -> str | None:
    """Create a Google Calendar event with Meet conferencing and return the join URL.

    The event is created on the shared calendar identified by
    ``settings.GOOGLE_CALENDAR_ID``.  Google Meet conferencing is attached via
    a ``conferenceData.createRequest``; the API populates the join link
    synchronously on successful insertion.

    The function is **async** to match the rest of the codebase but the
    ``googleapiclient`` library is synchronous. For the volume of calls
    expected (one per ceremony scheduling, not per request), this is
    acceptable; a future improvement could wrap the call in
    ``asyncio.to_thread``.

    Args:
        title: Event title, e.g. ``"Sprint Planning"``.
        start: The ceremony start time (timezone-aware, any zone).
        duration_minutes: Length of the event.
        description: Optional event description / agenda text.
        organizer_email: The organiser's email, added as an attendee when provided.

    Returns:
        str | None: The ``meet.google.com/xxx-xxxx-xxx`` join URL, or ``None``
        on any configuration or API error.
    """
    if not settings.GOOGLE_MEET_ENABLED:
        return None

    service = _build_service()
    if service is None:
        return None

    from datetime import timedelta

    end = start + timedelta(minutes=duration_minutes)

    event_body: dict[str, Any] = {
        "summary": title,
        "description": description or "",
        "start": {"dateTime": _format_rfc3339(start), "timeZone": "UTC"},
        "end": {"dateTime": _format_rfc3339(end), "timeZone": "UTC"},
        "conferenceData": {
            "createRequest": {
                # requestId must be unique per request; using the start ISO string
                # is deterministic and idempotent — re-scheduling the same
                # ceremony at the same time won't create a second Meet.
                "requestId": f"sprintflow-{start.strftime('%Y%m%dT%H%M%SZ')}",
                "conferenceSolutionKey": {"type": "hangoutsMeet"},
            }
        },
    }

    if organizer_email:
        event_body["attendees"] = [{"email": organizer_email}]

    try:
        # conferenceDataVersion=1 tells the API to fulfil the createRequest.
        created = (
            service.events()
            .insert(
                calendarId=settings.GOOGLE_CALENDAR_ID,
                body=event_body,
                conferenceDataVersion=1,
                sendUpdates="none",
            )
            .execute()
        )
        entry_points = created.get("conferenceData", {}).get("entryPoints", [])
        for ep in entry_points:
            if ep.get("entryPointType") == "video":
                link = ep.get("uri", "")
                if link:
                    logger.info(
                        "google_meet_created",
                        title=title,
                        scheduled_at=_format_rfc3339(start),
                        meet_link=link,
                    )
                    return link
        logger.warning("google_meet_no_video_entrypoint", created=created)
        return None
    except Exception as e:
        logger.exception("google_meet_create_failed", title=title, error=str(e))
        return None
