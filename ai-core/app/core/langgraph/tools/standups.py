"""Read-only standup summary tool for the current channel."""

import json
from datetime import date

from langchain_core.tools import tool

from app.core.requester import current_requester
from app.core.langgraph.tools.results import guarded_tool
from app.services.authorisation import ValidationFailed
from app.services.domain.standups import get_standup_summary_for_channel


@tool
@guarded_tool
async def summarize_standups(target_date: str) -> str:
    """Summarize submitted and missing standups for the current channel.

    Args:
        target_date: Calendar date in ``YYYY-MM-DD`` format.

    Returns:
        str: A JSON object containing submitted updates, missing members, and sprint information.
    """
    requester = current_requester.get()
    channel_id = requester.channel_id if requester else ""
    if not channel_id:
        raise ValidationFailed("Standup summaries require a channel context.")
    try:
        parsed_date = date.fromisoformat(target_date.strip())
    except ValueError as exc:
        raise ValidationFailed("Target date must use YYYY-MM-DD format.") from exc

    summary = await get_standup_summary_for_channel(channel_id, parsed_date)
    return json.dumps(summary._asdict())


TOOLS = [summarize_standups]

__all__ = ["TOOLS", "summarize_standups"]
