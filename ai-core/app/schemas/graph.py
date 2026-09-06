"""Graph state for the LangGraph agent.

Every field beyond ``messages`` and ``long_term_memory`` was added by the
Sprint 1 supervisor and is defaulted, so checkpoints written before it existed
still deserialise. Routing fields hold plain strings rather than enums so a
renamed route can never break loading an old conversation.
"""

from enum import Enum
from typing import (
    Annotated,
    List,
    Optional,
)

from langgraph.graph.message import add_messages
from pydantic import (
    BaseModel,
    Field,
)


class CapabilityRoute(str, Enum):
    """The specialist a turn is routed to. Each has its own tool group."""

    LEARNER_SUPPORT = "learner_support"
    BACK_OFFICE = "back_office"
    RICH_MEDIA = "rich_media"
    GENERAL = "general"


class GraphState(BaseModel):
    """State definition for the LangGraph Agent/Workflow."""

    messages: Annotated[list, add_messages] = Field(
        default_factory=list, description="The messages in the conversation"
    )
    long_term_memory: str = Field(default="", description="The long term memory of the conversation")

    # --- Sprint 1: supervisor routing (additive, all defaulted) ---
    route: Optional[str] = Field(default=None, description="The capability route this turn was sent to")
    route_plan: List[str] = Field(
        default_factory=list, description="Ordered routes still to run for a multi-step request"
    )
    route_confidence: Optional[float] = Field(default=None, description="Confidence of the rule match, 0-1")
    matched_rule: Optional[str] = Field(default=None, description="Which rule produced the decision")
    is_multi_intent: bool = Field(default=False, description="Whether the message spans more than one route")
