"""Ceremony scheduling tools for LangGraph.

These tools let the AI agent schedule, amend, and read Agile ceremonies
(standups, planning, retro, review, demo) for cohorts.

Aligned with the team's data-model-migrations schema:
  - Uses CeremonyType lookup table via `type_id` FK
  - Uses `status` field ("scheduled"/"cancelled") instead of `is_active` bool
  - Uses the shared domain service helpers where possible
  - Uses `Session(database_service.engine)` pattern (team standard)
"""

import dateparser
from datetime import datetime, timedelta, timezone
from typing import Optional

from langchain_core.tools import tool
from sqlmodel import Session, select

from app.core.logging import logger
from app.models.ceremony import Ceremony
from app.models.ceremony_type import CeremonyType
from app.services.database import database_service
from app.services.admin_service import AdminService, AuthorisationRefusalError

# Reuse the ask_human tool built by the Admin team (Norhan/Youhanna)
# The agent uses this to pause and ask the user for confirmation
from app.core.langgraph.tools.ask_human import ask_human  # noqa: F401

# One shared instance — same pattern used by the admin tools
admin_service = AdminService()


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _get_or_create_ceremony_type(session: Session, name: str) -> CeremonyType:
    """Get an existing CeremonyType or create one on the fly.

    This mirrors `domain.get_or_create_ceremony_type` but works within
    an already-open session so we can keep everything in one transaction.
    """
    normalized = name.strip().lower()
    ct = session.exec(
        select(CeremonyType).where(CeremonyType.name == normalized)
    ).first()
    if ct is not None:
        return ct

    ct = CeremonyType(name=normalized)
    session.add(ct)
    session.flush()  # get the ID without committing
    return ct


def validate_and_parse_time(raw_time: str) -> tuple[datetime | None, str]:
    """
    Takes the time the user typed and turns it into a UTC datetime.
    Returns (datetime, "") on success, or (None, "error message") on failure.
    """

    # Step 1: Try to understand what time the user wrote
    parsed = dateparser.parse(raw_time, settings={"RETURN_AS_TIMEZONE_AWARE": True})
    if not parsed:
        return None, "Error: I couldn't understand that time. Ask the user to be more specific (e.g. 'Monday 9 AM UTC')."

    # Step 2: Reject if the time is ambiguous
    # We need to know the timezone AND whether it's morning or afternoon
    raw_lower = raw_time.lower()
    has_ampm = any(word in raw_lower for word in ["am", "pm", "morning", "night", "afternoon", "evening"])
    has_24h = any(str(h) in raw_lower for h in range(13, 24))
    has_timezone = parsed.tzinfo is not None

    if not has_timezone or not (has_ampm or has_24h):
        return None, "Error: The time is ambiguous. Ask the user to add AM/PM and a timezone (e.g. '3 PM UTC')."

    # Step 3: Convert to UTC so everything in the DB is consistent
    utc_dt = parsed.astimezone(timezone.utc)

    # Step 4: Don't allow booking in the past
    if utc_dt <= datetime.now(timezone.utc):
        return None, "Error: That time is in the past. Ask the user to pick a future date."

    return utc_dt, ""


def find_conflict(
    session: Session,
    cohort_id: int,
    scheduled_at: datetime,
    exclude_id: int | None = None,
) -> Ceremony | None:
    """
    Looks in the DB to see if there's already an active ceremony for this
    cohort within 30 minutes of the requested time.
    Returns the conflicting ceremony if found, or None if the slot is free.
    """
    window_start = scheduled_at - timedelta(minutes=30)
    window_end = scheduled_at + timedelta(minutes=30)

    query = select(Ceremony).where(
        Ceremony.cohort_id == cohort_id,
        Ceremony.status != "cancelled",
        Ceremony.scheduled_at >= window_start,
        Ceremony.scheduled_at <= window_end,
    )

    # When amending, we ignore the ceremony being changed
    if exclude_id is not None:
        query = query.where(Ceremony.id != exclude_id)

    return session.exec(query).first()


def _ceremony_type_name(session: Session, type_id: int) -> str:
    """Resolve a CeremonyType ID back to its human-readable name."""
    ct = session.get(CeremonyType, type_id)
    return ct.name.upper() if ct else f"type#{type_id}"


# Tool 1: Schedule a new ceremony

@tool
def schedule_ceremony(
    cohort_id: int,
    ceremony_type: str,
    raw_time: str,
    organizer_id: str,
    agenda: Optional[str] = None,
    channel_id: Optional[str] = None,
) -> str:
    """
    Books a new ceremony for a cohort and saves it to the database.
    You MUST confirm all the details with the user via ask_human before calling this.

    Args:
        cohort_id:      The ID of the cohort (from the cohort table).
        ceremony_type:  Type of meeting: standup, planning, retro, review, demo...
        raw_time:       The time the user typed (must have timezone + AM/PM).
        organizer_id:   The user ID of the person making the booking.
        agenda:         Optional meeting agenda or notes.
        channel_id:     Optional Mattermost channel to post the reminder in.
    """

    # Check that the person has permission to schedule ceremonies
    try:
        admin_service.evaluate_permission(
            requester_id=organizer_id,
            required_role="admin",
            cohort_id=str(cohort_id),
        )
    except AuthorisationRefusalError as e:
        return f"Error: You don't have permission to schedule ceremonies. ({e})"

    # Validate the time the user typed
    utc_dt, error_msg = validate_and_parse_time(raw_time)
    if error_msg:
        return error_msg

    with Session(database_service.engine) as session:

        # Resolve ceremony type name → CeremonyType row (create if new)
        ctype = _get_or_create_ceremony_type(session, ceremony_type)

        # Make sure there's no other meeting at the same time for this cohort
        conflict = find_conflict(session, cohort_id, utc_dt)
        if conflict:
            conflict_name = _ceremony_type_name(session, conflict.type_id)
            return (
                f"Error: Cohort already has a '{conflict_name}' at "
                f"{conflict.scheduled_at.isoformat()} UTC (within 30 minutes). "
                f"Ask the user to pick a different time."
            )

        # Everything looks good — save to the database
        new_ceremony = Ceremony(
            cohort_id=cohort_id,
            type_id=ctype.id,
            scheduled_at=utc_dt,
            organizer=organizer_id,
            agenda=agenda,
            raw_input=raw_time,
            channel_id=channel_id,
        )
        session.add(new_ceremony)
        session.commit()
        session.refresh(new_ceremony)

    logger.info(f"Ceremony #{new_ceremony.id} created for cohort {cohort_id} by {organizer_id}")
    return f"SUCCESS: {ctype.name} scheduled at {utc_dt.isoformat()} UTC. Ceremony ID is #{new_ceremony.id}."


# Tool 2: Update or cancel a ceremony

@tool
def amend_ceremony(
    ceremony_id: int,
    organizer_id: str,
    new_raw_time: Optional[str] = None,
    new_agenda: Optional[str] = None,
    cancel: bool = False,
) -> str:
    """
    Changes the time or agenda of an existing ceremony, or cancels it.
    Confirm the change with the user via ask_human before calling this.

    Args:
        ceremony_id:    The ID of the ceremony to update.
        organizer_id:   Must match the person who originally booked it.
        new_raw_time:   New time string if rescheduling (optional).
        new_agenda:     New agenda text (optional).
        cancel:         Set to True to cancel the ceremony.
    """

    with Session(database_service.engine) as session:

        # Find the ceremony in the database
        ceremony = session.get(Ceremony, ceremony_id)

        if not ceremony:
            return f"Error: No ceremony found with ID #{ceremony_id}."

        if ceremony.status == "cancelled":
            return f"Error: Ceremony #{ceremony_id} is already cancelled."

        # Only the original organizer can make changes
        if ceremony.organizer != organizer_id:
            return f"Error: Only the original organizer ({ceremony.organizer}) can change this ceremony."

        # Handle cancellation
        if cancel:
            ceremony.status = "cancelled"
            session.commit()
            logger.info(f"Ceremony #{ceremony_id} cancelled by {organizer_id}")
            return f"SUCCESS: Ceremony #{ceremony_id} has been cancelled."

        # Handle rescheduling
        if new_raw_time:
            utc_dt, error_msg = validate_and_parse_time(new_raw_time)
            if error_msg:
                return error_msg

            # Make sure the new time doesn't clash with another ceremony
            conflict = find_conflict(session, ceremony.cohort_id, utc_dt, exclude_id=ceremony_id)
            if conflict:
                conflict_name = _ceremony_type_name(session, conflict.type_id)
                return (
                    f"Error: There's already a '{conflict_name}' at "
                    f"{conflict.scheduled_at.isoformat()} UTC (within 30 minutes)."
                )

            ceremony.scheduled_at = utc_dt
            ceremony.raw_input = new_raw_time

        # Handle agenda update
        if new_agenda:
            ceremony.agenda = new_agenda

        session.commit()

    logger.info(f"Ceremony #{ceremony_id} updated by {organizer_id}")
    return f"SUCCESS: Ceremony #{ceremony_id} has been updated."


# Tool 3: Read upcoming ceremonies for a cohort

@tool
def read_ceremonies(cohort_id: int, include_inactive: bool = False) -> str:
    """
    Returns a list of upcoming ceremonies for a cohort.

    Args:
        cohort_id:        The ID of the cohort.
        include_inactive: Set to True to also show cancelled ceremonies.
    """
    now = datetime.now(timezone.utc)

    with Session(database_service.engine) as session:

        query = select(Ceremony).where(Ceremony.cohort_id == cohort_id)

        # By default, only show future active (non-cancelled) ceremonies
        if not include_inactive:
            query = query.where(
                Ceremony.status != "cancelled",
                Ceremony.scheduled_at >= now,
            )

        # Show the soonest first
        query = query.order_by(Ceremony.scheduled_at)
        ceremonies = session.exec(query).all()

        if not ceremonies:
            return f"No upcoming ceremonies found for cohort #{cohort_id}."

        # Build a simple readable list
        lines = [f"Upcoming ceremonies for cohort #{cohort_id}:"]
        for c in ceremonies:
            type_name = _ceremony_type_name(session, c.type_id)
            line = f"  #{c.id} | {type_name} | {c.scheduled_at.isoformat()} UTC | {c.status} | by {c.organizer}"
            if c.agenda:
                line += f"\n         Agenda: {c.agenda}"
            lines.append(line)

    return "\n".join(lines)