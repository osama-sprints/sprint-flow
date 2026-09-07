"""The trusted facts about the request retrieval is allowed to use.

Every identifier that decides WHAT may be read and WHO it is read for comes
from the Mattermost event: the requester's user id, the channel and its type,
the triggering post, the thread it sits in. The model supplies none of them.
It can ask for an earlier page or name a post id, and those arguments are
checked against this context; it can never say who it is or where the reply
will land.

The context also carries the turn's retrieval budget and its seen-set, so
"ten more messages" means ten the turn has not already read, and the total
stays bounded however many times retrieval runs.
"""

from contextvars import ContextVar
from dataclasses import (
    dataclass,
    field,
)
from typing import (
    Any,
    Dict,
    List,
    Optional,
)

from app.core.logging import logger
from app.services.discussion.policy import POLICY


@dataclass
class DiscussionTurn:
    """One turn's request context, budget and memory of what it has read.

    Attributes:
        requester_user_id: Mattermost user id of the person asking. Server-derived.
        requester_username: Their handle, for the sub-agent's framing.
        channel_id: Where the message arrived — the default retrieval scope.
        channel_type: O public, P private, D direct, G group.
        trigger_post_id: The post that triggered this turn. Retrieval anchors here.
        root_id: The thread the trigger sits in, empty at top level.
        session_id: The conversation key, for logs.
        trigger_created_at: The trigger's Mattermost timestamp (ms), read lazily.
        seen: Post ids already handed to the sub-agent this turn.
        records: Every record returned this turn, by post id — the only source
            of authors, timestamps and permalinks the digest may cite.
        records_used: How much of the turn's record budget is spent.
        runs: How many times retrieval has run this turn.
        primed: Thread messages read automatically before the turn started.
        channels: Channel metadata resolved this turn, by channel id.
        users: User profiles resolved this turn, by user id.
        teams: Team names resolved this turn, by team id.
    """

    requester_user_id: str
    requester_username: str = ""
    channel_id: str = ""
    channel_type: str = ""
    trigger_post_id: str = ""
    root_id: str = ""
    session_id: str = ""
    trigger_created_at: Optional[int] = None
    seen: set = field(default_factory=set)
    records: Dict[str, Any] = field(default_factory=dict)
    records_used: int = 0
    runs: int = 0
    primed: List[Any] = field(default_factory=list)
    channels: Dict[str, Any] = field(default_factory=dict)
    users: Dict[str, Any] = field(default_factory=dict)
    teams: Dict[str, str] = field(default_factory=dict)

    def begin_run(self) -> None:
        """Start a fresh retrieval run.

        The seen-set is per RUN, not per turn. It exists so one run does not
        hand itself the same message twice; carried across runs it becomes a
        blindfold, because the second run holds none of the first run's pages
        and would be told there was nothing to read. The BUDGET is what spans
        the turn, and it is what actually bounds the work.
        """
        self.runs += 1
        self.seen = set()

    @property
    def budget_remaining(self) -> int:
        """Messages this turn may still take back from retrieval.

        Returns:
            int: Remaining record budget, never negative.
        """
        return max(0, POLICY.max_records_per_turn - self.records_used)

    def remember(self, records: List[Any], *, spend: bool = True) -> None:
        """Record what was returned, so it can be cited and is not returned twice.

        ``spend`` is False for messages read BEFORE the turn started — the
        thread context shown to the model automatically. Those must stay
        readable: marking them seen would make the retrieval sub-agent's first
        look at the thread come back empty, which is exactly the thread it was
        asked about.

        Args:
            records: The records to remember.
            spend: Whether they count against the turn's budget and dedupe.
        """
        for record in records:
            self.records[record.post_id] = record
            if spend:
                self.seen.add(record.post_id)
        if spend:
            self.records_used += len(records)


current_discussion: ContextVar[Optional[DiscussionTurn]] = ContextVar("current_discussion", default=None)


def begin_turn(
    *,
    requester_user_id: str,
    requester_username: str = "",
    channel_id: str = "",
    channel_type: str = "",
    trigger_post_id: str = "",
    root_id: str = "",
    session_id: str = "",
) -> DiscussionTurn:
    """Bind the retrieval context for one turn.

    Args:
        requester_user_id: Mattermost user id from the event.
        requester_username: Handle from the event.
        channel_id: Channel the message arrived in.
        channel_type: O, P, D or G.
        trigger_post_id: The triggering post.
        root_id: Thread root, when the trigger is inside a thread.
        session_id: Conversation key, for logs.

    Returns:
        DiscussionTurn: The bound context.
    """
    turn = DiscussionTurn(
        requester_user_id=requester_user_id,
        requester_username=requester_username,
        channel_id=channel_id,
        channel_type=channel_type,
        trigger_post_id=trigger_post_id,
        root_id=root_id,
        session_id=session_id,
    )
    current_discussion.set(turn)
    return turn


def end_turn() -> None:
    """Release the turn's retrieval context."""
    current_discussion.set(None)


def require_turn() -> DiscussionTurn:
    """Return the bound context, or refuse.

    Returns:
        DiscussionTurn: The current turn's context.

    Raises:
        NoDiscussionContext: When retrieval is attempted outside a turn.
    """
    turn = current_discussion.get()
    if turn is None:
        logger.warning("discussion_retrieval_without_context")
        raise NoDiscussionContext()
    return turn


class NoDiscussionContext(Exception):
    """Retrieval was attempted with no request context bound — never authorised."""


__all__ = [
    "DiscussionTurn",
    "NoDiscussionContext",
    "begin_turn",
    "current_discussion",
    "end_turn",
    "require_turn",
]
