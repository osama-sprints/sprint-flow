"""Reading the conversation: four ways in, all anchored and all bounded.

Every read starts from the message that triggered the turn. That anchoring is
not a detail: a channel keeps moving while the bot is thinking, and a summary
of "the discussion above" that quietly included three messages posted after
the question would answer a question nobody asked. Nothing newer than the
trigger is ever returned, whichever way in was used.

What each function guarantees:

* the requester's own access to the source channel was checked, and so was the
  destination the reply will land in (see ``access.py``);
* deleted posts, joins, leaves and header changes are never returned;
* the trigger itself and anything already returned this turn are skipped, so
  "ten more" means ten the turn has not seen;
* a cursor comes back whenever earlier messages exist, and the coverage note
  says plainly what was not read. A page that ran out of budget says so; it
  never reports "nothing found" for something it did not look at.
"""

from typing import (
    Any,
    Dict,
    List,
    Optional,
)

from app.core.logging import logger
from app.services.discussion.access import (
    ChannelAccess,
    resolve_channel,
)
from app.services.discussion.context import (
    DiscussionTurn,
    require_turn,
)
from app.services.discussion.policy import POLICY
from app.services.discussion.records import (
    Page,
    Record,
    is_readable_post,
    to_records,
)
from app.services.mattermost import mattermost_client


class RetrievalRefused(Exception):
    """The read was not allowed. ``str(exc)`` is the sentence the sub-agent is given."""


REFUSED_CHANNEL = "You may not read that conversation on this person's behalf."
REFUSED_DESTINATION = (
    "That conversation is private and this reply will be posted somewhere its members are not. It was not read."
)
BUDGET_SPENT = "This turn's reading budget is used up; nothing further was read."


def _page_size(requested: Optional[int], default: int) -> int:
    """Clamp a page size the model asked for.

    Args:
        requested: What was asked for, or None.
        default: The size to use when nothing was asked for.

    Returns:
        int: A size within policy.
    """
    if not requested or requested < 1:
        return default
    return min(int(requested), POLICY.max_page_size)


async def _trigger_created_at(turn: DiscussionTurn) -> int:
    """The timestamp everything is anchored to, read once per turn.

    Args:
        turn: The turn context.

    Returns:
        int: Mattermost timestamp in milliseconds, or 0 when it is unknown.
    """
    if turn.trigger_created_at is None:
        post = await mattermost_client.get_post(turn.trigger_post_id) if turn.trigger_post_id else None
        turn.trigger_created_at = int((post or {}).get("create_at") or 0)
    return turn.trigger_created_at


async def _require_access(channel_id: str, turn: DiscussionTurn) -> ChannelAccess:
    """Check a channel and refuse in the sub-agent's words when it fails.

    Args:
        channel_id: The channel to read.
        turn: The turn context.

    Returns:
        ChannelAccess: The verdict, always allowed.

    Raises:
        RetrievalRefused: When the requester may not read it, or the reply
            destination may not receive it.
    """
    access = await resolve_channel(channel_id, turn)
    if access.allowed:
        return access
    if access.reason == "destination_more_public_than_source":
        raise RetrievalRefused(REFUSED_DESTINATION)
    raise RetrievalRefused(REFUSED_CHANNEL)


def _fresh(records: List[Record], turn: DiscussionTurn, anchor: int) -> List[Record]:
    """Drop the trigger, anything newer than it, and anything already returned.

    Args:
        records: Candidate records, oldest first.
        turn: The turn context.
        anchor: The trigger's timestamp; 0 disables the time cut.

    Returns:
        list[Record]: What is left, oldest first.
    """
    kept: List[Record] = []
    for record in records:
        if record.post_id == turn.trigger_post_id or record.post_id in turn.seen:
            continue
        if anchor and record.created_at > anchor:
            continue
        kept.append(record)
    return kept


def _take(records: List[Record], turn: DiscussionTurn, limit: int) -> tuple[List[Record], str]:
    """Apply the turn's record budget to a page.

    Args:
        records: Fresh records, oldest first.
        turn: The turn context.
        limit: The page size.

    Returns:
        tuple: The records kept, and a note when something was held back.
    """
    allowance = min(limit, turn.budget_remaining)
    if allowance >= len(records):
        return records, ""
    # Keep the ones nearest the question: those are the context it needs.
    kept = records[len(records) - allowance :] if allowance else []
    return kept, (
        f"Only {len(kept)} of {len(records)} messages were kept — this turn's reading budget "
        f"({POLICY.max_records_per_turn} messages) is nearly spent. Say so rather than implying you read everything."
    )


async def channel_messages(
    *,
    cursor: str = "",
    limit: Optional[int] = None,
    turn: Optional[DiscussionTurn] = None,
) -> Page:
    """Read the messages immediately before the trigger, then earlier pages.

    Args:
        cursor: A post id from a previous page; reads what came before it.
        limit: Page size, clamped by policy.
        turn: The turn context; the bound one by default.

    Returns:
        Page: Records oldest first, with a cursor when more history exists.

    Raises:
        RetrievalRefused: When the channel may not be read for this requester.
    """
    turn = turn or require_turn()
    access = await _require_access(turn.channel_id, turn)
    size = _page_size(limit, POLICY.first_page)
    anchor_post = cursor or turn.trigger_post_id
    if turn.budget_remaining <= 0:
        return Page(note=BUDGET_SPENT)

    payload = await mattermost_client.get_channel_posts(turn.channel_id, before=anchor_post, per_page=size)
    if payload is None:
        return Page(note="The channel history could not be read just now; nothing was retrieved.")

    order: List[str] = list(payload.get("order") or [])
    posts: Dict[str, Any] = payload.get("posts") or {}
    records = await to_records([posts[pid] for pid in order if pid in posts], access, turn)
    anchor = await _trigger_created_at(turn)
    fresh = _fresh(records, turn, anchor)
    kept, note = _take(fresh, turn, size)
    turn.remember(kept)

    next_cursor = order[-1] if len(order) >= size and order else None
    hidden = len(records) - len(fresh)
    if hidden > 0:
        note = (
            note + " " if note else ""
        ) + f"{hidden} message(s) on this page were already read earlier in this turn."
    logger.info(
        "discussion_channel_page",
        session_id=turn.session_id,
        channel_id=turn.channel_id,
        asked=size,
        returned=len(kept),
        scanned=len(order),
        cursor=cursor or None,
    )
    return Page(records=kept, cursor=next_cursor, scanned=len(order), note=note.strip())


async def thread_messages(
    *,
    root_id: str = "",
    cursor: str = "",
    limit: Optional[int] = None,
    turn: Optional[DiscussionTurn] = None,
    spend: bool = True,
) -> Page:
    """Read a thread, newest-first by page but returned oldest-first.

    Args:
        root_id: The thread to read; the trigger's own thread by default.
        cursor: A post id from a previous page; reads what came before it.
        limit: Page size, clamped by policy.
        turn: The turn context; the bound one by default.
        spend: False when reading context automatically before the turn, so the
            same messages can still be retrieved by the sub-agent afterwards.

    Returns:
        Page: Records oldest first, with a cursor when the thread runs deeper.

    Raises:
        RetrievalRefused: When the thread's channel may not be read.
    """
    turn = turn or require_turn()
    root = root_id or turn.root_id or turn.trigger_post_id
    if not root:
        return Page(note="This message is not inside a thread.")
    if turn.budget_remaining <= 0:
        return Page(note=BUDGET_SPENT)

    root_post = await mattermost_client.get_post(root)
    if not root_post:
        return Page(note="That thread could not be read.")
    channel_id = str(root_post.get("channel_id") or turn.channel_id)
    access = await _require_access(channel_id, turn)

    payload = await mattermost_client.get_thread(root)
    if payload is None:
        return Page(note="That thread could not be read.")
    posts: Dict[str, Any] = payload.get("posts") or {}
    ordered = sorted(
        (post for post in posts.values() if is_readable_post(post)),
        key=lambda post: int(post.get("create_at") or 0),
    )
    if cursor:
        cut = next((i for i, post in enumerate(ordered) if post.get("id") == cursor), None)
        if cut is not None:
            ordered = ordered[:cut]

    size = _page_size(limit, POLICY.page_size)
    window = ordered[-size:] if len(ordered) > size else ordered
    records = await to_records(window, access, turn)
    anchor = await _trigger_created_at(turn)
    fresh = _fresh(records, turn, anchor)
    kept, note = _take(fresh, turn, size) if spend else (fresh, "")
    turn.remember(kept, spend=spend)

    earlier = len(ordered) - len(window)
    next_cursor = window[0].get("id") if earlier > 0 and window else None
    if earlier > 0:
        note = (note + " " if note else "") + f"{earlier} earlier message(s) in this thread were not read."
    logger.info(
        "discussion_thread_page",
        session_id=turn.session_id,
        root_id=root,
        returned=len(kept),
        thread_size=len(ordered),
    )
    return Page(records=kept, cursor=next_cursor, scanned=len(ordered), note=note.strip())


async def message(post_id: str, *, turn: Optional[DiscussionTurn] = None) -> Page:
    """Resolve one referenced post, with the thread it belongs to.

    Args:
        post_id: The post to open — from a permalink, or cited in the discussion.
        turn: The turn context; the bound one by default.

    Returns:
        Page: The post and a bounded window of its thread, oldest first.

    Raises:
        RetrievalRefused: When its channel may not be read for this requester.
    """
    turn = turn or require_turn()
    post = await mattermost_client.get_post(post_id)
    if not post or not is_readable_post(post):
        return Page(note="No readable message with that id.")
    access = await _require_access(str(post.get("channel_id") or ""), turn)
    anchor = await _trigger_created_at(turn)

    root = str(post.get("root_id") or "") or post_id
    payload = await mattermost_client.get_thread(root)
    posts = list(((payload or {}).get("posts") or {}).values()) or [post]
    ordered = sorted(
        (candidate for candidate in posts if is_readable_post(candidate)),
        key=lambda candidate: int(candidate.get("create_at") or 0),
    )
    window = ordered[-POLICY.page_size :] if len(ordered) > POLICY.page_size else ordered
    if not any(candidate.get("id") == post_id for candidate in window):
        window = [post, *window][: POLICY.page_size]

    records = await to_records(window, access, turn)
    fresh = _fresh(records, turn, anchor)
    kept, note = _take(fresh, turn, POLICY.page_size)
    turn.remember(kept)
    earlier = len(ordered) - len(window)
    if earlier > 0:
        note = (note + " " if note else "") + f"{earlier} earlier message(s) in that thread were not read."
    return Page(records=kept, scanned=len(ordered), note=note.strip())


def _terms(query: str) -> List[str]:
    """Split a search query into the words every match must contain.

    Args:
        query: What to look for.

    Returns:
        list[str]: Lower-cased terms, quotes and punctuation stripped.
    """
    cleaned = query.replace('"', " ").replace("'", " ")
    return [term for term in (word.strip().casefold() for word in cleaned.split()) if len(term) > 1]


async def search(
    query: str,
    *,
    cursor: str = "",
    turn: Optional[DiscussionTurn] = None,
) -> Page:
    """Look for messages in THIS conversation, scanning backwards from the trigger.

    The scan is bounded by policy, and the page says how far back it reached.
    A message that was not scanned is not a message without the phrase, and the
    note says so rather than implying the channel was searched to its start.

    Args:
        query: Words to look for; every word must appear.
        cursor: Continue a previous scan from this post id.
        turn: The turn context; the bound one by default.

    Returns:
        Page: Matching records oldest first, with a cursor when the scan stopped early.

    Raises:
        RetrievalRefused: When the channel may not be read for this requester.
    """
    turn = turn or require_turn()
    access = await _require_access(turn.channel_id, turn)
    terms = _terms(query)
    if not terms:
        return Page(note="Give at least one word to search for.")
    if turn.budget_remaining <= 0:
        return Page(note=BUDGET_SPENT)

    anchor = await _trigger_created_at(turn)
    before = cursor or turn.trigger_post_id
    matched: List[Record] = []
    scanned = 0
    oldest_seen = before

    for _ in range(POLICY.search_scan_pages):
        payload = await mattermost_client.get_channel_posts(turn.channel_id, before=before, per_page=POLICY.page_size)
        if payload is None:
            break
        order: List[str] = list(payload.get("order") or [])
        posts: Dict[str, Any] = payload.get("posts") or {}
        if not order:
            oldest_seen = ""
            break
        scanned += len(order)
        candidates = [posts[pid] for pid in order if pid in posts]
        hits = [
            post for post in candidates if all(term in str(post.get("message") or "").casefold() for term in terms)
        ]
        matched.extend(_fresh(await to_records(hits, access, turn), turn, anchor))
        before = order[-1]
        oldest_seen = before
        if len(order) < POLICY.page_size:
            oldest_seen = ""
            break
        if len(matched) >= POLICY.page_size:
            break

    matched.sort(key=lambda record: record.created_at)
    kept, note = _take(matched, turn, POLICY.page_size)
    turn.remember(kept)
    coverage = f"Searched the {scanned} message(s) before this one in {access.label}."
    if oldest_seen:
        coverage += " Older messages were NOT searched — say so if it matters."
    else:
        coverage += " That reached the start of the conversation."
    logger.info(
        "discussion_search",
        session_id=turn.session_id,
        channel_id=turn.channel_id,
        terms=terms,
        scanned=scanned,
        matched=len(kept),
    )
    return Page(
        records=kept,
        cursor=oldest_seen or None,
        scanned=scanned,
        note=(coverage + (" " + note if note else "")).strip(),
    )


__all__ = [
    "BUDGET_SPENT",
    "REFUSED_CHANNEL",
    "REFUSED_DESTINATION",
    "RetrievalRefused",
    "channel_messages",
    "message",
    "search",
    "thread_messages",
]
