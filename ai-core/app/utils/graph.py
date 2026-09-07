"""This file contains the graph utilities for the application."""

import tiktoken
from langchain_core.messages import BaseMessage
from langchain_core.messages import convert_to_messages
from langchain_core.messages import trim_messages as _trim_messages

from app.core.config import settings
from app.core.logging import logger
from app.schemas import Message

# Cache tiktoken encoding at module level — thread-safe and reusable
try:
    _TIKTOKEN_ENCODING = tiktoken.encoding_for_model(settings.DEFAULT_LLM_MODEL)
except KeyError:
    _TIKTOKEN_ENCODING = tiktoken.get_encoding("cl100k_base")


def _count_tokens_tiktoken(messages: list) -> int:
    """Count tokens locally using tiktoken — no API call needed."""
    num_tokens = 0
    for message in messages:
        # Every message has overhead tokens for role/name
        num_tokens += 4
        if isinstance(message, dict):
            for _, value in message.items():
                if isinstance(value, str):
                    num_tokens += len(_TIKTOKEN_ENCODING.encode(value))
        elif isinstance(message, BaseMessage):
            content = message.content
            if isinstance(content, str):
                num_tokens += len(_TIKTOKEN_ENCODING.encode(content))
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, str):
                        num_tokens += len(_TIKTOKEN_ENCODING.encode(block))
                    elif isinstance(block, dict) and "text" in block:
                        num_tokens += len(_TIKTOKEN_ENCODING.encode(block["text"]))
    num_tokens += 2  # every reply is primed with assistant
    return num_tokens


def dump_messages(messages: list[Message]) -> list[dict]:
    """Dump the messages to a list of dictionaries.

    Args:
        messages (list[Message]): The messages to dump.

    Returns:
        list[dict]: The dumped messages.
    """
    return [message.model_dump() for message in messages]


def extract_text_content(content: str | list) -> str:
    """Extract plain text from an LLM content value.

    Handles both the simple string format and the structured block list returned
    by GPT-5 / Responses API models:
        [{'type': 'reasoning', ...}, {'type': 'text', 'text': '...'}]

    Args:
        content: Raw content from a LangChain BaseMessage.

    Returns:
        Plain text string (empty string when nothing extractable is present).
    """
    if isinstance(content, str):
        return content

    parts: list[str] = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict):
            if block.get("type") == "text":
                parts.append(block.get("text", ""))
            elif block.get("type") == "reasoning":
                logger.debug(
                    "reasoning_block_received",
                    reasoning_id=block.get("id"),
                    has_summary=bool(block.get("summary")),
                )
    return "".join(parts)


def was_cut_short(response: BaseMessage) -> bool:
    """Whether the provider stopped the reply at the completion ceiling.

    Only a visible answer counts: a message that ends in tool calls is the
    start of more work, not a cut reply.

    Args:
        response: The model's message.

    Returns:
        bool: True when ``finish_reason`` is ``length`` and there is no tool call.
    """
    metadata = getattr(response, "response_metadata", None) or {}
    if getattr(response, "tool_calls", None):
        return False
    return str(metadata.get("finish_reason") or "").lower() in ("length", "max_tokens")


def process_llm_response(response: BaseMessage) -> BaseMessage:
    """Normalise a raw LLM response so that ``response.content`` is always a plain string, regardless of the provider's content format.

    Args:
        response: The raw response from the LLM.

    Returns:
        The same BaseMessage instance with ``content`` set to a plain string.
    """
    if isinstance(response.content, list):
        response.content = extract_text_content(response.content)
        logger.debug(
            "processed_structured_content",
            content_block_count=len(response.content),
            extracted_length=len(response.content),
        )
    return response


def _is_human(message: dict) -> bool:
    return message.get("role") == "user" or message.get("type") == "human"


def _is_tool_result(message: dict) -> bool:
    return message.get("role") == "tool" or message.get("type") == "tool"


# Characters kept from a shortened tool result, tried in turn: the first pass
# that brings the turn inside its budget stops the shrinking, so the newest
# results — the ones the answer is being written from — keep the most text.
_TOOL_RESULT_CAPS = (12000, 4000, 1200, 400)


def _shorten_tool_result(message: dict, keep: int) -> dict:
    """Cut a tool result down, leaving the model a way to read the rest.

    The head of every tool result in this codebase names the tool's subject —
    the document, the id, the page range — so keeping it makes the cut
    retrievable: the model can call the same tool again for the same pages.

    Args:
        message: A dumped tool message.
        keep: Characters of the result to keep.

    Returns:
        dict: The message, shortened when it was longer than ``keep``.
    """
    content = message.get("content")
    if not isinstance(content, str) or len(content) <= keep:
        return message
    shortened = dict(message)
    shortened["content"] = (
        content[:keep].rstrip() + f"\n\n… [{message.get('name') or 'tool'} result shortened to fit this turn "
        f"({len(content):,} characters in all). Call the same tool again with the arguments named above to "
        "read the part that was cut.]"
    )
    return shortened


def _bound_turn(current: list[dict], budget: int) -> list[dict]:
    """Bring the current turn inside ``budget`` tokens without dropping a message.

    The person's request and every tool call stay exactly as they are, so the
    tool-call/result pairing the provider requires is never broken; only the
    text of tool RESULTS is shortened, oldest first, and each cut says how to
    read the rest. A turn that cannot be brought inside the budget this way
    (hundreds of calls) is logged rather than truncated further.

    Args:
        current: The dumped messages of the current turn, starting with the person's.
        budget: Token ceiling for the turn.

    Returns:
        list[dict]: The bounded turn.
    """
    if not current or budget <= 0:
        return current
    tokens = _count_tokens_tiktoken(current)
    if tokens <= budget:
        return current

    bounded = list(current)
    positions = [index for index, message in enumerate(bounded) if _is_tool_result(message)]
    for cap in _TOOL_RESULT_CAPS:
        for index in positions:
            if _count_tokens_tiktoken(bounded) <= budget:
                break
            bounded[index] = _shorten_tool_result(bounded[index], cap)

    final_tokens = _count_tokens_tiktoken(bounded)
    logger.warning(
        "current_turn_shortened_to_fit",
        before_tokens=tokens,
        after_tokens=final_tokens,
        budget=budget,
        tool_results=len(positions),
        still_over=final_tokens > budget,
    )
    return bounded


def prepare_messages(messages: list[Message], system_prompt: str) -> list[Message]:
    """Prepare the messages for the LLM.

    The current turn — the person's latest message and everything the graph
    has added since (tool calls and their results) — is never dropped. Only
    the history before it competes for ``MAX_HISTORY_TOKENS``. Trimming the
    whole list used to drop this turn's tool results and, with them, the
    person's message itself once a turn's reads outgrew the budget; the model
    then saw only the system prompt and answered with a greeting.

    The turn is not unbounded either: past ``MAX_TURN_TOKENS`` its older tool
    results are shortened — never removed, so the tool-call/result pairing
    stays valid — and each cut tells the model how to read the rest.

    Args:
        messages (list[Message]): The messages to prepare.
        system_prompt (str): The system prompt to use.

    Returns:
        list[Message]: The prepared messages.
    """
    dumped = dump_messages(messages)
    split = next((i for i in range(len(dumped) - 1, -1, -1) if _is_human(dumped[i])), None)
    history = dumped if split is None else dumped[:split]
    current = _bound_turn([] if split is None else dumped[split:], settings.MAX_TURN_TOKENS)

    try:
        current_tokens = _count_tokens_tiktoken(current) if current else 0
        budget = max(0, settings.MAX_HISTORY_TOKENS - current_tokens)
        trimmed_history = (
            _trim_messages(
                history,
                strategy="last",
                token_counter=_count_tokens_tiktoken,
                max_tokens=budget,
                start_on="human",
                include_system=False,
                allow_partial=False,
            )
            if history and budget > 0
            else []
        )
        trimmed_messages = list(trimmed_history) + list(convert_to_messages(current))
    except ValueError as e:
        # Handle unrecognized content blocks (e.g., reasoning blocks from GPT-5)
        if "Unrecognized content block type" in str(e):
            logger.warning(
                "token_counting_failed_skipping_trim",
                error=str(e),
                message_count=len(messages),
            )
            # Skip trimming and return all messages
            trimmed_messages = messages
        else:
            raise

    # The system prompt is authored by us, not typed by a person, so the
    # 3000-character user-input cap on Message.content must not apply to it:
    # routing context plus long-term memory routinely exceeds it, and a
    # validation error here would fail every turn of the conversation.
    system_message = Message.model_construct(role="system", content=system_prompt)
    return [system_message] + trimmed_messages
