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
    Tuple,
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


class MattermostClient:
    """Async client for the Mattermost v4 REST API, authenticated as a bot."""

    def __init__(self) -> None:
        """Initialize the client without opening any connection."""
        self._client: Optional[httpx.AsyncClient] = None
        self._bot_user_id: Optional[str] = None

    def _get_client(self) -> httpx.AsyncClient:
        """Return the shared HTTP client, creating it on first use."""
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
        """Perform an authenticated request and return the decoded JSON body."""
        client = self._get_client()
        response = await client.request(method, path, **kwargs)
        response.raise_for_status()
        return response.json()

    async def get_bot_user_id(self) -> Optional[str]:
        """Return the bot's own user id, fetched once and cached."""
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

    async def get_user(self, user_id: str) -> Optional[Dict[str, Any]]:
        """Fetch a Mattermost user by ID."""
        try:
            return await self._request("GET", f"/users/{user_id}")
        except Exception as e:
            logger.exception("mattermost_get_user_failed", user_id=user_id, error=str(e))
            return None

    async def get_file(self, file_id: str) -> bytes:
        """Fetch raw binary content of an uploaded file by its Mattermost file ID."""
        client = self._get_client()
        response = await client.get(f"/files/{file_id}")
        response.raise_for_status()
        return response.content

    async def download_file(self, file_id: str) -> Optional[Tuple[bytes, str]]:
        """Download a file and return (content, original_filename)."""
        try:
            # 1. Get file metadata (for the original name)
            info = await self._request("GET", f"/files/{file_id}/info")
            filename = str(info.get("name") or f"file-{file_id}")

            # 2. Download the actual bytes
            client = self._get_client()
            response = await client.get(f"/files/{file_id}")
            response.raise_for_status()
            content = response.content

            return content, filename
        except Exception as e:
            logger.exception("mattermost_download_file_failed", file_id=file_id, error=str(e))
            return None

    async def get_post(self, post_id: str) -> Optional[Dict[str, Any]]:
        """Fetch a single post."""
        try:
            return await self._request("GET", f"/posts/{post_id}")
        except Exception as e:
            logger.exception("mattermost_get_post_failed", post_id=post_id, error=str(e))
            return None

    async def get_thread(self, root_id: str) -> Optional[Dict[str, Any]]:
        """Fetch every post in a thread."""
        try:
            return await self._request("GET", f"/posts/{root_id}/thread")
        except Exception as e:
            logger.exception("mattermost_get_thread_failed", root_id=root_id, error=str(e))
            return None

    async def get_team_by_name(self, name: str) -> Optional[Dict[str, Any]]:
        """Look up a team by its URL slug."""
        try:
            return await self._request("GET", f"/teams/name/{name}")
        except Exception as e:
            logger.warning("mattermost_team_lookup_failed", team=name, error=str(e))
            return None

    async def add_user_to_team(self, team_id: str, user_id: str) -> bool:
        """Add a user to a team. Idempotent."""
        try:
            await self._request(
                "POST",
                f"/teams/{team_id}/members",
                json={"team_id": team_id, "user_id": user_id},
            )
            return True
        except Exception as e:
            logger.warning("mattermost_add_user_to_team_failed", team_id=team_id, user_id=user_id, error=str(e))
            return False

    async def create_direct_channel(self, user_id: str) -> Optional[Dict[str, Any]]:
        """Create or retrieve an existing direct channel between the bot and a target user."""
        bot_user_id = await self.get_bot_user_id()
        if not bot_user_id:
            logger.error("mattermost_direct_channel_failed_no_bot_id", target_user_id=user_id)
            return None

        try:
            return await self._request(
                "POST",
                "/channels/direct",
                json=[bot_user_id, user_id],
            )
        except Exception as e:
            logger.exception("mattermost_create_direct_channel_failed", target_user_id=user_id, error=str(e))
            return None

    async def create_post(
        self,
        channel_id: str,
        message: str,
        root_id: Optional[str] = None,
        file_ids: Optional[List[str]] = None,
    ) -> Optional[Dict[str, Any]]:
        """Post a message to a specific Mattermost channel or thread."""
        payload: Dict[str, Any] = {
            "channel_id": channel_id,
            "message": message,
        }
        if root_id:
            payload["root_id"] = root_id
        if file_ids:
            payload["file_ids"] = file_ids

        try:
            return await self._request("POST", "/posts", json=payload)
        except Exception as e:
            logger.exception("mattermost_post_message_failed", channel_id=channel_id, error=str(e))
            return None

    async def post_message(
        self,
        channel_id: str,
        message: str,
        root_id: Optional[str] = None,
        file_ids: Optional[List[str]] = None,
    ) -> Optional[Dict[str, Any]]:
        """Alias for create_post for backward compatibility."""
        return await self.create_post(channel_id=channel_id, message=message, root_id=root_id, file_ids=file_ids)

    async def reply_to_post(
        self,
        channel_id: str,
        message: str,
        post_id: str,
        file_ids: Optional[List[str]] = None,
    ) -> Optional[Dict[str, Any]]:
        """Reply to a post, resolving the true root ID if post_id is inside a thread."""
        post = await self.get_post(post_id)
        root_id = (post.get("root_id") or post_id) if post else post_id
        return await self.create_post(channel_id=channel_id, message=message, root_id=root_id, file_ids=file_ids)

    async def send_direct_message(
        self,
        user_id: str,
        message: str,
        root_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Proactively open a DM channel with a user and post a message."""
        channel = await self.create_direct_channel(user_id)
        if not channel or "id" not in channel:
            return None

        return await self.create_post(
            channel_id=channel["id"],
            message=message,
            root_id=root_id,
        )


mattermost_client = MattermostClient()