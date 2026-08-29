"""Transport-agnostic handling of an inbound Mattermost message.

SprintFlow receives messages over two different transports:

* **Outgoing webhooks** — public channels only, hard 30s response ceiling.
* **WebSocket events** — everything else, notably direct messages.

Both must behave identically once a message is in hand: same loop protection,
same text normalisation, same agent invocation, same reply path. That shared
behaviour lives here so the two transports cannot drift apart; each transport
module is left with nothing but its own wire format.
"""

from pydantic import (
    BaseModel,
    Field,
)

from app.services.agent import agent
from app.core.config import settings
from app.core.langgraph.tools.mattermost_admin import current_requester
from app.core.logging import logger
from app.schemas.chat import Message
from app.services.mattermost import mattermost_client

# app/schemas/chat.py caps Message.content at 3000 characters. Mattermost posts
# can be far longer, so trim rather than let validation reject the turn.
MAX_INPUT_CHARS = 3000

FALLBACK_REPLY = "Sorry — I hit an error while working on that. Please try again in a moment."

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
    """Strip the trigger word and clamp the message to the schema's limit.

    Args:
        text: Raw message text.
        trigger_word: Trigger word that fired the hook, if any.

    Returns:
        str: Text ready to hand to the agent.
    """
    cleaned = text.strip()
    if trigger_word and cleaned.lower().startswith(trigger_word.lower()):
        cleaned = cleaned[len(trigger_word) :].strip()
    return cleaned[:MAX_INPUT_CHARS]


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


async def _resolve_requester(message: IncomingMessage) -> dict:
    """Build the authorisation context for this turn.

    The requester's identity is taken from the Mattermost user id on the event
    and their email is read back from the server — never from the message text,
    which the sender controls.

    The lookup is skipped outside direct messages: admin tools refuse anything
    that is not a DM anyway, so a public channel never pays for it.

    Args:
        message: The inbound message.

    Returns:
        dict: Requester context for the admin tools' ContextVar.
    """
    context = {
        "user_id": message.user_id,
        "user_name": message.user_name,
        "channel_type": message.channel_type,
        "email": None,
        "is_admin": False,
    }

    if message.channel_type != "D" or not message.user_id or not settings.ADMIN_EMAILS:
        return context

    user = await mattermost_client.get_user(message.user_id)
    email = (user or {}).get("email", "").strip().lower()
    context["email"] = email or None
    context["is_admin"] = bool(email) and email in settings.ADMIN_EMAILS
    return context


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

    # Bound before the graph runs so admin tools can read it, and never exposed
    # as a tool argument the model could fill in itself.
    current_requester.set(await _resolve_requester(message))

    try:
        result = await agent.get_response(
            [Message(role="user", content=message.text)],
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
    except Exception as e:
        logger.exception("mattermost_agent_turn_failed", session_id=session_id, source=source, error=str(e))
        reply = FALLBACK_REPLY

    await _deliver(message, reply)
    logger.info("mattermost_agent_turn_completed", session_id=session_id, source=source)


async def _deliver(message: IncomingMessage, reply: str) -> None:
    """Post the reply with the threading shape that suits the channel.

    Three cases, in priority order:

    1. The person is already inside a thread — stay in it, whatever the channel
       type. Replying flat here would drop the answer outside the conversation
       they deliberately opened.
    2. Public or private channel — open a thread under the trigger, so the
       exchange does not interleave with unrelated channel traffic.
    3. Direct or group message — reply as a plain message. A 1-on-1 has nothing
       to disambiguate from, and threading every answer reads as clutter.

    Args:
        message: The message being answered.
        reply: The agent's answer.
    """
    if message.root_id:
        await mattermost_client.create_post(message.channel_id, reply, root_id=message.root_id)
        return

    if message.threads_by_default:
        # reply_to_post resolves the true thread root first: Mattermost rejects
        # a root_id that is itself a reply, and swallows the error, so the
        # person would see no answer at all.
        await mattermost_client.reply_to_post(message.channel_id, reply, message.post_id)
        return

    await mattermost_client.create_post(message.channel_id, reply)
