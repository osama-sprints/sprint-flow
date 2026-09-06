"""The specialists the supervisor can delegate to, and what each one is allowed to do.

A specialist is a pair of graph nodes (a model node and a tool-executor node)
bound to ONE tool group. The model node binds exactly that group for its call,
so the model cannot see — let alone call — another specialist's tools, and the
executor node refuses any name outside the group. That structural boundary is
the point of the supervisor pattern: the learner-facing specialist has no way
to reach an administrative action, whatever the prompt or the model says.

Adding a specialisation is three edits: a ``CapabilityRoute`` value, a tool
group in ``tools/__init__.py``, and a ``Specialist`` entry here (plus routing
rules in ``routing_rules.py`` so the supervisor can pick it). ``graph.py``
builds the nodes from this table; it has no per-specialist code.

Node names for the ``general`` route are ``chat`` and ``tool_call`` on
purpose: they are the pre-Sprint-1 node names, and a conversation that was
paused inside ``tool_call`` before the supervisor existed resumes into the
same-named node of the new graph.
"""

from dataclasses import dataclass
from typing import (
    Dict,
    List,
    Optional,
)

from app.schemas.graph import CapabilityRoute


@dataclass(frozen=True)
class Specialist:
    """One specialised capability the supervisor can route a turn to.

    Attributes:
        route: The capability route this specialist serves.
        node_name: Graph node that calls the model with this specialist's tools bound.
        tools_node_name: Graph node that executes ONLY this specialist's tool group.
        tool_group: Key into ``TOOL_GROUPS`` — the only tools this specialist can use.
        prompt_context: The ``# Routing`` text appended to the system prompt.
        force_tool_use: Require a tool call on this specialist's FIRST model call
            of a turn. For a specialist whose whole job is to produce an
            artifact, a prose answer is a silent failure — the person asked for
            a diagram and received a description of one — and instructions alone
            did not reliably prevent it.
    """

    route: CapabilityRoute
    node_name: str
    tools_node_name: str
    tool_group: str
    prompt_context: str
    force_tool_use: bool = False


_LEARNER_SUPPORT_CONTEXT = (
    "You are acting as Learner Support for this message: answer questions, help with "
    "blockers and study or process questions, and read the cohort calendar with the tools "
    "you have.\n"
    "You have NO tools that create or change cohorts, roles, sprints or ceremonies, and none "
    "for workspace administration. Never say or imply that such an action was performed, "
    "queued or 'taken care of' — it was not. If the person asks for one of those, say plainly "
    "that it needs a tech lead or scrum master of their cohort (or a platform administrator "
    "for a new cohort) and suggest they ask that person; do not attempt it and do not "
    "promise to do it later.\n"
    "If a tool answers with [AUTHORISATION_REFUSED], relay the refusal sentence exactly."
    "\nWhen the person asks about something the system stores — their cohorts, "
    "members, sprints, ceremonies or schedule — CALL the read tool that has it and "
    "answer from what it returns. Never ask them to supply data you can look up "
    "yourself, and never state a figure you have not read from a tool. If a tool "
    "returns nothing, say plainly that there is nothing recorded rather than "
    "inventing an example."
)

_BACK_OFFICE_CONTEXT = (
    "You are acting as the Back Office for this message: cohort, role, sprint and ceremony "
    "administration. Use the back-office tools; each one checks authorisation in code from "
    "stored data and tells you the outcome, so call the tool rather than judging permission "
    "yourself.\n"
    "Creating a cohort, assigning a role and opening a sprint are idempotent and validated by "
    "the tools — call them directly, without asking for confirmation first. The scheduling "
    "tools ask the person for confirmation themselves when a change needs it; when a tool "
    "asks a question, relay that question and wait for the answer.\n"
    "Report exactly what the tool result says. If it answers with [AUTHORISATION_REFUSED], "
    "relay the refusal sentence exactly and do not try another route; if it answers with "
    "[VALIDATION_ERROR], explain what was wrong so the person can correct it."
    "\nWhen the person asks about something the system stores — their cohorts, "
    "members, sprints, ceremonies or schedule — CALL the read tool that has it and "
    "answer from what it returns. Never ask them to supply data you can look up "
    "yourself, and never state a figure you have not read from a tool. If a tool "
    "returns nothing, say plainly that there is nothing recorded rather than "
    "inventing an example."
)

_RICH_MEDIA_CONTEXT = (
    "You are composing the VISUAL part of this reply. You MUST call exactly one of your "
    "tools before you answer. Writing the diagram source, the chart specification or the "
    "component code into the message instead of calling the tool is a failure: the person "
    "sees raw text and no visual at all.\n"
    "Pick the tool by what was asked: send_mermaid_diagram for a flow, process, sequence or "
    "hierarchy; send_chart for measurements you already have; send_react_artifact when the "
    "person needs to try values themselves (a calculator, an estimator, a what-if); "
    "generate_and_send_image only when an illustration itself is the point.\n"
    "Do not deliberate in the reply and do not show your working. Call the tool with your "
    "best attempt; if it answers [VALIDATION_ERROR] fix exactly what it names and call it "
    "again.\n"
    "Never invent business numbers. If a chart needs figures nobody gave you and no tool of "
    "yours can read them, say which figures you need — do not estimate them.\n"
    "For an interface, build it only from the 'sprintflow/ui' design system the tool "
    "describes — no CSS, no style attributes, no other packages.\n"
    "Language: use the language of the person's LATEST message for the reply, the artifact "
    "title and every label inside it — English for an English message, Arabic for an Arabic "
    "one — regardless of the language of earlier messages, memories or this channel.\n"
    "After the tool succeeds, write ONE short sentence. The visual is attached to your reply "
    "automatically."
)

_GENERAL_CONTEXT = (
    "This message did not match a specialised area. Respond helpfully as a general assistant "
    "with the tools you have; if it needs rights the requester lacks, say so plainly."
)

SPECIALISTS: Dict[str, Specialist] = {
    CapabilityRoute.LEARNER_SUPPORT.value: Specialist(
        route=CapabilityRoute.LEARNER_SUPPORT,
        node_name="learner_support",
        tools_node_name="learner_support_tools",
        tool_group="learner_support",
        prompt_context=_LEARNER_SUPPORT_CONTEXT,
    ),
    CapabilityRoute.BACK_OFFICE.value: Specialist(
        route=CapabilityRoute.BACK_OFFICE,
        node_name="back_office",
        tools_node_name="back_office_tools",
        tool_group="back_office",
        prompt_context=_BACK_OFFICE_CONTEXT,
    ),
    CapabilityRoute.RICH_MEDIA.value: Specialist(
        route=CapabilityRoute.RICH_MEDIA,
        node_name="rich_media",
        tools_node_name="rich_media_tools",
        tool_group="rich_media",
        prompt_context=_RICH_MEDIA_CONTEXT,
        force_tool_use=True,
    ),
    CapabilityRoute.GENERAL.value: Specialist(
        route=CapabilityRoute.GENERAL,
        node_name="chat",
        tools_node_name="tool_call",
        tool_group="general",
        prompt_context=_GENERAL_CONTEXT,
    ),
}

DEFAULT_SPECIALIST: Specialist = SPECIALISTS[CapabilityRoute.GENERAL.value]


def specialist_for(route: Optional[str]) -> Specialist:
    """Return the specialist serving a route, falling back to the general one.

    An unknown or missing route (an old checkpoint, a renamed route) must never
    strand a turn, so the fallback is the behaviour that existed before the
    supervisor: the general specialist.

    Args:
        route: A ``CapabilityRoute`` value as stored in the graph state, or None.

    Returns:
        Specialist: The matching specialist, or the general one.
    """
    if not route:
        return DEFAULT_SPECIALIST
    return SPECIALISTS.get(route, DEFAULT_SPECIALIST)


def describe_route(
    route: Optional[str],
    route_plan: Optional[List[str]] = None,
    *,
    continuation: bool = False,
) -> str:
    """Return the ``# Routing`` section of the system prompt for one specialist call.

    Args:
        route: The route the current specialist is running as.
        route_plan: Routes still to run after this one, for a multi-step request.
        continuation: True when earlier routes of the same request already ran
            and this specialist must produce the single final reply.

    Returns:
        str: The prompt section, or an empty string when the turn has no route.
    """
    if not route:
        return ""
    body = specialist_for(route).prompt_context
    if continuation:
        body += (
            "\nEarlier parts of this same request were already handled by other specialists; "
            "their results are the assistant messages above, after the person's last message. "
            "Do your part now, then write ONE final reply that covers your part AND summarises "
            "those earlier results in a couple of sentences — only your reply will be shown to the "
            "person, so it must stand on its own. Do not mention routing, specialists or internal steps."
        )
    if route_plan:
        remaining = ", ".join(route_plan)
        body += (
            f"\nThis message spans more than one area; after you finish, the following will handle "
            f"the rest: {remaining}. Do only your part here, state its outcome plainly, and do not "
            f"apologise for or comment on the rest."
        )
    return f"# Routing\n{body}"


__all__ = [
    "DEFAULT_SPECIALIST",
    "SPECIALISTS",
    "Specialist",
    "describe_route",
    "specialist_for",
]
