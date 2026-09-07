"""Back-office administration: cohorts, cohort roles and sprints.

Every function here follows the same shape, in this order:

1. **Resolve** the human references the tool received (cohort name or id,
   ``@handle`` or email, role word) to stored rows.
2. **Authorise** through ``app.services.authorisation`` — from stored data,
   scoped to the cohort in question, *before* anything is validated or
   written. A requester who may not act on the cohort learns nothing about
   whether the role or person they named exists.
3. **Validate** the request itself (unknown role, bad dates, inactive cohort)
   and raise ``ValidationFailed`` — a different exception from a refusal.
4. **Mutate idempotently**: repeating an action reports the existing state
   (``*_ALREADY_*`` codes) instead of creating a second row. The database
   constraints (case-insensitive unique cohort name, one membership per
   person per cohort, unique sprint name per cohort) back this up under
   concurrency: an integrity error is re-read as "already exists".
5. **Audit** with a structlog event carrying ids only — never message text.

The requester is read from ``current_requester``; no function takes one as an
argument the model could populate. The tool wrappers in
``app.core.langgraph.tools.back_office`` are one line each over these.
"""

import re
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
    Cohort,
    CohortMembership,
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
    require_active_cohort,
    require_cohort_authority,
    require_cohort_membership,
    require_requester,
    require_requester_user,
    require_superadmin,
)
from app.services.domain import cohorts as cohort_repo
from app.services.domain import sprints as sprint_repo
from app.services.domain.cohorts import CohortMember
from app.services.identity import resolve_person
from app.services.mattermost import mattermost_client

# Mattermost ids are 26 lower-case base-32 characters.
_MATTERMOST_ID_RE = re.compile(r"^[a-z0-9]{26}$")
_MATTERMOST_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,63}$")

COHORT_NAME_MAX_LENGTH = 128
SPRINT_NAME_MAX_LENGTH = 128

KNOWN_ROLES_SENTENCE = ", ".join(ROLE_LABELS[key].lower() for key in RoleKey)


# ---------------------------------------------------------------------------
# Results — small, typed, carrying a ResultCode and the entities involved
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CohortResult:
    """Outcome of ``create_cohort``."""

    code: ResultCode
    cohort: Cohort
    message: str


@dataclass(frozen=True)
class RoleAssignmentResult:
    """Outcome of ``assign_role``."""

    code: ResultCode
    membership: CohortMembership
    user: User
    cohort: Cohort
    role: Role
    previous_role: Role | None
    message: str


@dataclass(frozen=True)
class SprintResult:
    """Outcome of ``open_sprint``."""

    code: ResultCode
    sprint: Sprint
    cohort: Cohort
    message: str


@dataclass(frozen=True)
class CohortListing:
    """Outcome of ``list_cohorts``: each cohort with the requester's role there (None for superadmins)."""

    code: ResultCode
    entries: list[tuple[Cohort, Role | None]]
    is_superadmin: bool
    message: str


@dataclass(frozen=True)
class CohortMembersResult:
    """Outcome of ``list_cohort_members``."""

    code: ResultCode
    cohort: Cohort
    members: list[CohortMember]
    message: str


# ---------------------------------------------------------------------------
# Pure helpers (unit-tested without a database)
# ---------------------------------------------------------------------------


def looks_like_mattermost_id(value: str) -> bool:
    """Whether a string has the shape of a Mattermost id.

    Args:
        value: The text as typed.

    Returns:
        bool: True for 26 lower-case alphanumerics.
    """
    return bool(_MATTERMOST_ID_RE.match(value.strip()))


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
        what: ``"cohort"`` or ``"sprint"``, for the error sentence.
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

    Cohort-scoped actions must resolve the cohort before they can decide
    authority for it; this guard makes sure even that read never happens for
    a call made outside a conversation turn.

    Returns:
        RequesterContext: The bound context.

    Raises:
        AuthorisationRefused: When no requester is bound.
    """
    return require_requester()


async def _resolve_cohort_or_fail(reference: str) -> Cohort:
    """Resolve a cohort reference or raise a validation failure.

    Args:
        reference: Name or numeric id as typed.

    Returns:
        Cohort: The stored row.

    Raises:
        ValidationFailed: When nothing matches.
    """
    text = reference.strip()
    if not text:
        raise ValidationFailed("Which cohort? Give its name or id.")
    cohort = await cohort_repo.resolve_cohort(text)
    if cohort is None:
        raise ValidationFailed(f"Cohort '{text}' does not exist.")
    return cohort


async def _resolve_mattermost_team(reference: str | None) -> tuple[str | None, str]:
    """Turn what the administrator typed into a Mattermost team id, when possible.

    An id-shaped string is stored as given. A slug is looked up over the
    Mattermost REST API; when Mattermost cannot confirm it, nothing is stored
    and the note says so, because a wrong team id would misroute every later
    announcement.

    Args:
        reference: A Mattermost team id, a URL slug, or None.

    Returns:
        tuple[str | None, str]: The id to store (or None) and a note for the reply.
    """
    if reference is None or not reference.strip():
        return None, ""
    text = reference.strip()
    if looks_like_mattermost_id(text):
        return text, f" Linked to Mattermost team id {text}."
    slug = text.lower()
    if _MATTERMOST_SLUG_RE.match(slug):
        try:
            team = await mattermost_client.get_team_by_name(slug)
        except Exception as e:
            logger.warning("back_office_team_lookup_failed", slug=slug, error=str(e))
            team = None
        if team and team.get("id"):
            return str(team["id"]), f" Linked to Mattermost team '{slug}'."
    return None, f" I could not confirm a Mattermost team '{text}', so the cohort is not linked to one yet."


# ---------------------------------------------------------------------------
# Privileged actions
# ---------------------------------------------------------------------------


async def create_cohort(name: str, mattermost_team: str | None = None) -> CohortResult:
    """Create a cohort (platform-level; superadmin only). Idempotent by case-insensitive name.

    Args:
        name: Human name, e.g. ``Backend-01``.
        mattermost_team: Optional Mattermost team id or slug to link.

    Returns:
        CohortResult: ``COHORT_CREATED`` or ``COHORT_ALREADY_EXISTS`` (nothing changed).

    Raises:
        AuthorisationRefused: When the requester is not a stored superadmin.
        ValidationFailed: When the name is empty or too long.
    """
    admin = await require_superadmin(_requester(), action="create_cohort")
    cohort_name = _clean_name(name, what="cohort", max_length=COHORT_NAME_MAX_LENGTH)

    existing = await cohort_repo.get_cohort_by_name(cohort_name)
    if existing is not None:
        logger.info("back_office_cohort_exists", user_id=admin.id, cohort_id=existing.id)
        return CohortResult(
            ResultCode.COHORT_ALREADY_EXISTS,
            existing,
            f"Cohort '{existing.name}' already exists (id {existing.id}); nothing was changed.",
        )

    team_id, note = await _resolve_mattermost_team(mattermost_team)
    try:
        cohort = await cohort_repo.create_cohort(cohort_name, mattermost_team_id=team_id, created_by_id=admin.id)
    except IntegrityError:
        # Lost a race with an identical request: the unique index held, so report the winner.
        raced = await cohort_repo.get_cohort_by_name(cohort_name)
        if raced is None:
            raise
        logger.info("back_office_cohort_exists", user_id=admin.id, cohort_id=raced.id, raced=True)
        return CohortResult(
            ResultCode.COHORT_ALREADY_EXISTS,
            raced,
            f"Cohort '{raced.name}' already exists (id {raced.id}); nothing was changed.",
        )

    logger.info("back_office_cohort_created", user_id=admin.id, cohort_id=cohort.id, has_team=team_id is not None)
    return CohortResult(
        ResultCode.COHORT_CREATED,
        cohort,
        f"Created cohort '{cohort.name}' (id {cohort.id}).{note}",
    )


async def assign_role(person: str, role: str, cohort: str) -> RoleAssignmentResult:
    """Give a person a role in a cohort (cohort authority required). One role per person per cohort.

    Args:
        person: ``@handle`` or email.
        role: Role key, label or alias.
        cohort: Cohort name or numeric id.

    Returns:
        RoleAssignmentResult: ``ROLE_ASSIGNED``, ``ROLE_ALREADY_ASSIGNED`` (nothing changed) or
        ``ROLE_CHANGED`` (previous role named).

    Raises:
        AuthorisationRefused: When the requester may not administer this cohort.
        ValidationFailed: Unknown cohort, inactive cohort, unknown role or unknown person.
    """
    requester = _bound_requester()
    target_cohort = await _resolve_cohort_or_fail(cohort)
    assert target_cohort.id is not None
    decision = await require_cohort_authority(requester, target_cohort.id, action="assign_role")
    assert decision.user is not None
    require_active_cohort(target_cohort)

    role_key = role_from_text(role)
    role_row = await cohort_repo.get_role_by_key(role_key)
    if role_row is None or role_row.id is None:
        raise ValidationFailed(
            f"Role '{ROLE_LABELS[role_key]}' is not seeded in this environment; ask an administrator."
        )

    subject = await resolve_person(person)
    if subject is None or subject.id is None:
        raise ValidationFailed(f"I could not find anyone matching '{person.strip()}' in Mattermost.")

    change = await cohort_repo.upsert_membership(
        user_id=subject.id,
        cohort_id=target_cohort.id,
        role_id=role_row.id,
        assigned_by_id=decision.user.id,
    )
    who = f"@{subject.username}"
    if not change.created and change.previous_role_id == role_row.id and not change.reactivated:
        logger.info(
            "back_office_role_unchanged",
            user_id=decision.user.id,
            subject_user_id=subject.id,
            cohort_id=target_cohort.id,
            role=role_row.key,
        )
        return RoleAssignmentResult(
            ResultCode.ROLE_ALREADY_ASSIGNED,
            change.membership,
            subject,
            target_cohort,
            role_row,
            role_row,
            f"{who} is already {role_row.label} in cohort '{target_cohort.name}'; nothing was changed.",
        )

    previous: Role | None = None
    if change.previous_role_id is not None:
        previous = await cohort_repo.get_role(change.previous_role_id)

    await _notify_onboarding(subject.id, target_cohort.id)

    if change.created:
        logger.info(
            "back_office_role_assigned",
            user_id=decision.user.id,
            subject_user_id=subject.id,
            cohort_id=target_cohort.id,
            role=role_row.key,
        )
        return RoleAssignmentResult(
            ResultCode.ROLE_ASSIGNED,
            change.membership,
            subject,
            target_cohort,
            role_row,
            None,
            f"Assigned {who} as {role_row.label} in cohort '{target_cohort.name}'.",
        )

    if change.reactivated and previous is not None and previous.id == role_row.id:
        logger.info(
            "back_office_membership_reactivated",
            user_id=decision.user.id,
            subject_user_id=subject.id,
            cohort_id=target_cohort.id,
            role=role_row.key,
        )
        return RoleAssignmentResult(
            ResultCode.ROLE_ASSIGNED,
            change.membership,
            subject,
            target_cohort,
            role_row,
            None,
            f"Re-added {who} as {role_row.label} in cohort '{target_cohort.name}' (the membership had been inactive).",
        )

    previous_label = previous.label if previous else "an inactive membership"
    logger.info(
        "back_office_role_changed",
        user_id=decision.user.id,
        subject_user_id=subject.id,
        cohort_id=target_cohort.id,
        role=role_row.key,
        previous_role=previous.key if previous else None,
    )
    return RoleAssignmentResult(
        ResultCode.ROLE_CHANGED,
        change.membership,
        subject,
        target_cohort,
        role_row,
        previous,
        f"Changed {who} from {previous_label} to {role_row.label} in cohort '{target_cohort.name}'.",
    )


async def _notify_onboarding(user_id: int, cohort_id: int) -> None:
    """Tell onboarding about a role assignment; never let it break administration.

    Args:
        user_id: The person.
        cohort_id: The cohort.
    """
    try:
        await onboarding.on_role_assigned(user_id, cohort_id)
    except Exception as e:
        logger.exception("back_office_onboarding_hook_failed", user_id=user_id, cohort_id=cohort_id, error=str(e))


async def open_sprint(
    cohort: str,
    sprint_name: str,
    start_date: str | None = None,
    end_date: str | None = None,
) -> SprintResult:
    """Open (activate) a sprint for a cohort (cohort authority required). Idempotent by (cohort, name).

    Args:
        cohort: Cohort name or numeric id.
        sprint_name: Unique within the cohort, e.g. ``Sprint 1``.
        start_date: ISO date; defaults to today (UTC).
        end_date: ISO date; defaults to ``start + SPRINT_DEFAULT_LENGTH_DAYS``.

    Returns:
        SprintResult: ``SPRINT_OPENED`` (new, or a planned sprint activated) or
        ``SPRINT_ALREADY_OPEN`` (nothing changed).

    Raises:
        AuthorisationRefused: When the requester may not administer this cohort.
        ValidationFailed: Unknown or inactive cohort, bad dates, a completed sprint of that
            name, or an overlap with another non-completed sprint.
    """
    requester = _bound_requester()
    target_cohort = await _resolve_cohort_or_fail(cohort)
    assert target_cohort.id is not None
    decision = await require_cohort_authority(requester, target_cohort.id, action="open_sprint")
    assert decision.user is not None
    require_active_cohort(target_cohort)

    name = _clean_name(sprint_name, what="sprint", max_length=SPRINT_NAME_MAX_LENGTH)
    start, end = resolve_sprint_dates(start_date, end_date)

    existing = await sprint_repo.get_sprint_by_name(target_cohort.id, name)
    if existing is not None:
        return await _open_existing_sprint(existing, target_cohort, decision.user)

    overlaps = await sprint_repo.find_overlapping_sprints(target_cohort.id, start, end)
    if overlaps:
        clash = overlaps[0]
        raise ValidationFailed(
            f"Sprint '{name}' ({start.isoformat()} to {end.isoformat()}) overlaps sprint '{clash.name}' "
            f"({clash.start_date.isoformat()} to {clash.end_date.isoformat()}) in cohort '{target_cohort.name}'."
        )

    try:
        sprint = await sprint_repo.create_sprint(
            cohort_id=target_cohort.id,
            name=name,
            start_date=start,
            end_date=end,
            status=SprintStatus.ACTIVE,
            opened_by_id=decision.user.id,
        )
    except IntegrityError:
        raced = await sprint_repo.get_sprint_by_name(target_cohort.id, name)
        if raced is None:
            raise
        return await _open_existing_sprint(raced, target_cohort, decision.user)

    logger.info("back_office_sprint_opened", user_id=decision.user.id, cohort_id=target_cohort.id, sprint_id=sprint.id)
    return SprintResult(
        ResultCode.SPRINT_OPENED,
        sprint,
        target_cohort,
        f"Opened sprint '{sprint.name}' for cohort '{target_cohort.name}' "
        f"({sprint.start_date.isoformat()} to {sprint.end_date.isoformat()}).",
    )


async def _open_existing_sprint(sprint: Sprint, cohort: Cohort, actor: User) -> SprintResult:
    """Report or activate a sprint that already carries the requested name.

    Args:
        sprint: The stored sprint.
        cohort: Its cohort.
        actor: The authorised requester.

    Returns:
        SprintResult: ``SPRINT_ALREADY_OPEN`` when active, ``SPRINT_OPENED`` when a planned one was activated.

    Raises:
        ValidationFailed: When the sprint is completed or activating it would overlap another sprint.
    """
    assert sprint.id is not None and cohort.id is not None
    window = f"{sprint.start_date.isoformat()} to {sprint.end_date.isoformat()}"
    if sprint.status == SprintStatus.ACTIVE.value:
        logger.info("back_office_sprint_already_open", user_id=actor.id, cohort_id=cohort.id, sprint_id=sprint.id)
        return SprintResult(
            ResultCode.SPRINT_ALREADY_OPEN,
            sprint,
            cohort,
            f"Sprint '{sprint.name}' is already open for cohort '{cohort.name}' ({window}); nothing was changed.",
        )
    if sprint.status == SprintStatus.COMPLETED.value:
        raise ValidationFailed(
            f"Sprint '{sprint.name}' in cohort '{cohort.name}' is already completed ({window}); "
            "choose a new sprint name."
        )

    overlaps = await sprint_repo.find_overlapping_sprints(
        cohort.id, sprint.start_date, sprint.end_date, exclude_id=sprint.id
    )
    if overlaps:
        clash = overlaps[0]
        raise ValidationFailed(
            f"Sprint '{sprint.name}' ({window}) overlaps sprint '{clash.name}' "
            f"({clash.start_date.isoformat()} to {clash.end_date.isoformat()}) in cohort '{cohort.name}'."
        )

    activated = await sprint_repo.set_sprint_status(sprint.id, SprintStatus.ACTIVE)
    if activated is None:
        raise ValidationFailed(f"Sprint '{sprint.name}' disappeared while I was opening it; please try again.")
    logger.info(
        "back_office_sprint_opened", user_id=actor.id, cohort_id=cohort.id, sprint_id=activated.id, activated=True
    )
    return SprintResult(
        ResultCode.SPRINT_OPENED,
        activated,
        cohort,
        f"Opened the planned sprint '{activated.name}' for cohort '{cohort.name}' ({window}).",
    )


# ---------------------------------------------------------------------------
# Scoped reads
# ---------------------------------------------------------------------------


async def list_cohorts() -> CohortListing:
    """List the cohorts the requester may see: all for a superadmin, otherwise their own memberships.

    Returns:
        CohortListing: The cohorts, with the requester's role in each (None for superadmins).

    Raises:
        AuthorisationRefused: When the requester was never synced into ``users``.
    """
    user = await require_requester_user(_requester(), action="list_cohorts")
    assert user.id is not None

    if user.is_superadmin:
        cohorts = await cohort_repo.list_cohorts(active_only=False)
        entries: list[tuple[Cohort, Role | None]] = [(cohort, None) for cohort in cohorts]
    else:
        memberships = await cohort_repo.list_memberships_for_user(user.id, active_only=True)
        entries = [(cohort, role) for _membership, cohort, role in memberships]

    logger.info("back_office_cohorts_listed", user_id=user.id, count=len(entries), superadmin=user.is_superadmin)
    return CohortListing(ResultCode.OK, entries, user.is_superadmin, _describe_cohorts(entries, user.is_superadmin))


def _describe_cohorts(entries: list[tuple[Cohort, Role | None]], is_superadmin: bool) -> str:
    """Render a cohort listing as one readable sentence block.

    Args:
        entries: Cohorts with the requester's role in each.
        is_superadmin: Whether the listing is platform-wide.

    Returns:
        str: The text the agent relays.
    """
    if not entries:
        return "You are not a member of any cohort yet." if not is_superadmin else "There are no cohorts yet."
    lines = []
    for cohort, role in entries:
        state = "" if cohort.is_active else " (inactive)"
        held = f" — your role: {role.label}" if role else ""
        lines.append(f"- {cohort.name} (id {cohort.id}){state}{held}")
    heading = "All cohorts:" if is_superadmin else "Your cohorts:"
    return heading + "\n" + "\n".join(lines)


async def list_cohort_members(cohort: str) -> CohortMembersResult:
    """List a cohort's active members and their roles (membership of that cohort required).

    Args:
        cohort: Cohort name or numeric id.

    Returns:
        CohortMembersResult: The members.

    Raises:
        AuthorisationRefused: When the requester is neither a member nor a superadmin.
        ValidationFailed: When the cohort does not exist.
    """
    requester = _bound_requester()
    target_cohort = await _resolve_cohort_or_fail(cohort)
    assert target_cohort.id is not None
    decision = await require_cohort_membership(requester, target_cohort.id, action="list_cohort_members")
    assert decision.user is not None

    members = await cohort_repo.list_cohort_members(target_cohort.id, active_only=True)
    logger.info("back_office_members_listed", user_id=decision.user.id, cohort_id=target_cohort.id, count=len(members))
    if not members:
        text = f"Cohort '{target_cohort.name}' has no members yet."
    else:
        rows = [f"- @{m.user.username} — {m.role.label}" for m in members]
        text = f"Members of cohort '{target_cohort.name}' ({len(members)}):\n" + "\n".join(rows)
    return CohortMembersResult(ResultCode.OK, target_cohort, members, text)
