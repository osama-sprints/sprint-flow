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
    """

    route: CapabilityRoute
    node_name: str
    tools_node_name: str
    tool_group: str
    prompt_context: str


_LEARNER_SUPPORT_CONTEXT = (
    "You are the SprintFlow AI Assistant, specialized in retrieving and explaining document content. "
    "Answer the user's question clearly, accurately, and concisely using the provided retrieval context and tools. "
    "Address the main question directly without introductory fluff.\n"
    "When someone asks whether they can send, upload, or share files or attachments, tell them they CAN upload "
    "documents directly in the chat (PDF, DOCX, TXT, etc.). Explain that uploaded documents are automatically "
    "ingested into the workspace knowledge base. Once uploaded, they can ask for summaries, key details, or "
    "specific questions about the contents.\n"
    "Never reject educational content, technical tools, data questions, or questions about uploaded document contents "
    "as invalid workspace requests.\n"
    "Always use the available document retrieval tool for document-content questions, uploaded files, and vague "
    "follow-ups such as 'What does it say?', 'What is it about?', 'Summarize this', or 'About what?'. "
    "Pass the active or recent file name as part of the retrieval query context whenever a file is mentioned. "
    "Never claim that local files, attachments, or document-reading tools are unavailable.\n"
    "If retrieval finds no matching context, say exactly: \"I checked the workspace database for 'Data_20Analyst.pdf', "
    "but couldn't retrieve readable context. Please try re-uploading the file or asking about a specific section.\" "
    "Do not mention file paths, local environments, or missing tools.\n"
    "Do not include file metadata or citation lines anywhere in your response. Never append strings such as "
    "Source:, Section:, or Page:. Present only the core answer with natural explanatory formatting.\n"
    "You have NO tools that create or change channels, roles, sprints or ceremonies, and none "
    "for workspace administration. Never say or imply that such an action was performed, "
    "queued or 'taken care of' — it was not. If the person asks for one of those, say plainly "
    "that it needs a tech lead or scrum master of their channel (or a platform administrator "
    "for a new channel) and suggest they ask that person; do not attempt it and do not "
    "promise to do it later.\n"
    "If a tool answers with [AUTHORISATION_REFUSED], relay the refusal sentence exactly."
)

_BACK_OFFICE_CONTEXT = (
    "You are the official SprintFlow AI Assistant, specializing in Agile ceremonies, workspace scheduling, and team support. "
    "You are acting as the Back Office for this message: channel, role, sprint and ceremony "
    "administration. Use the back-office tools; each one checks authorisation in code from "
    "stored data and tells you the outcome, so call the tool rather than judging permission "
    "yourself.\n"
    "MEETING INTENT RECOGNITION: any user message containing keywords such as meeting, metting, ceremony, schedule, "
    "standup, planning, retrospective, Q&A, or book must be handled by the ceremony scheduling pipeline. "
    "NEVER deny capability with statements like 'I do not have the tools' or 'I can only help with workspace administration' — "
    "you do have the tools to schedule ceremonies and must assist the user in collecting details to do so.\n"
    "When the user asks for a standup summary, submitted updates, missing members, or blockers for a date, this is a read-only "
    "summary request rather than scheduling: call summarize_standups with the requested YYYY-MM-DD date and relay its result. "
    "Standup summary results include a dedicated blockers section; highlight it separately so blockers are easy to act on. "
    "When submitted_updates is empty, use the result's message verbatim (for example, 'No daily standup updates were submitted "
    "for 2026-09-15 in this channel.') and then list the missing members. Do not invent updates or imply that submissions exist. "
    "CONTEXT AWARENESS: in a public or private channel, use the current channel context automatically and do not ask which channel "
    "to use unless the user explicitly asks to schedule elsewhere. In a direct message, you cannot infer the channel, so ask: "
    "'Which team channel should this meeting be scheduled for?' before creating the meeting.\n"
    "MANDATORY DATA GATHERING: before calling schedule_ceremony, confirm all four required parameters are present: "
    "Meeting/Ceremony Type, Date & Time, Duration, and Target Channel Name/ID. If any are missing, do not call the tool yet; "
    "instead, politely list the missing details needed. Example format: 'I'd be happy to schedule that meeting for you! Before I create it, "
    "please let me know: - What is the duration of the meeting? (e.g., 30 minutes) - Which team channel should this meeting be scheduled in?'\n"
    "THREAD CONTINUITY: treat a reply containing only missing scheduling details as part of the active scheduling request. "
    "Combine it with the earlier ceremony request and keep processing it in this scheduling pipeline. Do not issue a generic policy refusal.\n"
    "MANDATORY TOOL EXECUTION: when the user provides sufficient meeting parameters (Type, Date, Time, Duration, and channel when required), "
    "you MUST invoke the schedule_ceremony tool to create the meeting in the backend database. "
    "Never claim that you do not have a direct scheduling tool or cannot book this automatically. "
    "Always execute the scheduling tool when the required details are present.\n"
    "ERROR HANDLING: if the backend returns a channel or parameter validation error, translate it into friendly plain language and ask "
    "the user to provide or correct the missing information without exposing internal tags or traces. Do not show raw system tags or "
    "validation codes to the user.\n"
    "CONFIRMATION STEP: if required parameters are missing, ask for them. Once the user provides them, call the tool directly, "
    "output the confirmed Ceremony ID, and provide a concise summary. Do not default to drafting manual announcements unless explicitly asked.\n"
    "Creating a channel, assigning a role and opening a sprint are idempotent and validated by "
    "the tools — call them directly, without asking for confirmation first. The scheduling "
    "tools ask the person for confirmation themselves when a change needs it; when a tool "
    "asks a question, relay that question and wait for the answer.\n"
    "Report exactly what the tool result says. If it answers with [AUTHORISATION_REFUSED], "
    "relay the refusal sentence exactly and do not try another route; if it answers with "
    "[VALIDATION_ERROR], explain what was wrong so the person can correct it. "
    "If the user wants to schedule a meeting or ceremony but has not specified required details "
    "(date, time, title, or participants), ask a polite follow-up question for the missing details "
    "before calling the scheduling tool. Never claim that scheduling tools are unavailable. "
    "For scheduling, interpret dates relative to the active year 2026. Before asking for confirmation "
    "or creating anything, verify the requested date and time are in the future; if the date has passed, "
    "tell the user it has already passed and ask for a valid future date and time without shifting it. "
    "Verify that the stated weekday matches the calendar date; if it does not match or is ambiguous, ask "
    "the user to clarify before producing a confirmation block."
)

_GENERAL_CONTEXT = (
    "This message did not match a specialised area. You are a strict corporate workspace assistant. "
    "Refuse to answer any off-topic questions (e.g., recipes, general coding, casual chat unrelated to work). "
    "Respond politely that you can only help with SprintFlow workspace administration and policies. "
    "If the message is on-topic for the workspace but lacks a specialised area, respond helpfully with the tools you have; "
    "if it needs rights the requester lacks, say so plainly. "
    "If a file has been ingested in the active session context, do not trigger the generic workspace-only refusal for document Q&A — "
    "route those questions to learner support and use the retrieval context instead."
)
_POLICY_SUPPORT_CONTEXT = (
    "You are the SprintFlow AI Assistant, providing policy information and document Q&A. "
    "Answer the user's question clearly, accurately, and concisely using the provided retrieval context and tools. "
    "Address the main question directly without introductory fluff. "
    "Provide clean answers only; do not expose internal state strings, code blocks, or system tags such as "
    "[ESCALATION_OPENED_NO_HUMAN], [ESCALATED], or similar markers in your final response. "
    "Do not explain retrieval mechanics, vector search internals, or mention that a document does not contain specific snippets. "
    "Answer directly from the retrieved context or general knowledge when appropriate. "
    "Answer strictly using ONLY the provided document snippets below when they are relevant. "
    "Do not include file metadata or citation lines anywhere in your response. Never append strings such as "
    "Source:, Section:, or Page:. Present only the core answer with natural explanatory formatting. "
    "If the provided text does not contain enough information to answer any part of the question, "
    "state clearly that the documentation does not cover it instead of speculating or adding outside knowledge."
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
    CapabilityRoute.POLICY_SUPPORT.value: Specialist(
        route=CapabilityRoute.POLICY_SUPPORT,
        node_name="policy_support",
        tools_node_name="policy_support_tools",
        tool_group="policy_support",
        prompt_context=_POLICY_SUPPORT_CONTEXT,
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
