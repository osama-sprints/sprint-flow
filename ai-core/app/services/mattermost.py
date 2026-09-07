"""Mattermost REST API client.

This is the *proactive* half of the SprintFlow protocol. Mattermost pushes user
messages to us through an outgoing webhook, but the reply is not sent in that
webhook's HTTP response — outgoing webhooks are hard-capped at a 30s round trip
by the Mattermost HTTP client, which is shorter than a typical agent turn. So we
acknowledge the webhook immediately and post the real answer back through this
client, authenticated with the bot account's Personal Access Token.

The same client is what any future scheduled agent (standups, nudges) will use
to open conversations on its own.
"""

from typing import (
    Any,
    Dict,
    List,
    Optional,
)

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from app.core.config import settings
from app.core.logging import logger

# Retried only on transport errors and 5xx — a 4xx means the request itself is
# wrong (bad token, missing channel) and will never succeed on retry.
_RETRYABLE = (httpx.TransportError, httpx.HTTPStatusError)


class FileTooLarge(Exception):
    """A download exceeded the caller's byte ceiling and was abandoned."""


class MattermostClient:
    """Async client for the Mattermost v4 REST API, authenticated as a bot."""

    def __init__(self) -> None:
        """Initialize the client without opening any connection."""
        self._client: Optional[httpx.AsyncClient] = None
        self._bot_user_id: Optional[str] = None

    def _get_client(self) -> httpx.AsyncClient:
        """Return the shared HTTP client, creating it on first use.

        Returns:
            httpx.AsyncClient: Client pre-configured with the bot token.
        """
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=f"{settings.MATTERMOST_URL.rstrip('/')}/api/v4",
                headers={"Authorization": f"Bearer {settings.MATTERMOST_BOT_TOKEN}"},
                timeout=settings.MATTERMOST_HTTP_TIMEOUT,
            )
        return self._client

    async def close(self) -> None:
        """Close the underlying HTTP client, if one was opened."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None
            logger.info("mattermost_client_closed")

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=8),
        retry=retry_if_exception_type(_RETRYABLE),
        reraise=True,
    )
    async def _request(self, method: str, path: str, **kwargs: Any) -> Dict[str, Any]:
        """Perform an authenticated request and return the decoded JSON body.

        Args:
            method: HTTP method.
            path: Path below /api/v4, e.g. "/posts".
            **kwargs: Passed through to httpx.

        Returns:
            dict: Decoded JSON response body.

        Raises:
            httpx.HTTPStatusError: On a non-2xx response.
            httpx.TransportError: On a connection failure.
        """
        client = self._get_client()
        response = await client.request(method, path, **kwargs)
        response.raise_for_status()
        return response.json()

    async def get_bot_user_id(self) -> Optional[str]:
        """Return the bot's own user id, fetched once and cached.

        This is the loop guard. A post created through the REST API re-triggers
        outgoing webhooks (Mattermost sets TriggerWebhooks unconditionally on
        that path), so without comparing the incoming user_id against this value
        the bot would answer its own replies forever.

        Returns:
            str | None: The bot user id, or None if it could not be resolved.
        """
        if self._bot_user_id is not None:
            return self._bot_user_id

        if not settings.MATTERMOST_BOT_TOKEN:
            logger.warning("mattermost_bot_token_missing")
            return None

        try:
            me = await self._request("GET", "/users/me")
        except Exception as e:
            logger.exception("mattermost_get_me_failed", error=str(e))
            return None

        self._bot_user_id = me.get("id")
        logger.info("mattermost_bot_identified", bot_user_id=self._bot_user_id, username=me.get("username"))
        return self._bot_user_id

    async def get_post(self, post_id: str) -> Optional[Dict[str, Any]]:
        """Fetch a single post.

        Args:
            post_id: The post id.

        Returns:
            dict | None: The post, or None on failure.
        """
        try:
            return await self._request("GET", f"/posts/{post_id}")
        except Exception as e:
            logger.exception("mattermost_get_post_failed", post_id=post_id, error=str(e))
            return None

    async def get_thread(self, root_id: str) -> Optional[Dict[str, Any]]:
        """Fetch every post in a thread.

        Args:
            root_id: The thread's root post id.

        Returns:
            dict | None: The thread ({"order": [...], "posts": {...}}), or None
            on failure.
        """
        try:
            return await self._request("GET", f"/posts/{root_id}/thread")
        except Exception as e:
            logger.exception("mattermost_get_thread_failed", root_id=root_id, error=str(e))
            return None

    async def get_team_by_name(self, name: str) -> Optional[Dict[str, Any]]:
        """Look up a team by its URL slug.

        Note this returns a truthful answer only when the bot can see the team.
        A plain `system_user` bot gets 403 here and, worse, a cheerful
        `{"exists": false}` from the /exists endpoint — which is why the bot is
        promoted to system_admin during bootstrap.

        Args:
            name: The team's URL slug.

        Returns:
            dict | None: The team, or None if absent or not visible.
        """
        try:
            return await self._request("GET", f"/teams/name/{name}")
        except Exception as e:
            logger.warning("mattermost_team_lookup_failed", team=name, error=str(e))
            return None

    async def add_user_to_team(self, team_id: str, user_id: str) -> bool:
        """Add a user to a team. Idempotent — an existing membership is fine.

        Args:
            team_id: Target team id.
            user_id: User to add.

        Returns:
            bool: True when the user is a member afterwards.
        """
        try:
            await self._request(
                "POST",
                f"/teams/{team_id}/members",
                json={"team_id": team_id, "user_id": user_id},
            )
        except Exception as e:
            logger.exception("mattermost_add_to_team_failed", team_id=team_id, user_id=user_id, error=str(e))
            return False

        logger.info("mattermost_user_added_to_team", team_id=team_id, user_id=user_id)
        return True

    async def get_user(self, user_id: str) -> Optional[Dict[str, Any]]:
        """Fetch a user by id.

        Args:
            user_id: The user id.

        Returns:
            dict | None: The user, or None on failure.
        """
        try:
            return await self._request("GET", f"/users/{user_id}")
        except Exception as e:
            logger.warning("mattermost_get_user_failed", user_id=user_id, error=str(e))
            return None

    async def get_user_by_username(self, username: str) -> Optional[Dict[str, Any]]:
        """Look up a user by handle.

        Args:
            username: The handle, without the leading ``@``.

        Returns:
            dict | None: The user, or None when no account has that handle.
        """
        try:
            return await self._request("GET", f"/users/username/{username.strip().lstrip('@')}")
        except Exception as e:
            logger.info("mattermost_user_username_lookup_miss", username=username, error=str(e))
            return None

    async def get_user_by_email(self, email: str) -> Optional[Dict[str, Any]]:
        """Look up a user by email address.

        Args:
            email: The address to find.

        Returns:
            dict | None: The user, or None when no account has that address.
        """
        try:
            return await self._request("GET", f"/users/email/{email}")
        except Exception as e:
            logger.info("mattermost_user_email_lookup_miss", email=email, error=str(e))
            return None

    async def create_team(self, name: str, display_name: str) -> Optional[Dict[str, Any]]:
        """Create an open team.

        The creating account is auto-joined and made team admin by Mattermost
        (CreateTeamWithUser sets that when the team email matches the creator),
        so a team the bot creates needs no explicit self-add.

        Args:
            name: URL slug — lowercase alphanumeric and dashes.
            display_name: Human-readable name.

        Returns:
            dict | None: The created team, or None on failure.
        """
        try:
            team = await self._request(
                "POST", "/teams", json={"name": name, "display_name": display_name, "type": "O"}
            )
        except Exception as e:
            logger.exception("mattermost_create_team_failed", name=name, error=str(e))
            return None

        logger.info("mattermost_team_created", team_id=team.get("id"), name=name)
        return team

    async def create_post(
        self,
        channel_id: str,
        message: str,
        root_id: Optional[str] = None,
        *,
        post_type: Optional[str] = None,
        props: Optional[Dict[str, Any]] = None,
        file_ids: Optional[List[str]] = None,
    ) -> Optional[Dict[str, Any]]:
        """Post a message to a channel.

        Args:
            channel_id: Target channel id.
            message: Message body (Markdown is rendered by Mattermost). This is
                also the fallback every client without the plugin shows, so it
                must read as a complete answer on its own.
            root_id: Root post id to reply under. Must be a thread ROOT —
                Mattermost rejects a root_id that is itself a reply.
            post_type: Custom post type for a plugin-rendered reply. Mattermost
                stores this in a varchar(26) column and rejects anything longer
                with a 500.
            props: Post props. Kept small by contract — artifact references, not
                artifact content.
            file_ids: Ids returned by ``upload_file``, attached natively.

        Returns:
            dict | None: The created post, or None on failure.
        """
        payload: Dict[str, Any] = {"channel_id": channel_id, "message": message}
        if root_id:
            payload["root_id"] = root_id
        if post_type:
            payload["type"] = post_type
        if props:
            payload["props"] = props
        if file_ids:
            payload["file_ids"] = file_ids

        try:
            post = await self._request("POST", "/posts", json=payload)
        except Exception as e:
            logger.exception("mattermost_create_post_failed", channel_id=channel_id, error=str(e))
            return None

        logger.info(
            "mattermost_post_created",
            channel_id=channel_id,
            post_id=post.get("id"),
            root_id=root_id,
            post_type=post_type,
            artifact_count=len(props.get("sf_artifacts", [])) if props else 0,
            file_count=len(file_ids or []),
        )
        return post

    async def reply_to_post(
        self,
        channel_id: str,
        message: str,
        trigger_post_id: Optional[str],
        *,
        post_type: Optional[str] = None,
        props: Optional[Dict[str, Any]] = None,
        file_ids: Optional[List[str]] = None,
    ) -> Optional[Dict[str, Any]]:
        """Reply in the same thread as the triggering post.

        Mattermost requires root_id to point at a thread root. If the trigger is
        itself a reply, we resolve its root first; posting with a non-root
        root_id is rejected with a 400 and the user would see no answer at all.

        The rich-media arguments are forwarded rather than dropped: forgetting
        them here would make artifacts render in direct messages and silently
        degrade to plain text in channels, which is exactly the transport
        divergence this module exists to prevent.

        Args:
            channel_id: Target channel id.
            message: Message body.
            trigger_post_id: The post that triggered this reply, if any.
            post_type: Custom post type, forwarded to ``create_post``.
            props: Post props, forwarded to ``create_post``.
            file_ids: Attachment ids, forwarded to ``create_post``.

        Returns:
            dict | None: The created post, or None on failure.
        """
        if not trigger_post_id:
            return await self.create_post(channel_id, message, post_type=post_type, props=props, file_ids=file_ids)

        root_id = trigger_post_id
        post = await self.get_post(trigger_post_id)
        if post and post.get("root_id"):
            # The trigger was already inside a thread — attach to that thread's root.
            root_id = post["root_id"]

        return await self.create_post(
            channel_id, message, root_id=root_id, post_type=post_type, props=props, file_ids=file_ids
        )

    async def update_post(
        self,
        post_id: str,
        *,
        message: Optional[str] = None,
        props: Optional[Dict[str, Any]] = None,
        file_ids: Optional[List[str]] = None,
    ) -> Optional[Dict[str, Any]]:
        """Update an already-published post in place.

        This is how a pending artifact becomes a finished one: the reply is
        posted immediately with a loading card and the worker fills it in.

        It is a PATCH, deliberately. ``PUT /posts/{id}`` REPLACES the post from
        the body, so a body without ``channel_id`` updates the post with an
        empty channel — and when that update attaches new ``file_ids``,
        Mattermost stamps that empty channel onto each FileInfo row, erasing
        the channel the upload had recorded. Every later read of the file then
        answers 404 "Unable to find the existing channel", and the webapp can
        only show its blurred mini-preview. That was the "pixelated images".
        PATCH merges into the stored post, so the channel survives.

        Args:
            post_id: The post to update.
            message: New message body, or None to keep the current one.
            props: New props map, already merged by the caller.
            file_ids: New attachment list, already merged by the caller.

        Returns:
            dict | None: The updated post, or None on failure.
        """
        payload: Dict[str, Any] = {}
        if message is not None:
            payload["message"] = message
        if props is not None:
            payload["props"] = props
        if file_ids is not None:
            payload["file_ids"] = file_ids

        try:
            post = await self._request("PUT", f"/posts/{post_id}/patch", json=payload)
        except Exception as e:
            logger.exception("mattermost_update_post_failed", post_id=post_id, error=str(e))
            return None

        logger.info("mattermost_post_updated", post_id=post_id, file_count=len(file_ids or []))
        return post

    async def upload_file(
        self,
        channel_id: str,
        filename: str,
        content: bytes,
        mime_type: str = "application/octet-stream",
    ) -> Optional[str]:
        """Upload one file and return its id, ready to attach to a post.

        This uses Mattermost's simple upload form — the channel and filename as
        QUERY parameters with the raw bytes as the body — rather than multipart.
        Multipart looks equivalent and is not: the server parses the parts in
        order, and httpx writes the file part before the ``channel_id`` field,
        so the FileInfo row was created with an EMPTY ChannelId. Such a file
        uploads and attaches without error, and then every read of it answers
        404 "Unable to find the existing channel" — so the webapp can never
        fetch the real image and shows its blurred mini-preview placeholder
        instead. That is what "the generated images are pixelated" was.

        Args:
            channel_id: Channel the file belongs to. The bot must be a member.
            filename: Name shown in the attachment.
            content: The bytes to upload.
            mime_type: Content type of the bytes.

        Returns:
            str | None: The file id, or None on failure.
        """
        try:
            result = await self._request(
                "POST",
                "/files",
                params={"channel_id": channel_id, "filename": filename},
                content=content,
                headers={"Content-Type": mime_type},
                timeout=settings.MATTERMOST_UPLOAD_TIMEOUT,
            )
        except Exception as e:
            logger.exception("mattermost_upload_failed", channel_id=channel_id, filename=filename, error=str(e))
            return None

        infos = result.get("file_infos") or []
        if not infos:
            logger.warning("mattermost_upload_returned_no_file", channel_id=channel_id, filename=filename)
            return None

        info = infos[0]
        file_id = info.get("id")
        if not info.get("channel_id"):
            # Refuse to attach a file the viewer will never be able to load.
            logger.error(
                "mattermost_upload_missing_channel",
                file_id=file_id,
                channel_id=channel_id,
                filename=filename,
            )
            return None

        logger.info(
            "mattermost_file_uploaded",
            channel_id=channel_id,
            file_id=file_id,
            size=len(content),
            width=info.get("width"),
            height=info.get("height"),
        )
        return file_id

    async def get_file_info(self, file_id: str) -> Optional[Dict[str, Any]]:
        """Read a file's metadata: name, size, content type, and the post it belongs to.

        Args:
            file_id: The file's id.

        Returns:
            dict | None: The FileInfo, or None when it cannot be read.
        """
        try:
            return await self._request("GET", f"/files/{file_id}/info")
        except Exception as e:
            logger.warning("mattermost_file_info_failed", file_id=file_id, error=str(e))
            return None

    async def download_file(self, file_id: str, *, max_bytes: int) -> Optional[bytes]:
        """Download a file's bytes, refusing to read past a ceiling.

        The size in FileInfo is checked first by the caller; this guard is for
        a body that turns out larger than declared.

        Args:
            file_id: The file's id.
            max_bytes: Abandon the download once more than this has arrived.

        Returns:
            bytes | None: The content, or None when the download fails.

        Raises:
            FileTooLarge: When the body exceeds ``max_bytes``.
        """
        client = self._get_client()
        chunks: list[bytes] = []
        received = 0
        try:
            async with client.stream(
                "GET", f"/files/{file_id}", timeout=settings.FILE_INPUT_DOWNLOAD_TIMEOUT
            ) as response:
                response.raise_for_status()
                async for chunk in response.aiter_bytes():
                    received += len(chunk)
                    if received > max_bytes:
                        raise FileTooLarge(file_id)
                    chunks.append(chunk)
        except FileTooLarge:
            raise
        except Exception as e:
            logger.warning("mattermost_file_download_failed", file_id=file_id, error=str(e))
            return None
        return b"".join(chunks)

    async def create_direct_channel(self, user_id: str) -> Optional[Dict[str, Any]]:
        """Open (or fetch) the DM channel between the bot and a user.

        Useful for proactive messages. Note that outgoing webhooks never fire in
        DMs, so a DM conversation can be *started* here but incoming DM replies
        require the WebSocket event stream instead.

        Args:
            user_id: The human user's id.

        Returns:
            dict | None: The direct channel, or None on failure.
        """
        bot_user_id = await self.get_bot_user_id()
        if not bot_user_id:
            logger.warning("mattermost_direct_channel_skipped_no_bot_id", user_id=user_id)
            return None

        try:
            return await self._request("POST", "/channels/direct", json=[bot_user_id, user_id])
        except Exception as e:
            logger.exception("mattermost_create_direct_channel_failed", user_id=user_id, error=str(e))
            return None


mattermost_client = MattermostClient()
