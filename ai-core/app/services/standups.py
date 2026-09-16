"""Proactive daily standup collection: timezone math, prompt text, delivery, parsing, ingestion.

One responsibility per block, kept intentionally thin:

- **Timezones** — every "what day is it for this person?" question is answered
  here. The learner's zone comes from the stored ``users.timezone`` (resolved
  through ``resolve_timezone``), the prompt's day and dispatch moment are
  computed in that zone and persisted on the row, so later attribution never
  re-derives "which day" from the server's clock.
- **Content** — the three question blocks asked first thing in a learner's day.
- **Delivery** — ``deliver_prompt`` takes one *claimed* prompt, re-checks that
  the person still holds an active learner role in the sprint's channel, opens
  the DM, posts, and only then marks the row dispatched. No database session is
  open across the network call, exactly like onboarding's delivery.
- **Parsing** — ``parse_standup_reply`` is deterministic (numbered 1/2/3,
  then labeled sections, then a flat fallback) so the stored fields are
  reproducible from the raw text that ``standup_replies`` keeps losslessly.
- **Ingestion** — ``ingest_standup_reply`` attributes an arriving DM to the
  outstanding prompt for that channel, records the raw reply, and only when
  the day is still open parses it into a ``daily_standups`` entry. First
  submission wins; anything after that, or after the day closed, is kept raw
  and acknowledged as duplicate/late, never rewritten.
"""

import re
from dataclasses import (
    dataclass,
)
from datetime import (
    date,
    datetime,
    time,
    timedelta,
)
from enum import StrEnum
from typing import (
    Collection,
    NamedTuple,
)
from zoneinfo import ZoneInfo

from app.core.config import settings
from app.core.logging import logger
from app.models import (
    DailyStandupPrompt,
    Sprint,
    User,
    require_aware,
    utcnow,
)
from app.models.enums import (
    RoleKey,
    StandupPromptStatus,
    StandupReplyOutcome,
)
from app.services.domain import channels as channel_repo
from app.services.domain import identity as identity_repo
from app.services.domain import sprints as sprint_repo
from app.services.domain import standups as standup_repo
from app.services.identity import sync_mattermost_user
from app.services.mattermost import mattermost_client

# Big enough to survive a slow client plus our retries; bounded so a stale
# learner record is never taken over by someone else's dispatcher mid-flight.
DEFAULT_CLAIM_LEASE_SECONDS = 300

FALLBACK_TIMEZONE = "UTC"

# Bare acknowledgements that are polite, not answers. A reply that is only this
# is left to the chat pipeline instead of being parsed into a standup entry.
ACK_ONLY_WORDS = frozenset(
    {"ok", "okay", "k", "fine", "thanks", "thx", "thankyou", "ty", "noted", "gotit", "received", "sure", "surething", "cool", "done"}
)

# Human-facing labels (lowercase) accepted as the first word of a section line.
_DID_LABELS = frozenset({"done", "did", "progress", "completed", "finished", "what_i_did", "i_did", "worked"})
_WILL_LABELS = frozenset(
    {"planned", "plan", "next", "todo", "doing", "will_do", "what_i_will_do", "up_next", "queue", "upcoming"}
)
_BLOCK_LABELS = frozenset({"blocked", "blockers", "blocking", "risks", "issues", "problems", "stuck"})

_ACK_RE = re.compile(r"[^a-z0-9]")
_NUMBERED_RE = re.compile(r"^\s*\d[\)\.\:]+\s*(.*)$")


# ---------------------------------------------------------------------------
# Timezones
# ---------------------------------------------------------------------------


def resolve_timezone(user: User) -> str:
    """The IANA zone a learner's day belongs to, never raising.

    The stored ``users.timezone`` wins when it names a real zone; anything else
    (missing, malformed, or a name ``zoneinfo`` does not know) falls back to
    UTC so no learner is ever unprompted because of a bad profile field.

    Args:
        user: The stored user row.

    Returns:
        str: An IANA zone name.
    """
    declared = (user.timezone or "").strip()
    if declared:
        try:
            ZoneInfo(declared)
            return declared
        except Exception:
            logger.warning("standup_invalid_timezone", user_id=user.id, timezone=declared)
    return FALLBACK_TIMEZONE


def local_date_of(instant: datetime, zone: str) -> date:
    """The calendar day ``instant`` falls on in ``zone``.

    Args:
        instant: Timezone-aware instant.
        zone: IANA zone.

    Returns:
        date: The day in that zone.
    """
    return instant.astimezone(ZoneInfo(zone)).date()


def dispatch_at_for(local_day: date, zone: str, hour: int) -> datetime:
    """The UTC instant of the configured local hour on ``local_day``.

    Args:
        local_day: The day.
        zone: IANA zone.
        hour: Hour of day (0-23).

    Returns:
        datetime: Timezone-aware UTC instant.
    """
    naive = datetime.combine(local_day, time(hour, 0))
    return naive.replace(tzinfo=ZoneInfo(zone)).astimezone(ZoneInfo("UTC"))


def day_end_utc(local_day: date, zone: str) -> datetime:
    """The UTC instant when ``local_day`` ends in ``zone`` (start of the next day).

    Args:
        local_day: The day.
        zone: IANA zone.

    Returns:
        datetime: First instant of the following day, in UTC.
    """
    next_day = datetime.combine(local_day + timedelta(days=1), time(0, 0))
    return next_day.replace(tzinfo=ZoneInfo(zone)).astimezone(ZoneInfo("UTC"))


# ---------------------------------------------------------------------------
# Content
# ---------------------------------------------------------------------------


def render_prompt(user: User) -> str:
    """The three-question standup DM for one learner.

    Args:
        user: The recipient.

    Returns:
        str: Markdown asking for an update under numbered labels the parser knows.
    """
    first = (user.display_name or user.username or "there").strip().split()[0]
    return (
        f"Good morning {first} — time for a quick standup.\n\n"
        "Reply with three short lines:\n"
        "1. What did you do?\n"
        "2. What will you do next?\n"
        "3. Any blockers?"
    )


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


class ParsedStandup(NamedTuple):
    """The deterministic extraction from one reply."""

    what_i_did: str
    what_i_will_do: str
    blockers: str | None


def _as_section(remaining: list[str]) -> str:
    """Join non-empty remainder lines into one section string.

    Args:
        remaining: Lines to fold.

    Returns:
        str: Trimmed, newline-joined text, or ``""``.
    """
    joined = "\n".join(line.strip() for line in remaining if line.strip())
    return joined.strip()


def _match_label(line: str, labels: frozenset[str]) -> str | None:
    """The label a line announces, if any.

    Args:
        line: The line, untrimmed.
        labels: Accepted first-word labels, lowercase.

    Returns:
        str | None: The matched label word, or None.
    """
    head = line.strip().casefold().split(" ", 1)[0].strip(":.")
    return head if head in labels else None


def parse_standup_reply(text: str) -> ParsedStandup:
    """Extract structured fields from a standup answer, deterministically.

    Three strategies, in order; the first that yields a third section wins.

    1. **Numbered** — the reply lists ``1.`` / ``2.`` items (a third ``3.`` line
       becomes blockers); everything after each number becomes the matching field.
    2. **Labeled** — the reply uses first-word labels (``done``: / ``plan``: /
       ``blocked``:); each labeled block becomes the matching field.
    3. **Flat fallback** — the whole reply is progress.

    A section that was never present becomes an empty string (or None for
    blockers), so even a two-line answer is stored truthfully.

    Args:
        text: The raw reply.

    Returns:
        ParsedStandup: Extracted fields.
    """
    if not text or not text.strip():
        return ParsedStandup("", "", None)

    lines = text.splitlines()

    # 1. Numbered 1/2/3.
    sections: list[list[str]] = []
    current: list[str] | None = None
    for line in lines:
        match = _NUMBERED_RE.match(line)
        if match is not None:
            sections.append([match.group(1).strip()])
            current = sections[-1]
        elif current is not None:
            current.append(line)
    if len(sections) >= 2:
        return ParsedStandup(
            what_i_did=_as_section(sections[0]),
            what_i_will_do=_as_section(sections[1]),
            blockers=_as_section(sections[2]) if len(sections) > 2 else None,
        )

    # 2. Labeled sections.
    did: list[str] = []
    will: list[str] = []
    block: list[str] = []
    bucket: list[str] | None = None
    for line in lines:
        stripped = line.strip()
        label = _match_label(stripped, _DID_LABELS) or _match_label(stripped, _WILL_LABELS) or _match_label(
            stripped, _BLOCK_LABELS
        )
        if label is not None:
            bucket = (
                did
                if label in _DID_LABELS
                else (will if label in _WILL_LABELS else block)
            )
            _, _, rest = stripped.partition(" ")
            if rest.strip():
                bucket.append(rest.strip())
            continue
        if bucket is not None and stripped:
            bucket.append(stripped)
    if did or will or block:
        return ParsedStandup(
            what_i_did=_as_section(did),
            what_i_will_do=_as_section(will),
            blockers=_as_section(block) or None,
        )

    # 3. Flat fallback.
    return ParsedStandup(what_i_did=text.strip(), what_i_will_do="", blockers=None)


def is_ack_only(text: str) -> bool:
    """Whether a reply is a bare acknowledgement rather than a standup answer.

    Args:
        text: The raw message.

    Returns:
        bool: True when the message is punctuation-normalised to a known ack word.
    """
    normalized = _ACK_RE.sub("", text.casefold()).strip()
    return normalized in ACK_ONLY_WORDS


def starts_with_mention(text: str, bot_username: str) -> bool:
    """Whether a message opens by addressing the bot directly.

    A message that starts ``@assistant ...`` is an explicit ask, not an answer,
    so it must not be swallowed by the standup collector.

    Args:
        text: The raw message.
        bot_username: The bot's Mattermost handle without ``@``.

    Returns:
        bool: True when the first word is an at-mention of the bot.
    """
    words = text.lstrip().split()
    if not words:
        return False
    first = words[0].casefold()
    return first == ("@" + bot_username).casefold() or first.lstrip("@") == bot_username.casefold()


# ---------------------------------------------------------------------------
# Delivery
# ---------------------------------------------------------------------------


class DeliveryOutcome(StrEnum):
    """What happened to one claimed prompt. Also the ``outcome`` metric label."""

    SENT = "sent"
    RETRY = "retry"
    FAILED = "failed"
    HALTED = "halted"
    SKIPPED = "skipped"


@dataclass
class DeliveryResult:
    """Outcome of ``deliver_prompt`` with the context the worker logs."""

    outcome: DeliveryOutcome
    prompt_id: int | None
    reason: str | None = None
    post_id: str | None = None


def backoff_seconds(attempt: int, base_seconds: int | None = None, cap_seconds: int = 60 * 60) -> int:
    """Exponential retry delay after a failed dispatch.

    Args:
        attempt: Attempts made so far, before this failure.
        base_seconds: Base delay; defaults to ``STANDUP_RETRY_BACKOFF_SECONDS``.
        cap_seconds: Upper bound.

    Returns:
        int: ``base * 2**attempt`` capped, never below 1.
    """
    base = settings.STANDUP_RETRY_BACKOFF_SECONDS if base_seconds is None else base_seconds
    return max(1, min(base * (2 ** max(0, attempt)), cap_seconds))


async def _is_still_learner(prompt: DailyStandupPrompt) -> bool:
    """Whether the prompt's person still holds an active learner role in its sprint channel.

    Args:
        prompt: The claimed prompt.

    Returns:
        bool: True when the person remains an active learner in the channel.
    """
    members = await channel_repo.list_channel_roles(prompt.channel_id, active_only=True)
    return any(
        member.user.id == prompt.learner_id and member.role.key == RoleKey.LEARNER.value
        for member in members
    )


async def _send_dm(user: User, message: str) -> tuple[str, str]:
    """Open the DM channel and post the message.

    Args:
        user: The recipient.
        message: Markdown body.

    Returns:
        tuple[str, str]: ``(dm_channel_id, post_id)``.

    Raises:
        RuntimeError: When the channel could not be opened or the post was refused.
    """
    channel = await mattermost_client.create_direct_channel(user.mattermost_user_id)
    if not channel or not channel.get("id"):
        raise RuntimeError("could not open the direct channel")
    post = await mattermost_client.create_post(str(channel["id"]), message)
    if not post or not post.get("id"):
        raise RuntimeError("Mattermost did not create the post")
    return str(channel["id"]), str(post["id"])


async def deliver_prompt(prompt: DailyStandupPrompt, *, now: datetime | None = None) -> DeliveryResult:
    """Deliver one claimed prompt and settle its state. Never raises for delivery errors.

    Args:
        prompt: A row returned by ``claim_due_prompts`` (held by this worker).
        now: Reference instant; defaults to now.

    Returns:
        DeliveryResult: What happened.
    """
    reference = require_aware(now, "now") if now is not None else utcnow()
    if prompt.id is None:
        return DeliveryResult(outcome=DeliveryOutcome.SKIPPED, prompt_id=None, reason="no id")

    user = await identity_repo.get_user(prompt.learner_id)
    if user is None:
        # Identity row missing means we can neither address nor attribute the
        # reply; close the day as missed rather than retry forever.
        await standup_repo.mark_prompt_missed(prompt.id, closed_at=reference)
        logger.warning("standup_prompt_learner_missing", prompt_id=prompt.id, learner_id=prompt.learner_id)
        return DeliveryResult(outcome=DeliveryOutcome.SKIPPED, prompt_id=prompt.id, reason="learner_missing")

    try:
        active = await _is_still_learner(prompt)
    except Exception:
        # A role read failing is a transient problem; retry the send later.
        active = True
    if not active:
        # The person left the sprint before their prompt was due: nothing is
        # sent, and the day closes as missed with the reason visible in the
        # report, rather than failing (a failure would suggest a retry that
        # can never succeed).
        await standup_repo.mark_prompt_missed(prompt.id, closed_at=reference)
        logger.info(
            "standup_prompt_learner_inactive",
            prompt_id=prompt.id,
            learner_id=prompt.learner_id,
            sprint_id=prompt.sprint_id,
        )
        return DeliveryResult(outcome=DeliveryOutcome.HALTED, prompt_id=prompt.id, reason="learner_inactive")

    message = render_prompt(user)
    try:
        dm_channel_id, post_id = await _send_dm(user, message)
    except Exception as e:
        updated = await standup_repo.mark_prompt_failed(
            prompt.id,
            error=f"{type(e).__name__}: {e}",
            next_attempt_at=reference + timedelta(seconds=backoff_seconds(prompt.dispatch_count)),
            max_attempts=settings.STANDUP_MAX_ATTEMPTS,
            claimed_by=prompt.claimed_by,
        )
        gave_up = updated is not None and updated.status == StandupPromptStatus.FAILED.value
        logger.warning(
            "standup_prompt_dispatch_failed",
            prompt_id=prompt.id,
            learner_id=prompt.learner_id,
            attempt_count=updated.dispatch_count if updated else prompt.dispatch_count + 1,
            retry_in_seconds=None if gave_up else backoff_seconds(prompt.dispatch_count),
            gave_up=gave_up,
            error=str(e),
        )
        return DeliveryResult(
            outcome=DeliveryOutcome.FAILED if gave_up else DeliveryOutcome.RETRY,
            prompt_id=prompt.id,
            reason=str(e),
        )

    settled = await standup_repo.mark_prompt_sent(
        prompt.id,
        post_id=post_id,
        dm_channel_id=dm_channel_id,
        claimed_by=prompt.claimed_by,
        now=reference,
    )
    if settled is None:
        logger.error(
            "standup_claim_lost_after_send",
            prompt_id=prompt.id,
            learner_id=prompt.learner_id,
            post_id=post_id,
            worker=prompt.claimed_by,
        )
    logger.info(
        "standup_prompt_sent",
        prompt_id=prompt.id,
        learner_id=prompt.learner_id,
        sprint_id=prompt.sprint_id,
        dm_channel_id=dm_channel_id,
        post_id=post_id,
    )
    return DeliveryResult(outcome=DeliveryOutcome.SENT, prompt_id=prompt.id, post_id=post_id)


# ---------------------------------------------------------------------------
# Dispatching scope (used by the worker and by verification)
# ---------------------------------------------------------------------------


async def ensure_today_prompts(
    sprint: Sprint,
    members: list[channel_repo.ChannelMember],
    *,
    now: datetime | None = None,
    user_ids: Collection[int] | None = None,
) -> int:
    """Create today's prompt for every active learner in one sprint, idempotently.

    A prompt is created only when the learner's local day has already begun —
    the row exists before any DM goes out, and is the whole at-most-once
    guarantee. Learners whose stored zone is UTC keep working even when their
    profile was never updated.

    Args:
        sprint: An active sprint.
        members: That sprint channel's active members.
        now: Reference instant; defaults to now.
        user_ids: Restrict creation to these people (verification harnesses use this so
            a probe never registers real people's prompts); None creates for everyone.

    Returns:
        int: How many new prompt rows were created by this call.
    """
    reference = require_aware(now, "now") if now is not None else utcnow()
    created_count = 0
    for member in members:
        if member.role.key != RoleKey.LEARNER.value:
            continue
        user = member.user
        user_id = user.id
        if user_id is None:
            continue
        if user_ids is not None and user_id not in user_ids:
            continue
        zone = resolve_timezone(user)
        local_day = local_date_of(reference, zone)
        dispatch_at = dispatch_at_for(local_day, zone, settings.STANDUP_PROMPT_LOCAL_HOUR)
        prompt, created = await standup_repo.ensure_prompt(
            sprint_id=sprint.id if sprint.id is not None else 0,
            learner_id=user_id,
            channel_id=sprint.channel_id,
            local_date=local_day,
            timezone=zone,
            dispatch_at=dispatch_at,
            now=reference,
        )
        if created:
            created_count += 1
            logger.info(
                "standup_prompt_registered",
                prompt_id=prompt.id,
                learner_id=user_id,
                sprint_id=sprint.id,
                local_date=local_day.isoformat(),
                timezone=zone,
                dispatch_at=dispatch_at.isoformat(),
            )
    return created_count


async def ensure_scope(
    *,
    now: datetime | None = None,
    created_count_cap: int | None = None,
    user_ids: Collection[int] | None = None,
) -> int:
    """Create today's prompts for every active learner in every active sprint.

    Args:
        now: Reference instant; defaults to now.
        created_count_cap: When given, stop after this many new rows (verification).
        user_ids: Restrict creation to these people (verification harnesses use this so
            a probe never registers real people's prompts); None creates for everyone.

    Returns:
        int: Total new prompt rows created.
    """
    reference = require_aware(now, "now") if now is not None else utcnow()
    total = 0
    sprints = await sprint_repo.list_active_sprints()
    for sprint in sprints:
        if sprint.id is None:
            continue
        members = await channel_repo.list_channel_roles(sprint.channel_id, active_only=True)
        created = await ensure_today_prompts(sprint, members, now=reference, user_ids=user_ids)
        total += created
        if created_count_cap is not None and total >= created_count_cap:
            break
    return total


async def close_missed_prompts(*, now: datetime | None = None) -> int:
    """Close every outstanding prompt whose local day has ended as ``missed``.

    A silent day (or a day whose prompt was never delivered) becomes fact about
    the prompt row only — no standup entry is fabricated for someone who never
    replied.

    Args:
        now: Reference instant; defaults to now.

    Returns:
        int: How many prompts were closed.
    """
    reference = require_aware(now, "now") if now is not None else utcnow()
    closed = 0
    for prompt in await standup_repo.list_outstanding_prompts():
        if prompt.id is None:
            continue
        if reference >= day_end_utc(prompt.local_date, prompt.timezone):
            await standup_repo.mark_prompt_missed(prompt.id, closed_at=reference)
            closed += 1
            logger.info(
                "standup_prompt_missed",
                prompt_id=prompt.id,
                learner_id=prompt.learner_id,
                local_date=prompt.local_date.isoformat(),
                status_before=prompt.status,
            )
    return closed


# ---------------------------------------------------------------------------
# Reply ingestion (called from the WebSocket listener, DM channel only)
# ---------------------------------------------------------------------------


class ReplyResult(StrEnum):
    """What ``ingest_standup_reply`` decided, and what the caller should do next."""

    # Not a standup reply: leave the message to the normal chat pipeline.
    NOT_A_STANDUP = "not_a_standup"
    # Already handled on a previous delivery of this event; do nothing more.
    ALREADY_RECORDED = "already_recorded"
    ACCEPTED = "accepted"
    DUPLICATE = "duplicate"
    LATE = "late"


@dataclass
class IngestResult:
    """Outcome of ``ingest_standup_reply`` with the detail the listener logs."""

    result: ReplyResult
    prompt: DailyStandupPrompt | None = None
    reply_id: int | None = None
    entry_id: int | None = None


_CONFIRMATIONS = {
    ReplyResult.ACCEPTED: "Got it — your standup is recorded. Thanks!",
    ReplyResult.DUPLICATE: "You already submitted a standup for today — no need to repeat it.",
    ReplyResult.LATE: "That standup arrived after the day closed, so it was not added to the report.",
}


async def _find_reply_prompt(
    *,
    learner_id: int,
    dm_channel_id: str,
    root_id: str,
) -> DailyStandupPrompt | None:
    """The prompt an arriving DM answers, or None.

    A message replying to the prompt's post (``root_id``) refers to exactly that
    prompt, whether it is dispatched, answered, or already closed as missed (so
    a second or a late reply classifies instead of disappearing). Any other DM
    is attributed to the learner's newest still-open prompt in that channel.
    Only ``dispatched`` prompts are attributable by thread root or fallback: a
    prompt that has not been posted yet has nothing to answer.

    Args:
        learner_id: The person.
        dm_channel_id: The DM channel.
        root_id: Thread root, when the reply was in-thread.

    Returns:
        DailyStandupPrompt | None: The prompt, or None when nothing is open.
    """
    if root_id:
        prompt = await standup_repo.get_prompt_by_post_id(root_id)
        if prompt is not None:
            if prompt.status in (
                StandupPromptStatus.DISPATCHED.value,
                StandupPromptStatus.ANSWERED.value,
                StandupPromptStatus.MISSED.value,
            ) and prompt.learner_id == learner_id and prompt.dm_channel_id == dm_channel_id:
                return prompt
    candidates: list[DailyStandupPrompt] = []
    for prompt in await standup_repo.list_outstanding_prompts(dm_channel_id=dm_channel_id):
        if prompt.status != StandupPromptStatus.DISPATCHED.value:
            continue
        if prompt.learner_id != learner_id:
            continue
        candidates.append(prompt)

    if not candidates:
        return None
    return max(candidates, key=lambda p: (p.local_date, p.id))


async def _send_confirmation(result: ReplyResult, *, dm_channel_id: str, post_id: str, root_id: str) -> None:
    """Acknowledge a stored reply with a short DM (failures are logged, never raised).

    Args:
        result: The outcome achieved (must have a confirmation text).
        dm_channel_id: The DM channel.
        post_id: The learner's post to reply under, when in-thread.
        root_id: The thread root, when in-thread.
    """
    message = _CONFIRMATIONS.get(result)
    if message is None:
        return
    try:
        if root_id:
            await mattermost_client.reply_to_post(dm_channel_id, message, post_id)
        else:
            await mattermost_client.create_post(dm_channel_id, message)
    except Exception as e:
        logger.warning("standup_confirmation_failed", result=result.value, error=str(e))


async def ingest_standup_reply(
    *,
    mattermost_user_id: str,
    dm_channel_id: str,
    channel_type: str,
    post_id: str,
    root_id: str,
    text: str,
    now: datetime | None = None,
) -> IngestResult:
    """Attribute and store one incoming DM, if it is a standup reply.

    Returns ``ReplyResult.NOT_A_STANDUP`` whenever the message is not ours so
    the WebSocket listener can hand it to the normal chat pipeline unchanged.
    A message is ours only when: it arrives in a direct channel, names no bot
    mention, is not a bare acknowledgement, and matches an outstanding
    dispatched prompt for its author in that channel.

    First submission wins: once a day's entry exists the same day's later
    replies are recorded as ``duplicate`` (raw text preserved, never
    overwriting), and replies that arrive after the day closed as ``late``.

    Args:
        mattermost_user_id: The Mattermost id of the author, before identity sync.
        dm_channel_id: The DM channel.
        channel_type: Mattermost channel type; only ``D`` is collected here.
        post_id: The post id (the redelivery guard).
        root_id: The thread root, empty when not in a thread.
        text: The raw message.
        now: Reference instant; defaults to now.

    Returns:
        IngestResult: What happened, for the listener to log.
    """
    reference = require_aware(now, "now") if now is not None else utcnow()
    if channel_type != "D":
        return IngestResult(result=ReplyResult.NOT_A_STANDUP)
    if not text.strip():
        return IngestResult(result=ReplyResult.NOT_A_STANDUP)
    if starts_with_mention(text, settings.MATTERMOST_BOT_USERNAME):
        return IngestResult(result=ReplyResult.NOT_A_STANDUP)
    if is_ack_only(text):
        return IngestResult(result=ReplyResult.NOT_A_STANDUP)

    user = await sync_mattermost_user(mattermost_user_id)
    if user is None or user.id is None:
        logger.warning("standup_reply_user_unresolvable", mattermost_user_id=mattermost_user_id)
        return IngestResult(result=ReplyResult.NOT_A_STANDUP)

    prompt = await _find_reply_prompt(
        learner_id=user.id,
        dm_channel_id=dm_channel_id,
        root_id=root_id,
    )
    if prompt is None or prompt.id is None:
        return IngestResult(result=ReplyResult.NOT_A_STANDUP)

    zone = prompt.timezone
    arrival_day = local_date_of(reference, zone)

    # The day's date decides between accepted and late: a reply for a *closed*
    # day (already missed, or its local date in the past) is late even before
    # the close loop has run.
    if arrival_day != prompt.local_date:
        outcome = ReplyResult.LATE
    elif prompt.status == StandupPromptStatus.ANSWERED.value:
        outcome = ReplyResult.DUPLICATE
    elif prompt.status == StandupPromptStatus.MISSED.value:
        outcome = ReplyResult.LATE
    elif prompt.status == StandupPromptStatus.DISPATCHED.value:
        outcome = ReplyResult.ACCEPTED
    else:
        outcome = ReplyResult.NOT_A_STANDUP

    if outcome == ReplyResult.NOT_A_STANDUP:
        return IngestResult(result=ReplyResult.NOT_A_STANDUP, prompt=prompt)

    raw_outcome = StandupReplyOutcome.ACCEPTED if outcome == ReplyResult.ACCEPTED else (
        StandupReplyOutcome.DUPLICATE if outcome == ReplyResult.DUPLICATE else StandupReplyOutcome.LATE
    )

    try:
        reply = await standup_repo.record_reply(
            prompt_id=prompt.id,
            learner_id=user.id,
            dm_channel_id=dm_channel_id,
            post_id=post_id,
            post_root_id=root_id or None,
            raw_text=text,
            local_date=arrival_day,
            outcome=raw_outcome,
            received_at=reference,
        )
    except Exception as e:
        logger.warning("standup_reply_record_failed", error=str(e))
        return IngestResult(result=ReplyResult.NOT_A_STANDUP, prompt=prompt)

    if reply is None or reply.id is None:
        # The post was already recorded on a previous delivery of this event:
        # fully handled then, nothing more to store or say.
        return IngestResult(result=ReplyResult.ALREADY_RECORDED, prompt=prompt)

    entry_id: int | None = None
    if outcome == ReplyResult.ACCEPTED:
        parsed = parse_standup_reply(text)
        try:
            entry = await standup_repo.create_standup_entry(
                sprint_id=prompt.sprint_id,
                learner_id=user.id,
                log_date=prompt.local_date,
                what_i_did=parsed.what_i_did or text,
                what_i_will_do=parsed.what_i_will_do or "",
                blockers=parsed.blockers,
                prompt_id=prompt.id,
                raw_response=text,
                submitted_at=reference,
                timezone=zone,
            )
        except Exception as e:
            logger.warning("standup_entry_create_failed", error=str(e))
            entry = None
        if entry is None:
            # A racing first answer won; this reply becomes a duplicate.
            outcome = ReplyResult.DUPLICATE
            try:
                await standup_repo.record_reply(
                    prompt_id=prompt.id,
                    learner_id=user.id,
                    dm_channel_id=dm_channel_id,
                    post_id=post_id,
                    post_root_id=root_id or None,
                    raw_text=text,
                    local_date=arrival_day,
                    outcome=StandupReplyOutcome.DUPLICATE,
                    received_at=reference,
                )
            except Exception:
                pass
        else:
            entry_id = entry.id
            await standup_repo.mark_prompt_answered(prompt.id, answered_at=reference)

    await _send_confirmation(outcome, dm_channel_id=dm_channel_id, post_id=post_id, root_id=root_id)

    logger.info(
        "standup_reply_recorded",
        prompt_id=prompt.id,
        learner_id=user.id,
        outcome=outcome.value,
        reply_id=reply.id,
        entry_id=entry_id,
        in_thread=bool(root_id),
    )
    return IngestResult(result=outcome, prompt=prompt, reply_id=reply.id, entry_id=entry_id)