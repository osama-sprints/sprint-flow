"""Files and images people attach to a message.

Intake runs once per turn, before the graph: each attached file is fetched
from Mattermost as the bot, checked for what it really is, read, and recorded.
What the model sees is decided here too — a compact summary in the message
that is checkpointed, and the full extract plus any images only in the model
call for this turn (see ``augment_llm_messages``).
"""

from app.services.attachments.detect import (
    SUPPORTED_EXTENSIONS,
    Unsupported,
    detect,
)
from app.services.attachments.service import (
    AcceptedAttachment,
    RejectedAttachment,
    TurnAttachments,
    augment_llm_messages,
    bind,
    clear,
    current_attachments,
    ingest,
    state_text,
    vision_model_override,
)

__all__ = [
    "SUPPORTED_EXTENSIONS",
    "AcceptedAttachment",
    "RejectedAttachment",
    "TurnAttachments",
    "Unsupported",
    "augment_llm_messages",
    "bind",
    "clear",
    "current_attachments",
    "detect",
    "ingest",
    "state_text",
    "vision_model_override",
]
