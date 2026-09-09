"""Back-office administration: channel roles and sprints.

Every function here follows the same shape, in this order:

1. **Resolve** the human references the tool received (``@handle`` or email, role word) to stored rows.
2. **Authorise** through ``app.services.authorisation`` — from stored data,
   scoped to the channel in question, *before* anything is validated or
   written. A requester who may not act on the channel learns nothing about
   whether the role or person they named exists.
3. **Validate** the request itself (unknown role, bad dates)
   and raise ``ValidationFailed`` — a different exception from a refusal.
4. **Mutate idempotently**: repeating an action reports the existing state
   (``*_ALREADY_*`` codes) instead of creating a second row. The database
   constraints (one membership per person per channel, unique sprint name per channel)
   back this up under concurrency: an integrity error is re-read as "already exists".
5. **Audit** with a structlog event carrying ids only — never message text.

The requester and channel are read from ``current_requester``; no function takes them as an
argument the model could populate. The tool wrappers in
``app.core.langgraph.tools.back_office`` are one line each over these.
"""

from dataclasses import dataclass
from datetime import (
    UTC,
    date,
    datetime,
    timedelta,
)

from sqlalchemy.exc import IntegrityError

from app.core.config import settings
from app.core.langgraph.tools.results import ResultCode
from app.core.logging import logger
from app.core.requester import (
    RequesterContext,
    current_requester,
)
from app.models import (
    ChannelRole,
    Role,
    Sprint,
    User,
)
from app.models.enums import (
    ROLE_LABELS,
    RoleKey,
    SprintStatus,
    normalise_role_key,
)
from app.services import onboarding
from app.services.authorisation import (
    ValidationFailed,
    require_channel_authority,
    require_channel_membership,
    require_requester,
    require_requester_user,
    require_superadmin,
)
from app.services.domain import channels as channel_repo
from app.services.domain import sprints as sprint_repo
from app.services.domain.channels import ChannelMember
from app.services.identity import resolve_person

SPRINT_NAME_MAX_LENGTH = 128

KNOWN_ROLES_SENTENCE = ", ".join(ROLE_LABELS[key].lower() for key in RoleKey)


# ---------------------------------------------------------------------------
# Results — small, typed, carrying a ResultCode and the entities involved
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RoleAssignmentResult:
    """Outcome of ``assign_role``."""

    code: ResultCode
    role_assignment: ChannelRole
    user: User
    channel_id: str
    role: Role
    previous_role: Role | None
    message: str


@dataclass(frozen=True)
class SprintResult:
    """Outcome of ``open_sprint``."""

    code: ResultCode
    sprint: Sprint
    channel_id: str
    message: str


@dataclass(frozen=True)
class ChannelListing:
    """Outcome of ``list_channel_roles_for_requester``: each channel with the requester's role there (None for superadmins)."""

    code: ResultCode
    entries: list[tuple[str, Role | None]]
    is_superadmin: bool
    message: str


@dataclass(frozen=True)
class ChannelMembersResult:
    """Outcome of ``list_channel_members``."""

    code: ResultCode
    channel_id: str
    members: list[ChannelMember]
    message: str


# ---------------------------------------------------------------------------
# Pure helpers (unit-tested without a database)
# ---------------------------------------------------------------------------


def role_from_text(value: str) -> RoleKey:
    """Map a typed role (key, label or alias) to its machine key.

    Args:
        value: ``"scrum master"``, ``"Tech Lead"``, ``"tech_lead"``, ``"ops"`` ...

    Returns:
        RoleKey: The key.

    Raises:
        ValidationFailed: When the text matches no known role.
    """
    key = normalise_role_key(value)
    if key is None:
        raise ValidationFailed(f"Unknown role '{value.strip()}'. Known roles: {KNOWN_ROLES_SENTENCE}.")
    return key


def parse_iso_date(value: str, field: str) -> date:
    """Parse a ``YYYY-MM-DD`` date.

    Args:
        value: The text as typed.
        field: Which field, for the error sentence (``start date``).

    Returns:
        date: The calendar date.

    Raises:
        ValidationFailed: When the text is not an ISO calendar date.
    """
    text = value.strip()
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise ValidationFailed(f"The {field} '{text}' is not a valid date; use YYYY-MM-DD.")


def resolve_sprint_dates(
    start_date: str | None,
    end_date: str | None,
    *,
    today: date | None = None,
    default_length_days: int | None = None,
) -> tuple[date, date]:
    """Turn the optional date strings of ``open_sprint`` into a validated calendar range.

    Args:
        start_date: ISO date or None for today (UTC).
        end_date: ISO date or None for ``start + default_length_days``.
        today: Override for tests; defaults to the current UTC date.
        default_length_days: Override for tests; defaults to ``settings.SPRINT_DEFAULT_LENGTH_DAYS``.

    Returns:
        tuple[date, date]: ``(start, end)`` with ``end >= start``.

    Raises:
        ValidationFailed: On an unparsable date or an end before the start.
    """
    length = settings.SPRINT_DEFAULT_LENGTH_DAYS if default_length_days is None else default_length_days
    start = parse_iso_date(start_date, "start date") if start_date else (today or datetime.now(UTC).date())
    end = parse_iso_date(end_date, "end date") if end_date else start + timedelta(days=length)
    if end < start:
        raise ValidationFailed(f"The end date {end.isoformat()} is before the start date {start.isoformat()}.")
    return start, end


def _clean_name(value: str, *, what: str, max_length: int) -> str:
    """Trim and bound a human name.

    Args:
        value: The text as typed.
        what: ``"sprint"``, for the error sentence.
        max_length: Column width.

    Returns:
        str: The trimmed name.

    Raises:
        ValidationFailed: When empty or too long.
    """
    name = " ".join(value.split())
    if not name:
        raise ValidationFailed(f"A {what} needs a name.")
    if len(name) > max_length:
        raise ValidationFailed(f"A {what} name can be at most {max_length} characters.")
    return name


def _requester() -> RequesterContext | None:
    """The bound requester, or None (the authorisation layer refuses on None)."""
    return current_requester.get()


def _bound_requester() -> RequesterContext:
    """The bound requester, refusing before any database read when nobody is bound.

    Returns:
        RequesterContext: The bound context.

    Raises:
        AuthorisationRefused: When no requester is bound.
    """
    return require_requester()


# ---------------------------------------------------------------------------
# Privileged actions
# ---------------------------------------------------------------------------


async def assign_role(person: str, role: str) -> RoleAssignmentResult:
    """Give a person a role in the current channel. One role per person per channel.

    Args:
        person: ``@handle`` or email.
        role: Role key, label or alias.

    Returns:
        RoleAssignmentResult: ``ROLE_ASSIGNED``, ``ROLE_ALREADY_ASSIGNED`` (nothing changed) or
        ``ROLE_CHANGED`` (previous role named).

    Raises:
        AuthorisationRefused: When the requester may not administer this channel.
        ValidationFailed: Unknown role or unknown person.
    """
    requester = _bound_requester()
    channel_id = requester.channel_id
    team_id = requester.team_id
    if not channel_id:
        raise ValidationFailed("Cannot assign a role outside of a channel context.")

    decision = await require_channel_authority(requester, channel_id, action="assign_role")
    assert decision.user is not None

    role_key = role_from_text(role)
    role_row = await channel_repo.get_role_by_key(role_key)
    if role_row is None or role_row.id is None:
        raise ValidationFailed(
            f"Role '{ROLE_LABELS[role_key]}' is not seeded in this environment; ask an administrator."
        )

    subject = await resolve_person(person)
    if subject is None or subject.id is None:
        raise ValidationFailed(f"I could not find anyone matching '{person.strip()}' in Mattermost.")

    change = await channel_repo.upsert_channel_role(
        user_id=subject.id,
        team_id=team_id,
        channel_id=channel_id,
        role_id=role_row.id,
        assigned_by_id=decision.user.id,
    )
    who = f"@{subject.username}"
    if not change.created and change.previous_role_id == role_row.id and not change.reactivated:
        logger.info(
            "back_office_role_unchanged",
            user_id=decision.user.id,
            subject_user_id=subject.id,
            channel_id=channel_id,
            role=role_row.key,
        )
        return RoleAssignmentResult(
            ResultCode.ROLE_ALREADY_ASSIGNED,
            change.role_assignment,
            subject,
            channel_id,
            role_row,
            role_row,
            f"{who} is already {role_row.label} in this channel; nothing was changed.",
        )

    previous: Role | None = None
    if change.previous_role_id is not None:
        previous = await channel_repo.get_role(change.previous_role_id)

    if change.created:
        logger.info(
            "back_office_role_assigned",
            user_id=decision.user.id,
            subject_user_id=subject.id,
            channel_id=channel_id,
            role=role_row.key,
        )
        return RoleAssignmentResult(
            ResultCode.ROLE_ASSIGNED,
            change.role_assignment,
            subject,
            channel_id,
            role_row,
            None,
            f"Assigned {who} as {role_row.label} in this channel.",
        )

    if change.reactivated and previous is not None and previous.id == role_row.id:
        logger.info(
            "back_office_membership_reactivated",
            user_id=decision.user.id,
            subject_user_id=subject.id,
            channel_id=channel_id,
            role=role_row.key,
        )
        return RoleAssignmentResult(
            ResultCode.ROLE_ASSIGNED,
            change.role_assignment,
            subject,
            channel_id,
            role_row,
            None,
            f"Re-added {who} as {role_row.label} in this channel (the membership had been inactive).",
        )

    previous_label = previous.label if previous else "an inactive membership"
    logger.info(
        "back_office_role_changed",
        user_id=decision.user.id,
        subject_user_id=subject.id,
        channel_id=channel_id,
        role=role_row.key,
        previous_role=previous.key if previous else None,
    )
    return RoleAssignmentResult(
        ResultCode.ROLE_CHANGED,
        change.role_assignment,
        subject,
        channel_id,
        role_row,
        previous,
        f"Changed {who} from {previous_label} to {role_row.label} in this channel.",
    )


async def open_sprint(
    sprint_name: str,
    start_date: str | None = None,
    end_date: str | None = None,
) -> SprintResult:
    """Open (activate) a sprint for the current channel.

    Args:
        sprint_name: Unique within the channel, e.g. ``Sprint 1``.
        start_date: ISO date; defaults to today (UTC).
        end_date: ISO date; defaults to ``start + SPRINT_DEFAULT_LENGTH_DAYS``.

    Returns:
        SprintResult: ``SPRINT_OPENED`` (new, or a planned sprint activated) or
        ``SPRINT_ALREADY_OPEN`` (nothing changed).

    Raises:
        AuthorisationRefused: When the requester may not administer this channel.
        ValidationFailed: Unknown or inactive channel, bad dates, a completed sprint of that
            name, or an overlap with another non-completed sprint.
    """
    requester = _bound_requester()
    channel_id = requester.channel_id
    team_id = requester.team_id
    if not channel_id:
        raise ValidationFailed("Cannot create a sprint outside of a channel context.")

    decision = await require_channel_authority(requester, channel_id, action="open_sprint")
    assert decision.user is not None

    name = _clean_name(sprint_name, what="sprint", max_length=SPRINT_NAME_MAX_LENGTH)
    start, end = resolve_sprint_dates(start_date, end_date)

    existing = await sprint_repo.get_sprint_by_name(channel_id, name)
    if existing is not None:
        return await _open_existing_sprint(existing, channel_id, decision.user)

    overlaps = await sprint_repo.find_overlapping_sprints(channel_id, start, end)
    if overlaps:
        clash = overlaps[0]
        raise ValidationFailed(
            f"Sprint '{name}' ({start.isoformat()} to {end.isoformat()}) overlaps sprint '{clash.name}' "
            f"({clash.start_date.isoformat()} to {clash.end_date.isoformat()}) in this channel."
        )

    try:
        sprint = await sprint_repo.create_sprint(
            team_id=team_id,
            channel_id=channel_id,
            name=name,
            start_date=start,
            end_date=end,
            status=SprintStatus.ACTIVE,
            opened_by_id=decision.user.id,
        )
    except IntegrityError:
        raced = await sprint_repo.get_sprint_by_name(channel_id, name)
        if raced is None:
            raise
        return await _open_existing_sprint(raced, channel_id, decision.user)

    logger.info("back_office_sprint_opened", user_id=decision.user.id, channel_id=channel_id, sprint_id=sprint.id)
    return SprintResult(
        ResultCode.SPRINT_OPENED,
        sprint,
        channel_id,
        f"Opened sprint '{sprint.name}' for this channel "
        f"({sprint.start_date.isoformat()} to {sprint.end_date.isoformat()}).",
    )


async def _open_existing_sprint(sprint: Sprint, channel_id: str, actor: User) -> SprintResult:
    """Report or activate a sprint that already carries the requested name.

    Args:
        sprint: The stored sprint.
        channel_id: Its channel.
        actor: The authorised requester.

    Returns:
        SprintResult: ``SPRINT_ALREADY_OPEN`` when active, ``SPRINT_OPENED`` when a planned one was activated.

    Raises:
        ValidationFailed: When the sprint is completed or activating it would overlap another sprint.
    """
    assert sprint.id is not None
    window = f"{sprint.start_date.isoformat()} to {sprint.end_date.isoformat()}"
    if sprint.status == SprintStatus.ACTIVE.value:
        logger.info("back_office_sprint_already_open", user_id=actor.id, channel_id=channel_id, sprint_id=sprint.id)
        return SprintResult(
            ResultCode.SPRINT_ALREADY_OPEN,
            sprint,
            channel_id,
            f"Sprint '{sprint.name}' is already open for this channel ({window}); nothing was changed.",
        )
    if sprint.status == SprintStatus.COMPLETED.value:
        raise ValidationFailed(
            f"Sprint '{sprint.name}' in this channel is already completed ({window}); "
            "choose a new sprint name."
        )

    overlaps = await sprint_repo.find_overlapping_sprints(
        channel_id, sprint.start_date, sprint.end_date, exclude_id=sprint.id
    )
    if overlaps:
        clash = overlaps[0]
        raise ValidationFailed(
            f"Sprint '{sprint.name}' ({window}) overlaps sprint '{clash.name}' "
            f"({clash.start_date.isoformat()} to {clash.end_date.isoformat()}) in this channel."
        )

    activated = await sprint_repo.set_sprint_status(sprint.id, SprintStatus.ACTIVE)
    if activated is None:
        raise ValidationFailed(f"Sprint '{sprint.name}' disappeared while I was opening it; please try again.")
    logger.info(
        "back_office_sprint_opened", user_id=actor.id, channel_id=channel_id, sprint_id=activated.id, activated=True
    )
    return SprintResult(
        ResultCode.SPRINT_OPENED,
        activated,
        channel_id,
        f"Opened the planned sprint '{activated.name}' for this channel ({window}).",
    )


# ---------------------------------------------------------------------------
# Scoped reads
# ---------------------------------------------------------------------------


async def list_channel_roles_for_requester() -> ChannelListing:
    """List the channels the requester can act in.

    Returns:
        ChannelListing: The channels, with the requester's role in each.

    Raises:
        AuthorisationRefused: When the requester was never synced into ``users``.
    """
    user = await require_requester_user(_requester(), action="list_channel_roles")
    assert user.id is not None

    if user.is_superadmin:
        # Superadmins don't have a single "list" anymore, they can access anything.
        # So we just return an empty list or a note saying they are a superadmin.
        entries: list[tuple[str, Role | None]] = []
    else:
        memberships = await channel_repo.list_roles_for_user(user.id, active_only=True)
        entries = [(membership.channel_id, role) for membership, role in memberships]

    logger.info("back_office_channels_listed", user_id=user.id, count=len(entries), superadmin=user.is_superadmin)
    return ChannelListing(ResultCode.OK, entries, user.is_superadmin, _describe_channels(entries, user.is_superadmin))


def _describe_channels(entries: list[tuple[str, Role | None]], is_superadmin: bool) -> str:
    """Render a channel role listing as one readable sentence block.

    Args:
        entries: Channels with the requester's role in each.
        is_superadmin: Whether the listing is platform-wide.

    Returns:
        str: The text the agent relays.
    """
    if is_superadmin:
        return "You are a superadmin, so you have authority in all channels."
    if not entries:
        return "You have no roles assigned in any channel."
    lines = []
    for channel_id, role in entries:
        held = f" — your role: {role.label}" if role else ""
        lines.append(f"- Channel ID {channel_id}{held}")
    return "Your active roles:\n" + "\n".join(lines)


async def list_channel_members() -> ChannelMembersResult:
    """List the current channel's active members and their roles.

    Returns:
        ChannelMembersResult: The members.

    Raises:
        AuthorisationRefused: When the requester is neither a member nor a superadmin.
    """
    requester = _bound_requester()
    channel_id = requester.channel_id
    if not channel_id:
        raise ValidationFailed("Cannot list members outside of a channel context.")

    decision = await require_channel_membership(requester, channel_id, action="list_channel_members")
    assert decision.user is not None

    members = await channel_repo.list_channel_roles(channel_id, active_only=True)
    logger.info("back_office_members_listed", user_id=decision.user.id, channel_id=channel_id, count=len(members))
    if not members:
        text = f"This channel has no SprintFlow roles assigned yet."
    else:
        rows = [f"- @{m.user.username} — {m.role.label}" for m in members]
        text = f"Assigned roles in this channel ({len(members)}):\n" + "\n".join(rows)
    return ChannelMembersResult(ResultCode.OK, channel_id, members, text)
