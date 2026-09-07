"""Page transcription by a vision model, through the LiteLLM proxy.

Transcription is kept apart from answering: the model here is asked only to
write down what the page says, faithfully, in its own language, marking what
it cannot read. Whatever the agent then concludes is a separate model call
with the transcript in front of it. The proxy reports token usage in the body
and, when it can price the call, a cost header; both are recorded per page so
a run can be accounted for. No confidence numbers are requested: a model's
self-rating is not calibrated reliability and would be presented as such.
"""

import base64
import time
from dataclasses import dataclass
from typing import (
    Any,
    Dict,
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
from app.services.documents import policy

TRANSCRIPTION_PROMPT = (
    "You are transcribing one page of a document. Write out the page's text exactly as printed, in its "
    "original language or languages — do not translate, summarise, correct, complete or add anything. "
    "Keep the reading order; put headings and paragraphs on their own lines. Reproduce tables as Markdown "
    "tables with the same rows, columns and cell values. Copy numbers, dates, amounts and identifiers "
    "character for character. Where text cannot be read, write [unreadable]. For a figure, chart or "
    "diagram, write one line in square brackets describing what it shows and quoting any text in it, for "
    "example [figure: three boxes labelled A, B, C connected by arrows]. If the page is blank, write "
    "[empty page]. Output only the transcription."
)

EMPTY_PAGE_MARKER = "[empty page]"
UNREADABLE_MARKER = "[unreadable]"


class OcrFailed(Exception):
    """The model returned nothing usable for the page."""


@dataclass
class Transcription:
    """What the model wrote for one page, and what it cost.

    Attributes:
        text: The transcription.
        model: Model that produced it.
        prompt_tokens: Input tokens, when reported.
        completion_tokens: Output tokens, when reported.
        cost_usd: Cost, when the proxy priced the call.
        latency_ms: Wall time of the call.
    """

    text: str
    model: str
    prompt_tokens: Optional[int]
    completion_tokens: Optional[int]
    cost_usd: Optional[float]
    latency_ms: int

    @property
    def usage(self) -> Dict[str, Any]:
        """Usage as stored with the page.

        Returns:
            dict: tokens, cost and model.
        """
        return {
            "model": self.model,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "cost_usd": self.cost_usd,
        }


def _cost(header: Optional[str]) -> Optional[float]:
    if not header:
        return None
    try:
        return float(header)
    except ValueError:
        return None


@retry(
    stop=stop_after_attempt(policy.PDF.ocr_attempts),
    wait=wait_exponential(multiplier=1, min=1, max=6),
    retry=retry_if_exception_type(httpx.TransportError),
    reraise=True,
)
async def transcribe(image_jpeg: bytes, *, model: str, page_no: int) -> Transcription:
    """Transcribe one rendered page.

    Args:
        image_jpeg: The rendered page.
        model: Model name as the proxy knows it.
        page_no: For logs only.

    Returns:
        Transcription: The text and its accounting.

    Raises:
        OcrFailed: When the model returns no text.
        httpx.HTTPError: On a transport or status failure after retries.
    """
    payload = {
        "model": model,
        "temperature": 0,
        "max_tokens": 4096,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": TRANSCRIPTION_PROMPT},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{base64.b64encode(image_jpeg).decode('ascii')}"},
                    },
                ],
            }
        ],
    }
    started = time.monotonic()
    async with httpx.AsyncClient(timeout=policy.PDF.ocr_timeout_seconds) as client:
        response = await client.post(
            f"{settings.OPENAI_BASE_URL.rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {settings.OPENAI_API_KEY}"},
            json=payload,
        )
    latency_ms = int((time.monotonic() - started) * 1000)
    response.raise_for_status()
    body = response.json()

    choices = body.get("choices") or []
    content = (choices[0].get("message") or {}).get("content") if choices else None
    if isinstance(content, list):
        content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
    text = (content or "").strip()
    if not text:
        raise OcrFailed(f"page {page_no}: empty transcription")

    usage = body.get("usage") or {}
    result = Transcription(
        text=text,
        model=str(body.get("model") or model),
        prompt_tokens=usage.get("prompt_tokens"),
        completion_tokens=usage.get("completion_tokens"),
        cost_usd=_cost(response.headers.get("x-litellm-response-cost")),
        latency_ms=latency_ms,
    )
    logger.info(
        "pdf_page_transcribed",
        page=page_no,
        model=result.model,
        chars=len(text),
        prompt_tokens=result.prompt_tokens,
        completion_tokens=result.completion_tokens,
        cost_usd=result.cost_usd,
        latency_ms=latency_ms,
    )
    return result
