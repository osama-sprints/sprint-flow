"""Mattermost WebSocket listener.

Outgoing webhooks fire only in public channels — Mattermost blocks them for
direct messages, group messages and private channels server-side. That makes
them useless for anything 1-on-1, which is exactly what the Manager agent needs
for standups and check-ins.

This listener holds a persistent authenticated WebSocket to Mattermost as the
bot, watches for `posted` events, and hands direct messages to the same
conversation logic the webhook path uses.

Division of labour, and why it matters:

    public channels  ->  outgoing webhook   (app/api/v1/mattermost.py)
    direct messages  ->  this listener

The WebSocket receives events for EVERY channel the bot can see, including
public ones. If the channel-type filter were widened to include "O", every
public-channel message would be answered twice — once by the webhook and once
by this listener. `MATTERMOST_WS_CHANNEL_TYPES` therefore defaults to "D" only.
"""

import asyncio
import json
import re
from typing import (
    Any,
    Dict,
    Optional,
    Set,
)

from websockets.asyncio.client import connect

from app.core.cache import (
    cache_key,
    cache_service,
)
from app.core.config import settings
from app.core.logging import logger
from app.services.conversation import (
    IncomingMessage,
    answer_and_reply,
    clean_text,
    is_own_post,
)
from app.services.mattermost import mattermost_client

# Reconnect backoff: double on each consecutive failure, capped. Reset to the
# floor as soon as a connection authenticates.
_BACKOFF_MIN_SECONDS = 1.0
_BACKOFF_MAX_SECONDS = 60.0

# Channels where every message is meant for the bot: there is nobody else in the
# conversation, so no mention is required.
_DIRECT_CHANNEL_TYPES = frozenset({"D", "G"})

# Channel types the Mattermost outgoing webhook can fire in. Only public
# channels — everywhere else, this listener is the sole transport and must not
# defer to the webhook.
_WEBHOOK_OWNED_CHANNEL_TYPES = frozenset({"O"})


def _mention_pattern(username: str) -> re.Pattern[str]:
    """Build a regex matching an @mention of `username`.

    The lookarounds stop `@bot` from matching inside `@bot-staging` or an email
    address, since Mattermost usernames may contain dots, dashes and
    underscores.

    Args:
        username: The bot's username, without the leading @.

    Returns:
        re.Pattern: Compiled, case-insensitive mention matcher.
    """
    return re.compile(
        rf"(?<![A-Za-z0-9._@-])@{re.escape(username)}(?![A-Za-z0-9._-])",
        re.IGNORECASE,
    )


_MENTION_RE = _mention_pattern(settings.MATTERMOST_BOT_USERNAME)


def _starts_with_trigger_word(message: str) -> bool:
    """Return True when the outgoing webhook will also fire for this message.

    Mattermost compares its trigger words against the FIRST WORD of the message
    only. Mirroring that exactly is what keeps the two transports from both
    answering the same post.

    Args:
        message: Raw message text.

    Returns:
        bool: True if the first word is one of the configured trigger words.
    """
    words = message.strip().split()
    if not words:
        return False
    first = words[0].casefold()
    return any(first == trigger.casefold() for trigger in settings.MATTERMOST_TRIGGER_WORDS)


class MattermostWebSocketListener:
    """Maintains a persistent WebSocket to Mattermost and dispatches posts."""

    def __init__(self) -> None:
        """Initialize the listener without connecting."""
        self._task: Optional[asyncio.Task] = None
        self._handlers: Set[asyncio.Task] = set()
        self._connected: bool = False
        self._events_seen: int = 0
        self._messages_handled: int = 0

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    @property
    def websocket_url(self) -> str:
        """Return the WebSocket URL derived from the configured base URL.

        Returns:
            str: e.g. ``ws://mattermost:8065/api/v4/websocket``.
        """
        base = settings.MATTERMOST_URL.rstrip("/")
        if base.startswith("https://"):
            base = "wss://" + base[len("https://") :]
        elif base.startswith("http://"):
            base = "ws://" + base[len("http://") :]
        return f"{base}/api/v4/websocket"

    async def start(self) -> None:
        """Start the background connection task.

        Returns without doing anything when the listener is disabled or no bot
        token is configured yet — the stack must still boot before
        `scripts/bootstrap_mattermost.sh` has issued a token.
        """
        if not settings.MATTERMOST_WS_ENABLED:
            logger.info("mattermost_ws_disabled")
            return

        if not settings.MATTERMOST_BOT_TOKEN:
            logger.warning("mattermost_ws_not_started_no_token")
            return

        if self._task is not None and not self._task.done():
            logger.debug("mattermost_ws_already_running")
            return

        self._task = asyncio.create_task(self._run_forever(), name="mattermost-ws-listener")
        logger.info(
            "mattermost_ws_starting",
            url=self.websocket_url,
            channel_types=settings.MATTERMOST_WS_CHANNEL_TYPES,
        )

    async def stop(self) -> None:
        """Cancel the connection task and any in-flight agent turns."""
        if self._task is not None:
            self._task.cancel()
            # Swallow the CancelledError raised into the task we just cancelled;
            # this is an orderly shutdown, not a failure.
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

        if self._handlers:
            logger.info("mattermost_ws_cancelling_inflight", count=len(self._handlers))
            for handler in list(self._handlers):
                handler.cancel()
            await asyncio.gather(*self._handlers, return_exceptions=True)
            self._handlers.clear()

        self._connected = False
        logger.info("mattermost_ws_stopped")

    def status(self) -> Dict[str, Any]:
        """Return a snapshot of listener state for the health endpoint.

        Returns:
            dict: Connection state and counters.
        """
        return {
            "enabled": settings.MATTERMOST_WS_ENABLED,
            "running": self._task is not None and not self._task.done(),
            "connected": self._connected,
            "url": self.websocket_url,
            "channel_types": settings.MATTERMOST_WS_CHANNEL_TYPES,
            "events_seen": self._events_seen,
            "messages_handled": self._messages_handled,
            "inflight_turns": len(self._handlers),
        }

    # ------------------------------------------------------------------
    # Connection loop
    # ------------------------------------------------------------------

    async def _run_forever(self) -> None:
        """Reconnect forever, backing off after consecutive failures."""
        backoff = _BACKOFF_MIN_SECONDS
        while True:
            try:
                await self._consume()
                # A clean close is normal (server restart, deploy): reconnect
                # promptly rather than treating it as a failure.
                backoff = _BACKOFF_MIN_SECONDS
                logger.info("mattermost_ws_disconnected_reconnecting")
            except asyncio.CancelledError:
                logger.info("mattermost_ws_listener_cancelled")
                raise
            except Exception as e:
                logger.exception(
                    "mattermost_ws_connection_failed",
                    error=str(e),
                    retry_in_seconds=backoff,
                )
            finally:
                self._connected = False

            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, _BACKOFF_MAX_SECONDS)

    async def _consume(self) -> None:
        """Open one connection, authenticate, and process events until it closes.

        Raises:
            Exception: Any connection or protocol error, handled by the caller.
        """
        async with connect(
            self.websocket_url,
            open_timeout=15,
            close_timeout=5,
            # Mattermost is happy with protocol-level keepalives; these also
            # surface a dead connection instead of hanging forever.
            ping_interval=20,
            ping_timeout=20,
            max_size=2**22,
        ) as websocket:
            # Mattermost authenticates a Personal Access Token through an
            # in-band challenge rather than an HTTP header on the upgrade.
            await websocket.send(
                json.dumps(
                    {
                        "seq": 1,
                        "action": "authentication_challenge",
                        "data": {"token": settings.MATTERMOST_BOT_TOKEN},
                    }
                )
            )

            async for raw in websocket:
                try:
                    message = json.loads(raw)
                except (TypeError, ValueError):
                    logger.warning("mattermost_ws_undecodable_frame")
                    continue

                await self._handle_message(message)

    # ------------------------------------------------------------------
    # Event handling
    # ------------------------------------------------------------------

    async def _handle_message(self, message: Dict[str, Any]) -> None:
        """Route one decoded WebSocket frame.

        Args:
            message: Decoded frame.
        """
        event = message.get("event")

        if event == "hello":
            self._connected = True
            logger.info("mattermost_ws_authenticated")
            return

        # A failed authentication_challenge comes back as a status reply rather
        # than an event, and the server then closes the socket.
        if event is None and message.get("status") == "FAIL":
            logger.error("mattermost_ws_authentication_failed", detail=str(message.get("error")))
            return

        if event == "new_user":
            self._events_seen += 1
            await self._handle_new_user(message.get("data") or {})
            return

        if event != "posted":
            return

        self._events_seen += 1
        await self._handle_posted(message.get("data") or {})

    async def _handle_new_user(self, data: Dict[str, Any]) -> None:
        """Add a freshly registered account to the default team.

        Mattermost has no configuration that joins a new user to a named team.
        `ExperimentalPrimaryTeam` only hides UI, and an open-invite team still
        needs a click. The `new_user` event, however, is broadcast with an empty
        scope — every authenticated connection receives it, including the bot —
        so onboarding is done here instead.

        Channels are not handled here on purpose:
        `MM_TEAMSETTINGS_EXPERIMENTALDEFAULTCHANNELS` adds them automatically on
        every team join, so adding the team is enough.

        Args:
            data: The event's data object, carrying `user_id`.
        """
        if not settings.MATTERMOST_AUTO_ONBOARD:
            return

        user_id = str(data.get("user_id") or "")
        if not user_id:
            return

        bot_user_id = await mattermost_client.get_bot_user_id()
        if bot_user_id and user_id == bot_user_id:
            return

        team = await mattermost_client.get_team_by_name(settings.MATTERMOST_DEFAULT_TEAM)
        if not team:
            logger.warning(
                "mattermost_onboarding_team_missing",
                team=settings.MATTERMOST_DEFAULT_TEAM,
                user_id=user_id,
            )
            return

        added = await mattermost_client.add_user_to_team(team["id"], user_id)
        from app.services.onboarding import handle_arrival
        await handle_arrival(user_id)
        logger.info(
            "mattermost_user_onboarded",
            user_id=user_id,
            team=settings.MATTERMOST_DEFAULT_TEAM,
            added=added,
        )

        

    async def _bot_is_in_thread(self, root_id: str) -> bool:
        """Return True when the bot already participates in a thread.

        Participation means the bot authored one of the posts, or someone
        mentioned it anywhere in the thread. One REST call, cached — no model
        is consulted, so an unrelated thread costs nothing but a lookup.

        Only positive results are cached: participation never reverts to false,
        whereas a thread the bot has not joined yet may be joined a second
        later, and caching that "no" would silently ignore the mention that
        follows.

        Args:
            root_id: The thread's root post id.

        Returns:
            bool: True if the bot is part of the thread.
        """
        key = cache_key("mm_thread_member", root_id)
        if await cache_service.get(key) is not None:
            return True

        thread = await mattermost_client.get_thread(root_id)
        if not thread:
            # Treat an API failure as "not participating": staying quiet is the
            # safer failure mode for a bot in a shared channel.
            return False

        bot_user_id = await mattermost_client.get_bot_user_id()
        participating = False
        for post in (thread.get("posts") or {}).values():
            if bot_user_id and post.get("user_id") == bot_user_id:
                participating = True
                break
            if _MENTION_RE.search(str(post.get("message") or "")):
                participating = True
                break

        if participating:
            await cache_service.set(key, "1", ttl=settings.MATTERMOST_THREAD_CACHE_TTL)

        return participating

    async def _should_handle(self, channel_type: str, message: str, root_id: str) -> bool:
        """Decide whether this listener should answer a post.

        The rules, in order:

        1. Direct and group messages — always ours; there is no one else in the
           conversation and no webhook can reach them.
        2. Public channel, first word is a webhook trigger word — NOT ours. The
           outgoing webhook is already delivering this message, and answering
           here too would post the reply twice.
        3. The bot is mentioned anywhere in the message — ours. This also picks
           up mentions that are not the first word ("thanks @bot, can you..."),
           which the webhook's first-word matching silently ignores.
        4. Inside a thread the bot already participates in — ours. This is
           thread continuity: once the bot is in a conversation, follow-ups no
           longer need to re-mention it.
        5. Anything else — ignored, with no API call and no model call.

        Args:
            channel_type: Mattermost channel type (O, P, D, G).
            message: Raw message text.
            root_id: The post's root id, empty when not in a thread.

        Returns:
            bool: True when the message should be answered.
        """
        if channel_type in _DIRECT_CHANNEL_TYPES:
            return True

        if channel_type in _WEBHOOK_OWNED_CHANNEL_TYPES and _starts_with_trigger_word(message):
            return False

        if _MENTION_RE.search(message):
            return True

        if root_id:
            return await self._bot_is_in_thread(root_id)

        return False

    async def _handle_posted(self, data: Dict[str, Any]) -> None:
        """Handle a `posted` event, dispatching it when it is ours to answer.

        Args:
            data: The event's data object. Note `post` is a JSON-encoded STRING
                that has to be decoded a second time.
        """
        channel_type = data.get("channel_type", "")
        if channel_type not in settings.MATTERMOST_WS_CHANNEL_TYPES:
            return

        try:
            post: Dict[str, Any] = json.loads(data.get("post") or "{}")
        except (TypeError, ValueError):
            logger.warning("mattermost_ws_undecodable_post")
            return

        # Joins, leaves, header changes and the like.
        if str(post.get("type") or "").startswith("system_"):
            return

        # Anything posted by a bot or an incoming webhook, including our own
        # replies. Checked before the id lookup because it needs no round trip.
        props = post.get("props") or {}
        if props.get("from_bot") == "true" or props.get("from_webhook") == "true":
            return

        user_id = str(post.get("user_id") or "")
        user_name = str(data.get("sender_name") or "").lstrip("@")

        if await is_own_post(user_id, user_name):
            logger.info("mattermost_ws_ignored_own_post", user_name=user_name)
            return

        raw_message = str(post.get("message") or "")
        root_id = str(post.get("root_id") or "")

        # Routing decision comes before any expensive work. Most public-channel
        # chatter is discarded here without an API call, let alone a model call.
        if not await self._should_handle(channel_type, raw_message, root_id):
            return

        # Strip the mention only when it opens the message, so the agent sees
        # the question rather than the addressing. `match` anchors at position
        # zero: a mention of someone else first, or of the bot mid-sentence,
        # is left exactly as written.
        stripped = raw_message.lstrip()
        leading = _MENTION_RE.match(stripped)
        prompt = clean_text(stripped[leading.end() :] if leading else raw_message)
        if not prompt:
            return

        channel_id = str(post.get("channel_id") or "")
        post_id = str(post.get("id") or "")
        if not channel_id:
            return

        self._messages_handled += 1
        logger.info(
            "mattermost_ws_message_received",
            channel_id=channel_id,
            channel_type=channel_type,
            user_name=user_name,
            in_thread=bool(root_id),
            text_length=len(prompt),
        )

        # Dispatched as its own task so a slow agent turn cannot stall the
        # receive loop — otherwise one long answer would block every other
        # conversation and the connection's keepalives with it.
        handler = asyncio.create_task(
            answer_and_reply(
                IncomingMessage(
                    channel_id=channel_id,
                    post_id=post_id,
                    text=prompt,
                    user_id=user_id,
                    user_name=user_name,
                    channel_type=channel_type,
                    # Present when the person replied inside an existing thread;
                    # the reply then stays in that thread even in a DM.
                    root_id=root_id,
                    source="websocket",
                )
            ),
            name=f"mm-ws-turn-{post_id or 'unknown'}",
        )
        # Hold a strong reference until completion; asyncio only keeps weak ones
        # and an unreferenced task can be garbage collected mid-flight.
        self._handlers.add(handler)
        handler.add_done_callback(self._handlers.discard)


mattermost_ws_listener = MattermostWebSocketListener()
