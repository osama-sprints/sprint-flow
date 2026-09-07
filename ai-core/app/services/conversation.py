"""Transport-agnostic handling of an inbound Mattermost message.

SprintFlow receives messages over two different transports:

* **Outgoing webhooks** — public channels only, hard 30s response ceiling.
* **WebSocket events** — everything else, notably direct messages.

Both must behave identically once a message is in hand: same loop protection,
same text normalisation, same identity resolution, same agent invocation, same
reply path. That shared behaviour lives here so the two transports cannot
drift apart; each transport module is left with nothing but its own wire format.
"""

from typing import List

from pydantic import (
    BaseModel,
    Field,
)

from app.core.config import settings
from app.core.logging import logger
from app.core.requester import current_requester
from app.schemas.chat import Message
from app.schemas.rich_media import (
    RICH_MEDIA_POST_TYPE,
    ReplyEnvelope,
)
from app.services import (
    attachments,
    rich_media,
)
from app.services.agent import agent
from app.services.identity import resolve_requester
from app.services.mattermost import mattermost_client

# Longest message text a turn reads. The default is above Mattermost's own
# post limit, so in practice nothing is ever cut; if a transport does deliver
# more, the person is told what was read rather than losing the tail silently.
MAX_INPUT_CHARS = settings.MESSAGE_MAX_INPUT_CHARS

FALLBACK_REPLY = "Sorry — I hit an error while working on that. Please try again in a moment."


class _NothingToAnswer(Exception):
    """The message held nothing readable; the notices alone are the reply."""


# Mattermost channel types where a threaded reply is the natural shape. In a
# direct ("D") or group ("G") message there is no surrounding traffic to be
# separated from, so a thread just buries the answer one click deep.
_THREADED_CHANNEL_TYPES = frozenset({"O", "P"})


class IncomingMessage(BaseModel):
    """One inbound Mattermost message, normalised across both transports."""

    channel_id: str = Field(..., description="Channel the message arrived in")
    post_id: str = Field(default="", description="The triggering post")
    text: str = Field(..., description="Cleaned message body")
    user_id: str = Field(default="", description="Author's Mattermost user id — scopes long-term memory")
    user_name: str = Field(default="")
    channel_type: str = Field(default="O", description="O public, P private, D direct, G group")
    root_id: str = Field(default="", description="Set when the trigger is already inside a thread")
    source: str = Field(default="unknown", description="Transport label for logs")
    file_ids: List[str] = Field(default_factory=list, description="Files attached to the triggering post")

    @property
    def threads_by_default(self) -> bool:
        """Whether a fresh reply in this channel should open a thread.

        Returns:
            bool: True for public and private channels, False for DMs and GMs.
        """
        return self.channel_type in _THREADED_CHANNEL_TYPES

    @property
    def session_id(self) -> str:
        """The LangGraph thread id — one isolated conversation per key.

        This deliberately mirrors `_deliver()`: where a reply lands IS the
        conversation it belongs to, so the two must agree or a follow-up would
        resume a different history than the one it appears under.

        Keying on the bare channel id (the previous behaviour) merged every
        thread in a channel into one shared history, so an answer could quote
        facts stated in an unrelated conversation.

        Returns:
            str: Stable key for this conversation.
        """
        if self.root_id:
            # Already in a thread — that thread is the conversation.
            return f"{self.channel_id}:{self.root_id}"

        if self.threads_by_default:
            # Our reply will open a thread rooted at this post, so the
            # follow-up (which arrives with root_id == post_id) resolves to
            # this very same key. Continuity without a lookup.
            return f"{self.channel_id}:{self.post_id}"

        # DM or group message: one continuous conversation, no threads.
        return self.channel_id


def clean_text(text: str, trigger_word: str = "") -> str:
    """Strip the trigger word and surrounding whitespace.

    Nothing is cut here: the length ceiling is applied in ``answer_and_reply``,
    where the person can be told about it.

    Args:
        text: Raw message text.
        trigger_word: Trigger word that fired the hook, if any.

    Returns:
        str: Text ready to hand to the agent.
    """
    cleaned = text.strip()
    if trigger_word and cleaned.lower().startswith(trigger_word.lower()):
        cleaned = cleaned[len(trigger_word) :].strip()
    return cleaned


def with_notices(reply: str, notices: List[str]) -> str:
    """Prefix a reply with what the person should know about their input.

    Args:
        reply: The answer.
        notices: Lines about skipped files, caps that applied, or cut text.

    Returns:
        str: The reply, preceded by a quoted notice block when there is one.
    """
    if not notices:
        return reply
    block = "\n".join(f"> ⚠️ {line}" for line in notices)
    return f"{block}\n\n{reply}" if reply else block


async def is_own_post(user_id: str, user_name: str = "") -> bool:
    """Return True when the post was made by this bot.

    This is the loop guard, and it is required on BOTH transports:

    * over REST, creating a post re-triggers outgoing webhooks because
      Mattermost sets TriggerWebhooks unconditionally on that path;
    * over the WebSocket, the bot receives a `posted` event for its own reply
      like any other client.

    Without this check the bot would answer itself indefinitely.

    Args:
        user_id: Author's user id.
        user_name: Author's username, when the transport provides it.

    Returns:
        bool: True if the post came from the bot account.
    """
    if user_name and user_name == settings.MATTERMOST_BOT_USERNAME:
        return True

    bot_user_id = await mattermost_client.get_bot_user_id()
    return bool(bot_user_id) and user_id == bot_user_id


async def answer_and_reply(message: IncomingMessage) -> None:
    """Run the agent for one message and post the answer back to Mattermost.

    Never raises. Both callers run this detached from the request that produced
    it, so an exception escaping here would be an unhandled task error with the
    person left waiting for a reply that never comes.

    Args:
        message: The normalised inbound message.
    """
    channel_id = message.channel_id
    session_id = message.session_id
    source = message.source
    logger.info(
        "mattermost_agent_turn_started",
        channel_id=channel_id,
        session_id=session_id,
        user_name=message.user_name,
        channel_type=message.channel_type,
        source=source,
    )

    # Identity is resolved from the event's user id and STORED data, then bound
    # before the graph runs so the supervisor and every tool can read it. It is
    # never exposed as a tool argument the model could fill in itself.
    requester = await resolve_requester(
        mattermost_user_id=message.user_id,
        username=message.user_name,
        channel_id=channel_id,
        channel_type=message.channel_type,
    )
    current_requester.set(requester)

    # Visual output is staged against THIS turn. A resumed conversation opens a
    # new turn id, so artifacts staged before an interrupt can never be
    # published a second time by the turn that answers it.
    turn = rich_media.begin_turn(
        channel_id=channel_id,
        root_id=message.root_id,
        requester_user_id=requester.user_id,
        mattermost_user_id=message.user_id,
        session_id=session_id,
    )
    envelope = ReplyEnvelope()

    # What the person sent, made explicit: text past the ceiling is reported
    # rather than dropped, and every attached file is fetched, checked and
    # read before the model sees the message.
    notices: List[str] = []
    text = message.text
    if len(text) > MAX_INPUT_CHARS:
        notices.append(
            f"Your message was {len(text):,} characters long; I read the first {MAX_INPUT_CHARS:,}. "
            "Attach the rest as a file if you need me to read all of it."
        )
        text = text[:MAX_INPUT_CHARS]
    turn_files = await attachments.ingest(
        message.file_ids,
        post_id=message.post_id,
        channel_id=channel_id,
        root_id=message.root_id,
        session_id=session_id,
        turn_id=turn.turn_id,
        mattermost_user_id=message.user_id,
        requester_user_id=requester.user_id,
    )
    attachments.bind(turn_files)
    notices.extend(turn_files.notices)
    prompt_text = attachments.state_text(text, turn_files)

    try:
        if not prompt_text:
            # Only refused files arrived: the notices are the whole answer.
            raise _NothingToAnswer()
        result = await agent.get_response(
            [Message(role="user", content=prompt_text)],
            session_id=session_id,
            # Scopes mem0 long-term memory to the person. This is the layer
            # that makes per-thread isolation safe: durable facts about someone
            # follow them across threads, while transient thread context does
            # not leak between them.
            user_id=message.user_id or None,
            username=message.user_name or None,
        )
        # get_response returns the WHOLE accumulated thread from the checkpointer,
        # not just this turn's answer, so take the last assistant message only.
        # Joining them all would re-post the entire conversation every time.
        reply = next(
            (m.content.strip() for m in reversed(result) if m.role == "assistant" and m.content.strip()),
            "",
        )
        if not reply:
            logger.warning("mattermost_agent_returned_empty", session_id=session_id, source=source)
            reply = FALLBACK_REPLY
        envelope = await rich_media.collect(turn.turn_id)
    except _NothingToAnswer:
        reply = ""
    except Exception as e:
        logger.exception("mattermost_agent_turn_failed", session_id=session_id, source=source, error=str(e))
        reply = FALLBACK_REPLY
    finally:
        current_requester.set(None)
        rich_media.end_turn()
        attachments.clear()

    await _deliver(message, with_notices(reply, notices), envelope)
    logger.info(
        "mattermost_agent_turn_completed",
        session_id=session_id,
        source=source,
        turn_id=turn.turn_id,
        artifact_count=len(envelope.artifacts),
    )


async def _deliver(message: IncomingMessage, reply: str, envelope: ReplyEnvelope | None = None) -> None:
    """Post the reply with the threading shape that suits the channel.

    Three cases, in priority order:

    1. The person is already inside a thread — stay in it, whatever the channel
       type. Replying flat here would drop the answer outside the conversation
       they deliberately opened.
    2. Public or private channel — open a thread under the trigger, so the
       exchange does not interleave with unrelated channel traffic.
    3. Direct or group message — reply as a plain message. A 1-on-1 has nothing
       to disambiguate from, and threading every answer reads as clutter.

    Publication happens exactly once, here. Tools stage artifacts; they never
    post, so there is no path by which one turn produces two replies.

    Args:
        message: The message being answered.
        reply: The agent's answer. Always a complete answer on its own.
        envelope: Staged artifacts, when the turn produced any.
    """
    rich = envelope is not None and not envelope.is_empty()

    # Reconciliation, not a lease: creating the post and recording that we
    # created it are two steps, and a crash between them would publish the reply
    # twice. The turn id is stamped into the post's props, so a retry finds the
    # published post and adopts it instead of posting again.
    if rich and envelope is not None:
        existing = await rich_media.already_published(envelope.turn_id)
        if existing:
            logger.info(
                "reply_already_published",
                turn_id=envelope.turn_id,
                post_id=existing,
                channel_id=message.channel_id,
            )
            return
    post_type = RICH_MEDIA_POST_TYPE if rich else None
    props = envelope.to_props() if rich and envelope else None
    file_ids = list(envelope.file_ids) if rich and envelope else None

    posted = await _publish(message, reply, post_type, props, file_ids)

    if posted is None and rich:
        # The artifacts are decoration; the answer is not. A props map the
        # server rejects must never cost the person their reply, so fall back to
        # the same text with nothing attached.
        logger.warning(
            "rich_reply_rejected_falling_back_to_text",
            channel_id=message.channel_id,
            artifact_count=len(envelope.artifacts) if envelope else 0,
        )
        posted = await _publish(message, reply, None, None, None)

    if posted is None:
        logger.error("reply_delivery_failed", channel_id=message.channel_id, source=message.source)
        return

    if rich and envelope is not None:
        await rich_media.record_publication(envelope.turn_id, posted["id"], file_ids=list(envelope.file_ids))


async def _publish(
    message: IncomingMessage,
    reply: str,
    post_type: str | None,
    props: dict | None,
    file_ids: list[str] | None,
) -> dict | None:
    """Create the reply post with the right threading for this message.

    Args:
        message: The message being answered.
        reply: The answer text.
        post_type: Custom post type, when the reply carries artifacts.
        props: Post props, when the reply carries artifacts.
        file_ids: Native attachments, when the reply carries any.

    Returns:
        dict | None: The created post, or None when Mattermost refused it.
    """
    if message.root_id:
        return await mattermost_client.create_post(
            message.channel_id,
            reply,
            root_id=message.root_id,
            post_type=post_type,
            props=props,
            file_ids=file_ids,
        )

    if message.threads_by_default:
        # reply_to_post resolves the true thread root first: Mattermost rejects
        # a root_id that is itself a reply, and swallows the error, so the
        # person would see no answer at all.
        return await mattermost_client.reply_to_post(
            message.channel_id,
            reply,
            message.post_id,
            post_type=post_type,
            props=props,
            file_ids=file_ids,
        )

    return await mattermost_client.create_post(
        message.channel_id,
        reply,
        post_type=post_type,
        props=props,
        file_ids=file_ids,
    )
