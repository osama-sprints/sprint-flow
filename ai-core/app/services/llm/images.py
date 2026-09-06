"""Image generation through the LiteLLM proxy.

The endpoint and the response shape here were established against the live
proxy, not assumed:

* ``POST /images/generations`` answers 404 for ``gemini/nano-banana-pro-preview``
  — Gemini's image models are not served by the predict-style endpoint;
* ``POST /chat/completions`` answers 200 and returns the picture at
  ``choices[0].message.images[0].image_url.url`` as a ``data:image/jpeg;base64``
  URI, with ``message.content`` empty.

So this is a chat call that happens to return an image, and the parser is
written for that shape. Every call still goes through the proxy with the same
credential as the rest of the stack; no provider SDK and no second key.
"""

import base64
import binascii
import re
from typing import (
    Any,
    Dict,
    NamedTuple,
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

# Formats Mattermost renders inline. Anything else is refused rather than
# uploaded as an opaque attachment.
ALLOWED_MIME = ("image/jpeg", "image/png", "image/webp", "image/gif")

# A generated image larger than this is treated as a failure: it would take the
# post over Mattermost's attachment limits and says something went wrong.
MAX_IMAGE_BYTES = 12 * 1024 * 1024

_DATA_URI = re.compile(r"^data:(?P<mime>[-\w.+/]+);base64,(?P<payload>.+)$", re.DOTALL)


class GeneratedImage(NamedTuple):
    """One generated picture.

    Attributes:
        content: The decoded bytes.
        mime_type: Its media type, validated against ``ALLOWED_MIME``.
    """

    content: bytes
    mime_type: str


class ImageGenerationError(RuntimeError):
    """Raised when the proxy answers, but not with a usable image."""


def _extract(body: Dict[str, Any]) -> GeneratedImage:
    """Pull the image out of a chat-completions response.

    Args:
        body: The decoded response body.

    Returns:
        GeneratedImage: The decoded bytes and their media type.

    Raises:
        ImageGenerationError: When the response carries no usable image.
    """
    choices = body.get("choices") or []
    message = (choices[0].get("message") or {}) if choices else {}
    images = message.get("images") or []
    if not images:
        # The model sometimes answers in words instead — a refusal, usually.
        text = message.get("content") or ""
        raise ImageGenerationError(f"the model returned no image ({str(text)[:120]})")

    first = images[0]
    url = first.get("image_url", {}).get("url") if isinstance(first, dict) else None
    if not isinstance(url, str):
        raise ImageGenerationError("the image had no data url")

    match = _DATA_URI.match(url)
    if not match:
        # A remote URL would mean fetching from an origin we have not vetted.
        raise ImageGenerationError("the image was not returned inline")

    mime = match.group("mime").lower()
    if mime not in ALLOWED_MIME:
        raise ImageGenerationError(f"unsupported image type {mime}")

    try:
        content = base64.b64decode(match.group("payload"), validate=True)
    except (binascii.Error, ValueError) as e:
        raise ImageGenerationError(f"the image payload did not decode ({e})")

    if not content:
        raise ImageGenerationError("the image was empty")
    if len(content) > MAX_IMAGE_BYTES:
        raise ImageGenerationError(f"the image was too large ({len(content)} bytes)")

    return GeneratedImage(content=content, mime_type=mime)


@retry(
    stop=stop_after_attempt(2),
    wait=wait_exponential(multiplier=1, min=2, max=8),
    retry=retry_if_exception_type(httpx.TransportError),
    reraise=True,
)
async def generate_image(prompt: str, *, aspect_ratio: str = "1:1") -> GeneratedImage:
    """Generate one image and return its bytes.

    Only transport failures are retried, and only once. A generation request is
    metered: retrying an ambiguous outcome — a timeout after the model may have
    already produced the picture — risks paying twice, so a timeout is left to
    the caller's durable job, which decides with the row's attempt count.

    Args:
        prompt: What to draw.
        aspect_ratio: Requested shape, appended to the prompt because the chat
            endpoint takes no size parameter.

    Returns:
        GeneratedImage: The decoded image.

    Raises:
        ImageGenerationError: When the proxy returns no usable image.
        httpx.HTTPError: On a transport or status failure.
    """
    instruction = (
        f"{prompt.strip()}\n\nAspect ratio: {aspect_ratio}. Produce a single clean illustration with no text overlay."
    )
    payload = {
        "model": settings.IMAGE_MODEL,
        "messages": [{"role": "user", "content": instruction}],
        "modalities": ["image", "text"],
    }

    async with httpx.AsyncClient(timeout=settings.IMAGE_TIMEOUT) as client:
        response = await client.post(
            f"{settings.OPENAI_BASE_URL.rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {settings.OPENAI_API_KEY}"},
            json=payload,
        )
        response.raise_for_status()
        body = response.json()

    image = _extract(body)
    logger.info(
        "image_generated",
        model=settings.IMAGE_MODEL,
        mime_type=image.mime_type,
        size_bytes=len(image.content),
        aspect_ratio=aspect_ratio,
    )
    return image


def extension_for(mime_type: str) -> str:
    """Return a file extension for a validated media type.

    Args:
        mime_type: One of ``ALLOWED_MIME``.

    Returns:
        str: The extension, including the dot.
    """
    return {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "image/gif": ".gif"}.get(
        mime_type, ".img"
    )


def parse_data_uri(url: str) -> Optional[GeneratedImage]:
    """Decode a data URI, for tests and for replayed responses.

    Args:
        url: A ``data:`` URI.

    Returns:
        GeneratedImage | None: The image, or None when the URI is unusable.
    """
    try:
        return _extract({"choices": [{"message": {"images": [{"image_url": {"url": url}}]}}]})
    except ImageGenerationError:
        return None
