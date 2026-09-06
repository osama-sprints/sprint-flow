"""Proactive role-aware onboarding: journey policy, content and delivery.

Three responsibilities live here, deliberately apart from the pieces around them:

- **Journey policy** — ``start_journey`` (what an arrival enqueues) and
  ``on_role_assigned`` (what a later role assignment adds). Both only write
  outbox rows through ``app.services.domain.onboarding``; nothing is sent from
  an event handler, so a slow Mattermost can never delay the WebSocket loop.
- **Content** — Markdown templates in ``app/core/prompts/onboarding`` rendered
  per (step kind, role) with ``str.format``. Loaded once at import.
- **Delivery** — ``deliver_step`` takes one *claimed* outbox row, resolves the
  person's role at delivery time, honours the cohort kill switch, sends the DM
  through the REST client and only then marks the row sent. No database
  session is ever open across the network call: every data-access function
  opens and closes its own.

The loop that claims rows and calls ``deliver_step`` is
``app.workers.onboarding_dispatcher``; the arrival event handler is
``app.services.mattermost_ws``. Neither is imported here — the dispatcher
registers a wake-up callback instead, which keeps the modules acyclic.
"""

from dataclasses import (
    dataclass,
    field,
)
from datetime import (
    datetime,
    timedelta,
)
from enum import StrEnum
from pathlib import Path
from typing import (
    Callable,
    Sequence,
)

from app.core.config import settings
from app.core.logging import logger
from app.models import (
    Cohort,
    CohortMembership,
    OnboardingStep,
    Role,
    User,
    utcnow,
)
from app.models.enums import (
    COHORT_ADMIN_ROLES,
    ROLE_LABELS,
    OnboardingStepKind,
    OnboardingStepStatus,
    RoleKey,
)
from app.services.domain import cohorts as cohort_repo
from app.services.domain import identity as identity_repo
from app.services.domain import onboarding as outbox
from app.services.mattermost import mattermost_client

# ---------------------------------------------------------------------------
# Content
# ---------------------------------------------------------------------------

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "core" / "prompts" / "onboarding"

# The template key used when a person holds no active role yet.
NO_ROLE = "no_role"

# Every role variant a template exists for, in the order the tests iterate.
ROLE_VARIANTS: tuple[str, ...] = (*[role.value for role in RoleKey], NO_ROLE)

# Placeholders every template may use; rendering always supplies all of them.
TEMPLATE_FIELDS: tuple[str, ...] = ("first_name", "cohort_name", "role_label", "bot_handle", "lead_handles")

# Retry backoff is capped so a long Mattermost outage never pushes a retry
# further than an hour away.
MAX_BACKOFF_SECONDS = 3600

# Shown in place of lead handles when the cohort has no lead yet.
NO_LEADS_TEXT = "not assigned yet"


def _load_templates(directory: Path) -> dict[tuple[str, str], str]:
    """Read every ``<kind>_<role>.md`` template once.

    Args:
        directory: The folder holding the Markdown files.

    Returns:
        dict[tuple[str, str], str]: ``(step_kind, role_variant)`` to template text.

    Raises:
        FileNotFoundError: When a required template is missing — a deployment
            error that should surface at import, not at the first delivery.
    """
    loaded: dict[tuple[str, str], str] = {}
    for kind in OnboardingStepKind:
        for variant in ROLE_VARIANTS:
            path = directory / f"{kind.value}_{variant}.md"
            if not path.is_file():
                raise FileNotFoundError(f"onboarding template missing: {path}")
            loaded[(kind.value, variant)] = path.read_text(encoding="utf-8").strip()
    return loaded


TEMPLATES: dict[tuple[str, str], str] = _load_templates(TEMPLATES_DIR)


# ---------------------------------------------------------------------------
# Role context
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RoleContext:
    """The role a message is tailored to.

    Attributes:
        role_key: ``learner``, ``tech_lead``, ``ops_support`` or ``scrum_master``.
        role_label: Display label for the role.
        cohort_id: The cohort the role is held in.
        cohort_name: Its name, for the message.
        lead_handles: ``@handles`` of the cohort's tech leads and scrum masters, excluding the person.
    """

    role_key: str
    role_label: str
    cohort_id: int
    cohort_name: str
    lead_handles: tuple[str, ...] = ()


@dataclass(frozen=True)
class MembershipInfo:
    """One active membership, reduced to what onboarding decisions need."""

    cohort_id: int
    cohort_name: str
    cohort_active: bool
    role_key: str
    joined_at: datetime


@dataclass(frozen=True)
class OnboardingContext:
    """Everything ``deliver_step`` knows about a person at delivery time.

    Attributes:
        role: The role to tailor to (the step's cohort when it has one, else the
            most recent active membership in an active cohort), or None.
        memberships: Every ACTIVE membership, whatever the cohort's state.
        step_cohort: The step's cohort row when the step is cohort-scoped.
    """

    role: RoleContext | None
    memberships: tuple[MembershipInfo, ...] = ()
    step_cohort: Cohort | None = None


def pick_primary_membership(memberships: Sequence[MembershipInfo]) -> MembershipInfo | None:
    """Choose the membership a workspace-level message is tailored to.

    Only active cohorts count; among them the most recently joined wins, so a
    person moved to a new cohort is oriented for the new one.

    Args:
        memberships: Active memberships in any cohort state.

    Returns:
        MembershipInfo | None: The chosen membership, or None when no active cohort holds one.
    """
    candidates = [m for m in memberships if m.cohort_active]
    if not candidates:
        return None
    return max(candidates, key=lambda m: (m.joined_at, m.cohort_id))


def _membership_info(rows: Sequence[tuple[CohortMembership, Cohort, Role]]) -> tuple[MembershipInfo, ...]:
    """Reduce joined membership rows to ``MembershipInfo`` values.

    Args:
        rows: ``(membership, cohort, role)`` tuples from the data-access layer.

    Returns:
        tuple[MembershipInfo, ...]: One entry per row with a persisted cohort id.
    """
    return tuple(
        MembershipInfo(
            cohort_id=cohort.id,
            cohort_name=cohort.name,
            cohort_active=cohort.is_active,
            role_key=role.key,
            joined_at=membership.joined_at,
        )
        for membership, cohort, role in rows
        if cohort.id is not None
    )


async def _lead_handles(cohort_id: int, exclude_user_id: int) -> tuple[str, ...]:
    """Collect the ``@handles`` of a cohort's leads.

    Args:
        cohort_id: The cohort.
        exclude_user_id: The person being onboarded, left out of their own lead list.

    Returns:
        tuple[str, ...]: Handles of active tech leads and scrum masters, join order.
    """
    members = await cohort_repo.list_cohort_members(cohort_id, active_only=True)
    admin_keys = {role.value for role in COHORT_ADMIN_ROLES}
    return tuple(
        f"@{member.user.username}"
        for member in members
        if member.role.key in admin_keys and member.user.id != exclude_user_id and member.user.username
    )


async def _role_context_for(membership: MembershipInfo, user_id: int) -> RoleContext:
    """Build the ``RoleContext`` for one membership.

    Args:
        membership: The membership to tailor to.
        user_id: The person, excluded from the lead list.

    Returns:
        RoleContext: The context with lead handles resolved.
    """
    role_key = RoleKey(membership.role_key) if membership.role_key in RoleKey.__members__.values() else None
    label = ROLE_LABELS[role_key] if role_key is not None else membership.role_key.replace("_", " ").title()
    return RoleContext(
        role_key=membership.role_key,
        role_label=label,
        cohort_id=membership.cohort_id,
        cohort_name=membership.cohort_name,
        lead_handles=await _lead_handles(membership.cohort_id, user_id),
    )


async def resolve_role_context(user_id: int, cohort_id: int | None = None) -> RoleContext | None:
    """Resolve the role a message for this person should be tailored to.

    Args:
        user_id: The person.
        cohort_id: When given, the role held in that cohort (an orientation step);
            otherwise the most recent active membership in an active cohort.

    Returns:
        RoleContext | None: The role context, or None when the person holds no
            usable role yet (arrived before their role was assigned).
    """
    context = await resolve_onboarding_context(user_id, cohort_id)
    return context.role


async def resolve_onboarding_context(user_id: int, cohort_id: int | None = None) -> OnboardingContext:
    """Read everything a delivery decision needs, in short independent queries.

    Args:
        user_id: The person.
        cohort_id: The step's cohort, or None for workspace-level steps.

    Returns:
        OnboardingContext: Role, active memberships and the step's cohort row.
    """
    rows = await cohort_repo.list_memberships_for_user(user_id, active_only=True)
    memberships = _membership_info(rows)
    step_cohort = await cohort_repo.get_cohort(cohort_id) if cohort_id is not None else None

    if cohort_id is not None:
        chosen = next((m for m in memberships if m.cohort_id == cohort_id and m.cohort_active), None)
    else:
        chosen = pick_primary_membership(memberships)

    role = await _role_context_for(chosen, user_id) if chosen is not None else None
    return OnboardingContext(role=role, memberships=memberships, step_cohort=step_cohort)


# ---------------------------------------------------------------------------
# Halting (the cohort kill switch)
# ---------------------------------------------------------------------------


def is_halted(step: OnboardingStep, context: OnboardingContext) -> str | None:
    """Decide whether a step must not be delivered right now.

    Args:
        step: The claimed outbox row.
        context: What is known about the person at delivery time.

    Returns:
        str | None: A reason (``cohort_inactive``, ``all_cohorts_inactive``,
            ``membership_missing``) when the step must wait, None when it may go out.
            A person with no memberships at all is NOT halted — that is the
            "arrived before their role" case and gets the no-role content.
    """
    if step.cohort_id is not None:
        cohort = context.step_cohort
        if cohort is None or not cohort.is_active:
            return "cohort_inactive"
        if context.role is None or context.role.cohort_id != step.cohort_id:
            return "membership_missing"
        return None

    if context.memberships and not any(m.cohort_active for m in context.memberships):
        return "all_cohorts_inactive"
    return None


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def first_name_of(user: User) -> str:
    """Pick how to address a person.

    Args:
        user: The stored user row.

    Returns:
        str: The first word of the display name, else the handle.
    """
    display = (user.display_name or "").strip()
    if display:
        return display.split()[0]
    return user.username or "there"


def render_message(step_kind: str | OnboardingStepKind, context: RoleContext | None, user: User) -> str:
    """Render the Markdown for one step.

    Args:
        step_kind: ``welcome``, ``orientation`` or ``follow_up``.
        context: The role to tailor to, or None for the no-role variant.
        user: The recipient.

    Returns:
        str: The message body.
    """
    kind = OnboardingStepKind(step_kind).value
    variant = context.role_key if context is not None and (kind, context.role_key) in TEMPLATES else NO_ROLE
    template = TEMPLATES[(kind, variant)]
    leads = ", ".join(context.lead_handles) if context is not None and context.lead_handles else NO_LEADS_TEXT
    return template.format(
        first_name=first_name_of(user),
        cohort_name=context.cohort_name if context is not None else "",
        role_label=context.role_label if context is not None else "",
        bot_handle=f"@{settings.MATTERMOST_BOT_USERNAME}",
        lead_handles=leads,
    )


# ---------------------------------------------------------------------------
# Journey policy
# ---------------------------------------------------------------------------

_wake_listeners: list[Callable[[], None]] = []


def register_wake_listener(callback: Callable[[], None]) -> None:
    """Let the dispatcher be nudged when new work is enqueued, without importing it here.

    Args:
        callback: A synchronous, non-raising function (``OnboardingDispatcher.wake``).
    """
    if callback not in _wake_listeners:
        _wake_listeners.append(callback)


def _notify_dispatcher() -> None:
    """Call every registered wake listener; a failing listener is logged, never raised."""
    for callback in _wake_listeners:
        try:
            callback()
        except Exception as e:
            logger.exception("onboarding_wake_listener_failed", error=str(e))


def follow_up_due(now: datetime) -> datetime:
    """When the follow-up for an arrival at ``now`` is due.

    Args:
        now: The arrival instant (timezone-aware).

    Returns:
        datetime: ``now`` plus ``ONBOARDING_FOLLOW_UP_DELAY_HOURS``.
    """
    return now + timedelta(hours=settings.ONBOARDING_FOLLOW_UP_DELAY_HOURS)


async def start_journey(user: User, *, now: datetime | None = None) -> bool:
    """Begin (idempotently) the onboarding journey for a person who just arrived.

    Enqueues the workspace-level ``welcome`` (due now) and ``follow_up`` (due
    later); both inserts are ``ON CONFLICT DO NOTHING`` on the unique key, so a
    replayed ``new_user`` event is structurally a no-op. Nothing is sent here.

    Args:
        user: The stored user row for the arrival.
        now: The arrival instant; defaults to now. Verification passes a future
            instant so its rows are invisible to the live dispatcher.

    Returns:
        bool: True only when this call created the welcome row.
    """
    if not settings.ONBOARDING_ENABLED:
        return False
    if user.id is None:
        logger.warning("onboarding_arrival_without_user_id", username=user.username)
        return False

    arrived_at = now or utcnow()
    welcome, created = await outbox.enqueue_step(
        user_id=user.id, cohort_id=None, step_kind=OnboardingStepKind.WELCOME, due_at=arrived_at
    )
    await outbox.enqueue_step(
        user_id=user.id, cohort_id=None, step_kind=OnboardingStepKind.FOLLOW_UP, due_at=follow_up_due(arrived_at)
    )
    if not created:
        logger.info(
            "onboarding_arrival_replayed",
            user_id=user.id,
            username=user.username,
            welcome_step_id=welcome.id,
            welcome_status=welcome.status,
        )
        return False

    logger.info("onboarding_journey_started", user_id=user.id, username=user.username, welcome_step_id=welcome.id)
    _notify_dispatcher()
    return True


def _claim_in_flight(step: OnboardingStep) -> bool:
    """Whether a worker currently holds the step's lease (claimed within the lease window).

    Args:
        step: The step.

    Returns:
        bool: True when a delivery may be in progress right now.
    """
    if step.claimed_at is None:
        return False
    return utcnow() - step.claimed_at < timedelta(seconds=settings.ONBOARDING_CLAIM_LEASE_SECONDS)


async def on_role_assigned(user_id: int, cohort_id: int, *, now: datetime | None = None) -> None:
    """React to a role assignment: queue the cohort orientation unless the welcome will carry it.

    Called by the back-office ``assign_role`` tool after its commit. Three cases:
    the welcome is still pending (it will carry the orientation — nothing to do),
    an orientation for this cohort is already recorded (sent or pending — nothing
    to do), or the person was welcomed without a role, was never welcomed, or
    joined a further cohort (enqueue the orientation, due now).

    Args:
        user_id: The person.
        cohort_id: The cohort they were assigned a role in.
        now: The due instant; defaults to now. Verification passes a future instant.
    """
    if not settings.ONBOARDING_ENABLED:
        return

    welcome = await outbox.get_step_for(user_id, None, OnboardingStepKind.WELCOME)
    if welcome is not None and welcome.status == OnboardingStepStatus.PENDING.value and not _claim_in_flight(welcome):
        # The welcome has not been picked up yet, so it will resolve the role at
        # delivery time and carry this orientation. A welcome that is already
        # claimed may have resolved the role BEFORE this assignment, so the
        # orientation is enqueued on its own (idempotent; if the welcome does
        # carry it after all, _record_carried_orientation marks it sent).
        logger.info("onboarding_orientation_deferred_to_welcome", user_id=user_id, cohort_id=cohort_id)
        return

    existing = await outbox.get_step_for(user_id, cohort_id, OnboardingStepKind.ORIENTATION)
    if existing is not None:
        logger.info(
            "onboarding_orientation_already_recorded",
            user_id=user_id,
            cohort_id=cohort_id,
            status=existing.status,
        )
        return

    step, created = await outbox.enqueue_step(
        user_id=user_id, cohort_id=cohort_id, step_kind=OnboardingStepKind.ORIENTATION, due_at=now or utcnow()
    )
    logger.info(
        "onboarding_orientation_enqueued",
        user_id=user_id,
        cohort_id=cohort_id,
        step_id=step.id,
        created=created,
        welcomed_without_role=welcome is not None and welcome.role_key_at_delivery is None,
        welcome_missing=welcome is None,
    )
    if created:
        _notify_dispatcher()


# ---------------------------------------------------------------------------
# Delivery
# ---------------------------------------------------------------------------


class DeliveryOutcome(StrEnum):
    """What happened to one claimed step. Also the ``outcome`` metric label."""

    SENT = "sent"
    RETRY = "retry"
    FAILED = "failed"
    HALTED = "halted"
    SKIPPED = "skipped"


@dataclass
class DeliveryResult:
    """Outcome of ``deliver_step`` with the details verification wants."""

    outcome: DeliveryOutcome
    step_id: int | None
    step_kind: str
    reason: str | None = None
    post_id: str | None = None
    role_key: str | None = None
    extra_step_ids: list[int] = field(default_factory=list)


def backoff_seconds(attempt: int, base_seconds: int | None = None, cap_seconds: int = MAX_BACKOFF_SECONDS) -> int:
    """Exponential retry delay after a failed attempt.

    Args:
        attempt: Attempts made so far, before this failure (0 for the first).
        base_seconds: Base delay; defaults to ``ONBOARDING_RETRY_BACKOFF_SECONDS``.
        cap_seconds: Upper bound.

    Returns:
        int: ``base * 2**attempt`` capped at ``cap_seconds``, never below 1.
    """
    base = settings.ONBOARDING_RETRY_BACKOFF_SECONDS if base_seconds is None else base_seconds
    return max(1, min(base * (2 ** max(0, attempt)), cap_seconds))


async def _send_dm(user: User, message: str) -> str:
    """Open the DM channel and post the message.

    Args:
        user: The recipient.
        message: Markdown body.

    Returns:
        str: The created post id.

    Raises:
        RuntimeError: When the channel could not be opened or the post was refused.
    """
    channel = await mattermost_client.create_direct_channel(user.mattermost_user_id)
    if not channel or not channel.get("id"):
        raise RuntimeError("could not open the direct channel")
    post = await mattermost_client.create_post(str(channel["id"]), message)
    if not post or not post.get("id"):
        raise RuntimeError("Mattermost did not create the post")
    return str(post["id"])


async def _record_failure(step: OnboardingStep, error: str, now: datetime) -> DeliveryResult:
    """Persist a failed attempt with backoff and decide between retry and giving up.

    Args:
        step: The claimed row (its ``attempt_count`` is the pre-failure count).
        error: What went wrong, for ``last_error``.
        now: The reference instant.

    Returns:
        DeliveryResult: ``retry`` while attempts remain, ``failed`` afterwards.
    """
    assert step.id is not None
    delay = backoff_seconds(step.attempt_count)
    updated = await outbox.mark_step_failed(
        step.id,
        error=error,
        next_attempt_at=now + timedelta(seconds=delay),
        max_attempts=settings.ONBOARDING_MAX_ATTEMPTS,
        claimed_by=step.claimed_by,
    )
    gave_up = updated is not None and updated.status == OnboardingStepStatus.FAILED.value
    logger.warning(
        "onboarding_step_delivery_failed",
        step_id=step.id,
        step_kind=step.step_kind,
        user_id=step.user_id,
        attempt_count=updated.attempt_count if updated else step.attempt_count + 1,
        retry_in_seconds=None if gave_up else delay,
        gave_up=gave_up,
        error=error,
    )
    outcome = DeliveryOutcome.FAILED if gave_up else DeliveryOutcome.RETRY
    return DeliveryResult(outcome=outcome, step_id=step.id, step_kind=step.step_kind, reason=error)


async def _record_carried_orientation(step: OnboardingStep, role: RoleContext, post_id: str) -> list[int]:
    """When a welcome went out with a role, record that cohort's orientation as sent too.

    Args:
        step: The welcome step that was delivered.
        role: The role the welcome was tailored to.
        post_id: The welcome post, recorded on the orientation row as well.

    Returns:
        list[int]: Ids of orientation rows marked sent (at most one).
    """
    orientation, created = await outbox.enqueue_step(
        user_id=step.user_id,
        cohort_id=role.cohort_id,
        step_kind=OnboardingStepKind.ORIENTATION,
        due_at=utcnow(),
    )
    if orientation.status != OnboardingStepStatus.PENDING.value or orientation.id is None:
        return []
    # A pending row may already be CLAIMED by another dispatcher that is
    # delivering it right now. Marking it sent here would clear that claim and
    # leave the other worker unable to settle its own delivery, so settle only
    # an unclaimed row and let a live claim run its course.
    settled = await outbox.mark_step_sent(
        orientation.id,
        mattermost_post_id=post_id,
        role_key=role.role_key,
        require_unclaimed=True,
        lease_seconds=settings.ONBOARDING_CLAIM_LEASE_SECONDS,
    )
    if settled is None:
        logger.info(
            "onboarding_orientation_left_to_its_claimant",
            user_id=step.user_id,
            cohort_id=role.cohort_id,
            orientation_step_id=orientation.id,
        )
        return []
    logger.info(
        "onboarding_orientation_carried_by_welcome",
        user_id=step.user_id,
        cohort_id=role.cohort_id,
        orientation_step_id=orientation.id,
        created=created,
    )
    return [orientation.id]


async def deliver_step(step: OnboardingStep, *, now: datetime | None = None) -> DeliveryResult:
    """Deliver one claimed outbox row and settle its state. Never raises for delivery errors.

    Args:
        step: A row returned by ``claim_due_steps`` (this worker holds its lease).
        now: The reference instant for backoff scheduling; defaults to now.

    Returns:
        DeliveryResult: What happened.
    """
    reference = now or utcnow()
    if step.id is None:
        return DeliveryResult(outcome=DeliveryOutcome.SKIPPED, step_id=None, step_kind=step.step_kind, reason="no id")

    user = await identity_repo.get_user(step.user_id)
    if user is None:
        await outbox.release_claim(step.id)
        logger.warning("onboarding_step_user_missing", step_id=step.id, user_id=step.user_id)
        return DeliveryResult(
            outcome=DeliveryOutcome.SKIPPED, step_id=step.id, step_kind=step.step_kind, reason="user_missing"
        )

    context = await resolve_onboarding_context(step.user_id, step.cohort_id)
    reason = is_halted(step, context)
    if reason is not None:
        await outbox.release_claim(step.id)
        logger.info(
            "onboarding_step_halted_inactive_cohort",
            step_id=step.id,
            step_kind=step.step_kind,
            user_id=step.user_id,
            cohort_id=step.cohort_id,
            reason=reason,
        )
        return DeliveryResult(outcome=DeliveryOutcome.HALTED, step_id=step.id, step_kind=step.step_kind, reason=reason)

    role = context.role
    message = render_message(step.step_kind, role, user)

    # Network I/O happens here with no database session open: every
    # data-access call above has already committed and closed its own.
    try:
        post_id = await _send_dm(user, message)
    except Exception as e:
        return await _record_failure(step, f"{type(e).__name__}: {e}", reference)

    role_key = role.role_key if role is not None else None
    settled = await outbox.mark_step_sent(
        step.id, mattermost_post_id=post_id, role_key=role_key, claimed_by=step.claimed_by
    )
    if settled is None:
        # Our lease expired mid-delivery and another worker took the row: the
        # message went out, but the other worker owns the outcome now. This is
        # the documented at-least-once window; make it visible.
        logger.error(
            "onboarding_claim_lost_after_send",
            step_id=step.id,
            step_kind=step.step_kind,
            user_id=step.user_id,
            post_id=post_id,
            worker=step.claimed_by,
        )
    extra: list[int] = []
    if step.step_kind == OnboardingStepKind.WELCOME.value and role is not None:
        extra = await _record_carried_orientation(step, role, post_id)
    logger.info(
        "onboarding_step_sent",
        step_id=step.id,
        step_kind=step.step_kind,
        user_id=step.user_id,
        cohort_id=step.cohort_id if step.cohort_id is not None else (role.cohort_id if role else None),
        role_key=role_key,
        post_id=post_id,
        carried_orientation=bool(extra),
    )
    return DeliveryResult(
        outcome=DeliveryOutcome.SENT,
        step_id=step.id,
        step_kind=step.step_kind,
        post_id=post_id,
        role_key=role_key,
        extra_step_ids=extra,
    )
