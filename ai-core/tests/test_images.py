"""Tests for the image-generation response contract.

These pin the shape this integration was BUILT against, established against the
live proxy: the picture arrives from /chat/completions as a base64 data URI at
``choices[0].message.images[0].image_url.url``. If a proxy upgrade changes that,
these fail loudly rather than the worker silently producing empty attachments.

Nothing here calls the model. A live generation is a separate, metered check.
"""

import base64

import pytest

from app.services.llm.images import (
    ALLOWED_MIME,
    MAX_IMAGE_BYTES,
    ImageGenerationError,
    _extract,
    extension_for,
    parse_data_uri,
)

PIXEL = base64.b64encode(b"\xff\xd8\xff\xe0 fake jpeg bytes").decode()


def _response(url: str) -> dict:
    """Build a chat-completions body carrying one image."""
    return {"choices": [{"message": {"content": None, "images": [{"image_url": {"url": url}}]}}]}


def test_data_uri_is_decoded_with_its_media_type():
    """The shape the proxy actually returns is parsed."""
    image = _extract(_response(f"data:image/jpeg;base64,{PIXEL}"))
    assert image.mime_type == "image/jpeg"
    assert image.content.startswith(b"\xff\xd8")


def test_every_allowed_type_round_trips():
    """The renderable formats are the ones accepted."""
    for mime in ALLOWED_MIME:
        assert parse_data_uri(f"data:{mime};base64,{PIXEL}") is not None
        assert extension_for(mime).startswith(".")


def test_unexpected_media_type_is_refused():
    """An SVG would be an executable document, not a picture."""
    with pytest.raises(ImageGenerationError, match="unsupported image type"):
        _extract(_response(f"data:image/svg+xml;base64,{PIXEL}"))


def test_remote_url_is_refused():
    """A URL would mean fetching from an origin nobody vetted."""
    with pytest.raises(ImageGenerationError, match="not returned inline"):
        _extract(_response("https://example.com/generated.png"))


def test_oversized_image_is_refused():
    """Beyond the cap the upload would fail anyway; fail early and say why."""
    payload = base64.b64encode(b"x" * (MAX_IMAGE_BYTES + 1)).decode()
    with pytest.raises(ImageGenerationError, match="too large"):
        _extract(_response(f"data:image/png;base64,{payload}"))


def test_corrupt_payload_is_refused():
    """A truncated response must not become a zero-byte attachment."""
    with pytest.raises(ImageGenerationError, match="did not decode"):
        _extract(_response("data:image/png;base64,!!!not-base64!!!"))


def test_a_text_only_answer_is_reported_as_a_refusal():
    """The model sometimes answers in words; that is a failure, not an image."""
    body = {"choices": [{"message": {"content": "I can't create that image.", "images": []}}]}
    with pytest.raises(ImageGenerationError, match="no image"):
        _extract(body)
