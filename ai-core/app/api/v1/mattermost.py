"""Mattermost outgoing-webhook endpoint for the SprintFlow Assistant.

Flow (the "hybrid" transport):

    student posts in a public channel
        -> Mattermost outgoing webhook  ->  POST /api/v1/mattermost/webhook
        -> we validate + ACK in milliseconds with an empty JSON body
        -> a background task runs the LangGraph agent (seconds to a minute)
        -> the answer is posted back with the bot's token via the REST API

Why not answer in the webhook response? Mattermost's outgoing-webhook HTTP
client has a hardcoded 30s ceiling that no config setting can raise, and it
never retries. An agent turn that overruns it would be dropped silently, with
the student seeing nothing. Acknowledging first makes the reply latency
irrelevant. An empty JSON body is the documented way to say "no inline reply":
Mattermost only creates a post from the response when `text` or `attachments`
is present.

Constraint worth remembering: outgoing webhooks fire ONLY in public channels.
DMs and private channels need the WebSocket event stream instead.
"""

import hmac
import json
from typing import (
    Any,
    Dict,
    List,
    Union,
)
from urllib.parse import parse_qs

from fastapi import (
    APIRouter,
    BackgroundTasks,
    HTTPException,
    Request,
    status,
)
from pydantic import (
    BaseModel,
    Field,
    ValidationError,
)

from app.core.config import settings
from app.core.limiter import limiter
from app.core.logging import logger
from app.services.conversation import (
    IncomingMessage,
    answer_and_reply,
    clean_text,
    is_own_post,
)
from app.services.mattermost import mattermost_client
from app.services.mattermost_ws import mattermost_ws_listener

router = APIRouter()


class MattermostWebhookPayload(BaseModel):
    """Payload Mattermost sends for an outgoing webhook.

    Every field is delivered in both the form-encoded and JSON encodings, but
    all are optional here so a future Mattermost release that drops or renames
    one degrades into a log line instead of a 422.
    """

    model_config = {"extra": "ignore"}

    token: str = Field(default="", description="Shared secret generated when the webhook was created")
    team_id: str = Field(default="")
    team_domain: str = Field(default="")
    channel_id: str = Field(default="", description="Channel the message was posted in")
    channel_name: str = Field(default="", description="URL slug of the channel, not its display name")
    # Encoding trap: Mattermost sends this as a STRING of seconds when
    # form-encoded, but as an INTEGER of milliseconds when the webhook's content
    # type is application/json. Same field, two types and two units.
    timestamp: Union[int, str] = Field(default="", description="Seconds if form-encoded, ms if JSON")
    user_id: str = Field(default="", description="Author's user id — the loop guard compares against this")
    user_name: str = Field(default="")
    post_id: str = Field(default="")
    text: str = Field(default="", description="Full message body")
    trigger_word: str = Field(default="", description="Empty for channel-scoped hooks with no trigger words")
    file_ids: Union[str, List[str]] = Field(default="", description="Comma-joined string in practice")


async def _parse_payload(request: Request) -> MattermostWebhookPayload:
    """Decode the request body as either JSON or form-encoded.

    Mattermost defaults to `application/x-www-form-urlencoded` and only sends
    JSON when the webhook's Content Type field was explicitly set, so both must
    work. The form branch is parsed directly from the raw body to avoid
    depending on `python-multipart`.

    Args:
        request: The incoming request.

    Returns:
        MattermostWebhookPayload: The parsed payload.

    Raises:
        HTTPException: 400 if the body cannot be decoded.
    """
    raw = await request.body()
    content_type = request.headers.get("content-type", "").lower()

    try:
        if content_type.startswith("application/json"):
            data: Dict[str, Any] = json.loads(raw or b"{}")
        else:
            data = {key: values[0] for key, values in parse_qs(raw.decode("utf-8")).items() if values}
    except (ValueError, UnicodeDecodeError) as e:
        logger.warning("mattermost_webhook_unparsable_body", content_type=content_type, error=str(e))
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="malformed webhook body")

    try:
        return MattermostWebhookPayload.model_validate(data)
    except ValidationError as e:
        # A field whose type changes between Mattermost releases should degrade
        # into a logged 400, never an unhandled 500 with a traceback.
        logger.warning("mattermost_webhook_invalid_payload", error=str(e))
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid webhook payload")


def _verify_token(received: str) -> None:
    """Check the webhook's shared secret in constant time.

    Mattermost sends the token in the request body only — there is no signature
    or Authorization header — so this comparison is the whole authentication
    story for the endpoint.

    Args:
        received: Token from the payload.

    Raises:
        HTTPException: 401 when a configured token does not match.
    """
    expected = settings.MATTERMOST_OUTGOING_WEBHOOK_TOKEN
    if not expected:
        # Allowed so the stack can boot before the webhook exists, but this
        # leaves the endpoint open to anyone who can reach the container.
        logger.warning("mattermost_webhook_token_not_configured")
        return

    if not hmac.compare_digest(received, expected):
        logger.warning("mattermost_webhook_token_mismatch")
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid webhook token")


@router.post("/webhook")
@limiter.limit(settings.RATE_LIMIT_ENDPOINTS["mattermost_webhook"][0])
async def mattermost_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
) -> Dict[str, Any]:
    """Receive a Mattermost outgoing webhook and answer asynchronously.

    Args:
        request: The incoming request, also used for rate limiting.
        background_tasks: FastAPI background runner for the agent turn.

    Returns:
        dict: An empty object. Mattermost creates no inline post from it, which
        is what we want — the reply arrives over the REST API instead.

    Raises:
        HTTPException: 400 on an unparsable body, 401 on a bad token.
    """
    payload = await _parse_payload(request)
    _verify_token(payload.token)

    prompt = clean_text(payload.text, payload.trigger_word)

    if not prompt:
        logger.info("mattermost_webhook_ignored_empty_text", channel_id=payload.channel_id)
        return {}

    if await is_own_post(payload.user_id, payload.user_name):
        logger.info("mattermost_webhook_ignored_own_post", user_name=payload.user_name)
        return {}

    logger.info(
        "mattermost_webhook_received",
        channel_name=payload.channel_name,
        user_name=payload.user_name,
        text_length=len(prompt),
    )

    background_tasks.add_task(
        answer_and_reply,
        IncomingMessage(
            channel_id=payload.channel_id,
            post_id=payload.post_id,
            text=prompt,
            user_id=payload.user_id,
            user_name=payload.user_name,
            # Outgoing webhooks only ever fire in public channels, so this is a
            # fact about the transport, not a guess. Replies stay threaded here.
            channel_type="O",
            source="webhook",
        ),
    )
    return {}


@router.get("/health")
@limiter.limit(settings.RATE_LIMIT_ENDPOINTS["health"][0])
async def mattermost_health(request: Request) -> Dict[str, Any]:
    """Report whether the bot token works, for debugging the wiring.

    Args:
        request: The incoming request, used for rate limiting.

    Returns:
        dict: Resolved bot user id, the configured Mattermost URL, and the
        WebSocket listener's connection state.
    """
    bot_user_id = await mattermost_client.get_bot_user_id()
    return {
        "status": "connected" if bot_user_id else "unconfigured",
        "bot_user_id": bot_user_id,
        "mattermost_url": settings.MATTERMOST_URL,
        "webhook_token_configured": "yes" if settings.MATTERMOST_OUTGOING_WEBHOOK_TOKEN else "no",
        # Public channels arrive over the webhook, DMs over the WebSocket —
        # this shows whether the second half is actually live.
        "websocket": mattermost_ws_listener.status(),
    }
