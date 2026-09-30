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

GOOGLE_MEET_ENABLED=true
GOOGLE_IMPERSONATE_EMAIL=you@yourdomain.com # a real Google user to impersonate
GOOGLE_CALENDAR_ID=primary # or a shared calendar ID
GOOGLE_SERVICE_ACCOUNT_CREDENTIALS=<paste the entire JSON key file here>

--------------------------------------------------------------------------
"""

import asyncio
import json
from datetime import (
    datetime,
    timezone,
)
from typing import Any

from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from app.core.config import settings
from app.core.logging import logger
from googleapiclient.errors import HttpError  # type: ignore[import-untyped]
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

# These are optional dependencies — only imported when Google Meet is enabled.
# The try/except prevents ImportError at startup when the packages are not
# installed (the feature is gated behind GOOGLE_MEET_ENABLED anyway).
from google.oauth2 import service_account  # type: ignore[import-untyped]
from googleapiclient.discovery import build  # type: ignore[import-untyped]
from googleapiclient.errors import HttpError  # type: ignore[import-untyped]

_SCOPES = ["https://www.googleapis.com/auth/calendar"]


def _build_service() -> Any | None:
    """Build an authenticated Google Calendar API service client.

    When ``settings.GOOGLE_IMPERSONATE_EMAIL`` is set the service account uses
    domain-wide delegation to act as that user. This is **required** to create
    Google Meet links — service accounts do not hold Meet licences on their own.

    Returns:
        googleapiclient Resource | None: The service, or None on any error.
    """
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


# Statuses worth retrying: rate limit + transient server errors. A 4xx other
# than 429 means the request itself is wrong (bad event id, bad auth, bad
# body) and will never succeed on retry — retrying it just wastes time and
# risks compounding the outage on Google's side.
_RETRYABLE_STATUSES = {429, 500, 502, 503, 504}


def _is_retryable(exc: BaseException) -> bool:
    """True for rate limits/5xx/network errors; False for everything else.

    ``HttpError`` doesn't distinguish retryable from fatal by *type* alone —
    unlike ``mattermost.py``'s predicate, which retries any ``HTTPStatusError``
    including 4xx. We check the actual status code so a bad auth/config error
    fails fast instead of burning 3 attempts on something that can't succeed.
    """
    if isinstance(exc, HttpError):
        status = getattr(exc.resp, "status", None)
        return status in _RETRYABLE_STATUSES
    # Anything network/timeout related from googleapiclient's underlying
    # httplib2 surfaces as OSError/TimeoutError.
    return isinstance(exc, (TimeoutError, ConnectionError, OSError))


_retry_calendar_call = retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=8),
    retry=retry_if_exception(_is_retryable),
    reraise=True,
)


async def create_meet_event(
    title: str,
    start: datetime,
    duration_minutes: int,
    description: str | None = None,
    organizer_email: str | None = None,
) -> tuple[str | None, str | None]:
    """Create a Calendar event with Meet conferencing.

    Returns:
        tuple[str | None, str | None]: ``(join_url, event_id)``, both None on
        any configuration or (post-retry) API error.
    """
    if not settings.GOOGLE_MEET_ENABLED:
        return None, None

    service = _build_service()
    if service is None:
        return None, None

    from datetime import timedelta

    end = start + timedelta(minutes=duration_minutes)
    event_body: dict[str, Any] = {
        "summary": title,
        "description": description or "",
        "start": {"dateTime": _format_rfc3339(start), "timeZone": "UTC"},
        "end": {"dateTime": _format_rfc3339(end), "timeZone": "UTC"},
        "conferenceData": {
            "createRequest": {
                # Deterministic requestId: retrying the SAME create (whether
                # our tenacity retry, or a caller re-attempting after a lost
                # response) reuses the same conference request instead of
                # minting a second Meet link for one ceremony.
                "requestId": f"sprintflow-{start.strftime('%Y%m%dT%H%M%SZ')}",
                "conferenceSolutionKey": {"type": "hangoutsMeet"},
            }
        },
    }
    if organizer_email:
        event_body["attendees"] = [{"email": organizer_email}]

    try:
        created = await _create_with_retry(service, event_body)
    except Exception as e:
        logger.exception("google_meet_create_failed", title=title, error=str(e))
        return None, None

    event_id = created.get("id")
    for ep in created.get("conferenceData", {}).get("entryPoints", []):
        if ep.get("entryPointType") == "video" and ep.get("uri"):
            logger.info("google_meet_created", title=title, event_id=event_id, meet_link=ep["uri"])
            return ep["uri"], event_id
    logger.warning("google_meet_no_video_entrypoint", created=created)
    return None, event_id


@_retry_calendar_call
async def _create_with_retry(service: Any, event_body: dict[str, Any]) -> dict[str, Any]:
    def _call() -> dict[str, Any]:
        return (
            service.events()
            .insert(calendarId=settings.GOOGLE_CALENDAR_ID, body=event_body, conferenceDataVersion=1, sendUpdates="none")
            .execute()
        )
    return await asyncio.wait_for(asyncio.to_thread(_call), timeout=settings.GOOGLE_CALENDAR_HTTP_TIMEOUT)


async def update_meet_event(
    event_id: str,
    *,
    start: datetime,
    duration_minutes: int,
    title: str | None = None,
    description: str | None = None,
) -> bool:
    """Move an existing Calendar event to a new time (reschedule sync).

    A PATCH is naturally idempotent — retrying the same patch, or applying it
    twice because a response was lost, produces the same end state.

    Returns:
        bool: True on success (including "nothing to do" when disabled).
    """
    if not settings.GOOGLE_MEET_ENABLED:
        return True
    service = _build_service()
    if service is None:
        return False

    from datetime import timedelta

    end = start + timedelta(minutes=duration_minutes)
    body: dict[str, Any] = {
        "start": {"dateTime": _format_rfc3339(start), "timeZone": "UTC"},
        "end": {"dateTime": _format_rfc3339(end), "timeZone": "UTC"},
    }
    if title is not None:
        body["summary"] = title
    if description is not None:
        body["description"] = description

    try:
        await _patch_with_retry(service, event_id, body)
        logger.info("google_meet_updated", event_id=event_id, scheduled_at=_format_rfc3339(start))
        return True
    except HttpError as e:
        status = getattr(e.resp, "status", None)
        if status in (404, 410):
            # Event already gone (e.g. manually deleted on Google's side).
            # Nothing to reschedule; not a failure worth retrying forever.
            logger.warning("google_meet_update_target_missing", event_id=event_id, status=status)
            return False
        logger.exception("google_meet_update_failed", event_id=event_id, error=str(e))
        return False
    except Exception as e:
        logger.exception("google_meet_update_failed", event_id=event_id, error=str(e))
        return False


@_retry_calendar_call
async def _patch_with_retry(service: Any, event_id: str, body: dict[str, Any]) -> dict[str, Any]:
    def _call() -> dict[str, Any]:
        return (
            service.events()
            .patch(calendarId=settings.GOOGLE_CALENDAR_ID, eventId=event_id, body=body, sendUpdates="none")
            .execute()
        )
    return await asyncio.wait_for(asyncio.to_thread(_call), timeout=settings.GOOGLE_CALENDAR_HTTP_TIMEOUT)


async def cancel_meet_event(event_id: str) -> bool:
    """Delete an existing Calendar event (cancellation sync).

    A 404/410 (already gone) is treated as success: deleting an
    already-deleted event is exactly the state we want, so retries or a
    duplicate cancel call are safely idempotent.

    Returns:
        bool: True on success or "already gone"; False on a real failure.
    """
    if not settings.GOOGLE_MEET_ENABLED:
        return True
    service = _build_service()
    if service is None:
        return False

    try:
        await _delete_with_retry(service, event_id)
        logger.info("google_meet_cancelled", event_id=event_id)
        return True
    except HttpError as e:
        status = getattr(e.resp, "status", None)
        if status in (404, 410):
            logger.info("google_meet_already_cancelled", event_id=event_id)
            return True
        logger.exception("google_meet_cancel_failed", event_id=event_id, error=str(e))
        return False
    except Exception as e:
        logger.exception("google_meet_cancel_failed", event_id=event_id, error=str(e))
        return False


@_retry_calendar_call
async def _delete_with_retry(service: Any, event_id: str) -> None:
    def _call() -> None:
        service.events().delete(calendarId=settings.GOOGLE_CALENDAR_ID, eventId=event_id, sendUpdates="none").execute()
    await asyncio.wait_for(asyncio.to_thread(_call), timeout=settings.GOOGLE_CALENDAR_HTTP_TIMEOUT)