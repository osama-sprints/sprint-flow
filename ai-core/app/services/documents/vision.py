"""Question-driven reading of page images — interpretation, kept apart from OCR.

``ocr.transcribe`` copies a page. This asks a vision model a question about
one or a few page images: what a diagram shows, whether a form is signed,
what the layout of a table implies, what a stamp says. The answer is an
interpretation and is labelled as one; provenance (which physical pages
were shown, which model answered) travels with it, and the prompt binds the
model to what is visible, asks it to name pages, and tells it to say when
the answer is not on these pages rather than guess.
"""

import base64
import time
from dataclasses import dataclass
from typing import (
    Any,
    Dict,
    List,
    Optional,
    Sequence,
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
from app.services.documents import policy

VISUAL_PROMPT = (
    "You are looking at {count} page image(s) from the document “{name}” — physical page(s) {pages}. "
    "Answer the question below using only what is visible on these pages. Name the page you rely on for "
    "each point, as (p. N). Quote text exactly when you rely on it; describe figures, charts, tables, "
    "stamps and layout as they appear. If the answer is not visible on these pages, say so plainly instead "
    "of guessing. Answer in the language of the question.\n\nQuestion: {question}"
)


class VisualAnswerFailed(Exception):
    """The model returned nothing usable."""


@dataclass
class VisualAnswer:
    """What the model said about the pages, and what it cost.

    Attributes:
        text: The answer.
        model: Model that answered.
        pages: Physical pages that were shown.
        prompt_tokens: Input tokens, when reported.
        completion_tokens: Output tokens, when reported.
        cost_usd: Cost, when the proxy priced the call.
        latency_ms: Wall time of the call.
    """

    text: str
    model: str
    pages: List[int]
    prompt_tokens: Optional[int]
    completion_tokens: Optional[int]
    cost_usd: Optional[float]
    latency_ms: int

    @property
    def usage(self) -> Dict[str, Any]:
        """Usage for logs and results.

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
async def answer_about_pages(
    images: Sequence[Tuple[int, bytes]], *, question: str, model: str, document_name: str
) -> VisualAnswer:
    """Ask a question about rendered pages.

    Args:
        images: ``(page_no, jpeg)`` pairs, in page order.
        question: The person's or the agent's question.
        model: Model name as the proxy knows it.
        document_name: For the prompt's provenance line.

    Returns:
        VisualAnswer: The answer with provenance and accounting.

    Raises:
        VisualAnswerFailed: When the model returns no text.
        httpx.HTTPError: On a transport or status failure after retries.
    """
    pages = [page_no for page_no, _ in images]
    content: List[Dict[str, Any]] = [
        {
            "type": "text",
            "text": VISUAL_PROMPT.format(
                count=len(images),
                name=document_name,
                pages=", ".join(str(p) for p in pages),
                question=question.strip(),
            ),
        }
    ]
    for page_no, jpeg in images:
        content.append({"type": "text", "text": f"Page {page_no}:"})
        content.append(
            {
                "type": "image_url",
                "image_url": {"url": f"data:image/jpeg;base64,{base64.b64encode(jpeg).decode('ascii')}"},
            }
        )
    payload = {
        "model": model,
        "temperature": 0,
        "max_tokens": 2048,
        "messages": [{"role": "user", "content": content}],
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
    answer = (choices[0].get("message") or {}).get("content") if choices else None
    if isinstance(answer, list):
        answer = "".join(part.get("text", "") for part in answer if isinstance(part, dict))
    text = (answer or "").strip()
    if not text:
        raise VisualAnswerFailed("empty answer")

    usage = body.get("usage") or {}
    result = VisualAnswer(
        text=text,
        model=str(body.get("model") or model),
        pages=pages,
        prompt_tokens=usage.get("prompt_tokens"),
        completion_tokens=usage.get("completion_tokens"),
        cost_usd=_cost(response.headers.get("x-litellm-response-cost")),
        latency_ms=latency_ms,
    )
    logger.info(
        "pdf_pages_asked",
        pages=pages,
        model=result.model,
        chars=len(text),
        prompt_tokens=result.prompt_tokens,
        completion_tokens=result.completion_tokens,
        cost_usd=result.cost_usd,
        latency_ms=latency_ms,
    )
    return result
