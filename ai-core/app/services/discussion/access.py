"""Who may read what, and where it may be repeated.

Two separate questions, both answered in code and never by a prompt:

1. **May the requester read this channel?** The bot's own access is not an
   answer. The bot is a member of channels many of the people talking to it
   are not, so every retrieval outside the triggering channel is checked
   against the REQUESTER's membership, read from Mattermost at the time of
   the read.

2. **May what is read be repeated where the reply will land?** A person can be
   in a private channel and still not be allowed to have its contents pasted
   into a public one. Access to the source is necessary and not sufficient;
   the destination is the second half of the check.

The disclosure table is deliberately conservative:

============  ==========================================================
Destination   Sources it may repeat
============  ==========================================================
same channel  itself, always
D (direct)    anything the requester may read — nobody else is present
G, P          itself, and public channels
O (public)    itself, and public channels
============  ==========================================================

The gap that leaves — quoting one private channel inside a different private
channel, where the audiences are not the same people — is refused in this
increment rather than approximated. Comparing two member lists is the only
correct test and it is not a cheap one; refusing is the honest default.
"""

from dataclasses import (
    dataclass,
    replace,
)
from typing import Optional

from app.core.logging import logger
from app.services.discussion.context import (
    DiscussionTurn,
    require_turn,
)
from app.services.mattermost import mattermost_client

# Channel types whose audience is exactly the people in the room.
PRIVATE_TYPES = frozenset({"D", "G", "P"})
PUBLIC_TYPE = "O"


@dataclass(frozen=True)
class ChannelAccess:
    """The verdict on one channel, and what is known about it.

    Attributes:
        channel_id: The channel.
        channel_type: O, P, D or G. Empty when the channel could not be read.
        name: URL slug, for permalinks.
        display_name: Human name, for citations.
        team_id: Owning team, empty for direct and group messages.
        allowed: Whether this turn may read it AND repeat it where the reply lands.
        reason: Machine-readable cause when it may not.
    """

    channel_id: str
    channel_type: str = ""
    name: str = ""
    display_name: str = ""
    team_id: str = ""
    allowed: bool = False
    reason: str = ""

    @property
    def label(self) -> str:
        """A short name for citations.

        Returns:
            str: ``#channel-name`` for channels, a plain word for DMs.
        """
        if self.channel_type == "D":
            return "this direct message"
        if self.channel_type == "G":
            return "this group message"
        return f"#{self.name}" if self.name else "this channel"


def may_disclose(source_type: str, source_id: str, dest_type: str, dest_id: str) -> bool:
    """Whether content from one channel may be repeated in another.

    Args:
        source_type: Type of the channel the content came from.
        source_id: Its id.
        dest_type: Type of the channel the reply will be posted in.
        dest_id: Its id.

    Returns:
        bool: True when repeating it widens the audience for nobody.
    """
    if source_id and source_id == dest_id:
        return True
    if dest_type == "D":
        # The reply is read by one person: the requester, who already had to
        # pass the membership check to get here.
        return True
    if source_type == PUBLIC_TYPE:
        # Public content, going somewhere no wider than public.
        return True
    return False


async def resolve_channel(channel_id: str, turn: Optional[DiscussionTurn] = None) -> ChannelAccess:
    """Decide whether this turn may read a channel and quote it into its reply.

    Args:
        channel_id: The channel to check.
        turn: The turn context; the bound one by default.

    Returns:
        ChannelAccess: The verdict, cached for the turn.
    """
    turn = turn or require_turn()
    cached = turn.channels.get(channel_id)
    if cached is not None:
        return cached

    verdict = await _decide(channel_id, turn)
    turn.channels[channel_id] = verdict
    if not verdict.allowed:
        logger.warning(
            "discussion_channel_refused",
            session_id=turn.session_id,
            channel_id=channel_id,
            reason=verdict.reason,
            requester=turn.requester_user_id,
        )
    return verdict


async def _decide(channel_id: str, turn: DiscussionTurn) -> ChannelAccess:
    """Run the two checks for one channel.

    Args:
        channel_id: The channel to check.
        turn: The turn context.

    Returns:
        ChannelAccess: The verdict.
    """
    channel = await mattermost_client.get_channel(channel_id)
    if not channel:
        return ChannelAccess(channel_id=channel_id, reason="channel_unreadable")

    channel_type = str(channel.get("type") or "")
    access = ChannelAccess(
        channel_id=channel_id,
        channel_type=channel_type,
        name=str(channel.get("name") or ""),
        display_name=str(channel.get("display_name") or ""),
        team_id=str(channel.get("team_id") or ""),
    )

    membership = await mattermost_client.channel_member(channel_id, turn.requester_user_id)
    if not membership:
        return replace(access, reason="requester_not_a_member")

    if not may_disclose(channel_type, channel_id, turn.channel_type, turn.channel_id):
        return replace(access, reason="destination_more_public_than_source")

    return replace(access, allowed=True)


__all__ = ["ChannelAccess", "PRIVATE_TYPES", "PUBLIC_TYPE", "may_disclose", "resolve_channel"]
