"""This file contains the graph schema for the application."""

from enum import Enum
from typing import Annotated, List, Optional

from langgraph.graph.message import add_messages
from pydantic import (
    BaseModel,
    Field,
)


class Specialisation(str, Enum):
    """The specialist handler a request is routed to."""

    LEARNER_SUPPORT = "learner_support"
    ADMIN_OPS = "admin_ops"
    GENERAL_FALLBACK = "general_fallback"


class GraphState(BaseModel):
    """State definition for the LangGraph Agent/Workflow."""

    messages: Annotated[list, add_messages] = Field(
        default_factory=list, description="The messages in the conversation"
    )
    long_term_memory: str = Field(default="", description="The long term memory of the conversation")

    # --- Sprint 1: supervisor routing (additive — all optional/defaulted so
    # existing checkpointed state, which predates these fields, still loads) ---
    specialisation: Optional[Specialisation] = Field(
        default=None, description="The specialist this turn was routed to"
    )
    route_confidence: Optional[float] = Field(
        default=None, description="Confidence of the rule-based routing decision, 0-1"
    )
    matched_rule: Optional[str] = Field(
        default=None, description="Which rule produced the routing decision, for observability"
    )
    is_admin: Optional[bool] = Field(
        default=False, description="Snapshot of the requester's admin status, for observability only"
    )
    is_multi_intent: Optional[bool] = Field(
        default=False, description="Whether the message spans more than one specialisation"
    )
    sub_intents: Optional[List[Specialisation]] = Field(
        default_factory=list, description="The specialisations involved when is_multi_intent is True"
    )
