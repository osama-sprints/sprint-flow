"""Read-only access to the files a conversation has received.

The extract of a document is shown to the model in the turn the file arrives;
later turns see one line per file with its id. These two tools are how a
later question reaches the stored text again — and how a long document is
paged through — without the bytes riding along in every prompt.

Both are scoped to the conversation the turn runs in, taken from the trusted
turn context. A file attached in another channel or thread is simply not
found, whatever id the model supplies.
"""

from langchain_core.tools import tool

from app.core.langgraph.tools.results import (
    ResultCode,
    guarded_tool,
    tool_result,
)
from app.services import rich_media
from app.services.attachments.service import format_bytes
from app.services.domain import attachments as store

_MAX_SLICE = 20000


@tool
@guarded_tool
async def list_attachments() -> str:
    """List the files people have attached earlier in this conversation.

    Use this when someone refers to "the file", "that spreadsheet" or "the
    document I sent" and its content is not in front of you. Each entry gives
    the id to pass to read_attachment. Images are listed but cannot be reread:
    they are shown to you only in the message they arrive with.

    Returns:
        A line per file: name, what it is, whether text is stored, and its id.
    """
    context = rich_media.current_rich_media.get()
    if context is None:
        return tool_result(
            ResultCode.ATTACHMENT_UNAVAILABLE, "Attachments can only be read while answering a message."
        )

    rows = await store.list_for_conversation(context.session_id, context.channel_id)
    if not rows:
        return tool_result(ResultCode.ATTACHMENTS_LISTED, "No files have been attached in this conversation.")

    lines = []
    for row in rows:
        if row.status != "accepted":
            lines.append(f"- {row.name} — not read ({row.rejection_reason}) — id {row.id}")
            continue
        detail = f"{row.kind}, {format_bytes(row.size_bytes)}"
        if row.page_count:
            detail += f", {row.page_count} page(s)/sheet(s)"
        if row.visual:
            detail += "; image or scanned pages, shown only in its own message"
        elif row.text:
            detail += f"; {row.text_chars:,} characters of text stored"
        lines.append(f"- {row.name} — {detail} — id {row.id}")
    return tool_result(ResultCode.ATTACHMENTS_LISTED, "\n".join(lines))


@tool
@guarded_tool
async def read_attachment(attachment_id: str, offset: int = 0, length: int = 8000) -> str:
    """Read stored text from a file attached in this conversation.

    Use this to continue past the excerpt you were shown, or to reread a
    document from an earlier message. Text carries its provenance markers
    ([page N], [sheet Name], [table N]); cite them. Ask for the next window
    by advancing offset by the length you read.

    Args:
        attachment_id: The id shown with the file.
        offset: Character position to start from (0 = beginning).
        length: How many characters to return (at most 20,000).

    Returns:
        The requested window of text with its position, or why it is unavailable.
    """
    context = rich_media.current_rich_media.get()
    if context is None:
        return tool_result(
            ResultCode.ATTACHMENT_UNAVAILABLE, "Attachments can only be read while answering a message."
        )

    row = await store.get_for_conversation(attachment_id.strip(), context.session_id, context.channel_id)
    if row is None or row.status != "accepted":
        return tool_result(
            ResultCode.ATTACHMENT_NOT_FOUND, "No readable file with that id exists in this conversation."
        )
    if row.visual or not row.text:
        return tool_result(
            ResultCode.ATTACHMENT_UNAVAILABLE,
            f"{row.name} is an image or scanned document; it was shown to you in the message it arrived with and has "
            "no stored text. Ask the person to send it again if you need to look at it now.",
        )

    start = max(0, int(offset))
    size = max(1, min(int(length), _MAX_SLICE))
    window = row.text[start : start + size]
    if not window:
        return tool_result(
            ResultCode.ATTACHMENT_TEXT,
            f"{row.name}: nothing at offset {start:,}; the text is {len(row.text):,} characters.",
        )
    end = start + len(window)
    more = f' read_attachment("{row.id}", offset={end}) continues.' if end < len(row.text) else " End of text."
    header = f"{row.name}, characters {start + 1:,}–{end:,} of {len(row.text):,}.{more}"
    return tool_result(ResultCode.ATTACHMENT_TEXT, f"{header}\n<<<\n{window}\n>>>")


ATTACHMENT_TOOLS = [list_attachments, read_attachment]
