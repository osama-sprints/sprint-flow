"""Labelled routing sentences: the executable specification of ``routing_rules.py``.

Every entry states what the supervisor must decide for a sentence from a given
kind of requester. The unit tests assert every entry; the in-container probe
(``scripts/_routing_probe.py``) re-runs the same set on the deployed code and
times it. Extend this list whenever a rule changes — a rule without a labelled
sentence is a rule nobody will notice breaking.

Requester kinds:

- ``learner``    an active learner in cohort 1, no admin role anywhere
- ``authority``  a scrum master of cohort 1 (cohort authority, not superadmin)
- ``admin``      a superadmin with no cohort membership at all
- ``anonymous``  no requester bound (identity sync failed)

Entries marked ``hard`` are the deliberately ambiguous ones; their notes say
why the expected answer is the right compromise.
"""

from types import MappingProxyType
from typing import (
    Dict,
    NamedTuple,
    Optional,
    Tuple,
)

from app.core.requester import RequesterContext
from app.schemas.graph import CapabilityRoute

LEARNER = CapabilityRoute.LEARNER_SUPPORT.value
BACK_OFFICE = CapabilityRoute.BACK_OFFICE.value
GENERAL = CapabilityRoute.GENERAL.value


class RoutingExample(NamedTuple):
    """One labelled sentence.

    Attributes:
        text: The message as a person would type it.
        requester: One of ``learner``, ``authority``, ``admin``, ``anonymous``.
        routes: Expected route values, in plan order.
        rule: Expected primary ``matched_rule``, or None to check the routes only.
        hard: Whether this is a deliberately ambiguous case.
        note: Why the expectation is what it is.
    """

    text: str
    requester: str
    routes: Tuple[str, ...]
    rule: Optional[str] = None
    hard: bool = False
    note: str = ""


REQUESTERS: Dict[str, Optional[RequesterContext]] = {
    "learner": RequesterContext(
        mattermost_user_id="mm-learner",
        username="lena",
        channel_type="D",
        user_id=11,
        cohort_roles=MappingProxyType({1: "learner"}),
    ),
    "authority": RequesterContext(
        mattermost_user_id="mm-sm",
        username="sam",
        channel_type="D",
        user_id=12,
        cohort_roles=MappingProxyType({1: "scrum_master"}),
    ),
    "admin": RequesterContext(
        mattermost_user_id="mm-admin",
        username="admin",
        email="admin@sprints.ai",
        channel_type="D",
        user_id=1,
        is_superadmin=True,
    ),
    "anonymous": None,
}


ROUTING_EXAMPLES: Tuple[RoutingExample, ...] = (
    # ---- cohort administration -----------------------------------------------------
    RoutingExample("create cohort Backend-01", "authority", (BACK_OFFICE,), "back_office_cohort"),
    RoutingExample("open a new cohort called Frontend-02", "admin", (BACK_OFFICE,), "back_office_cohort"),
    RoutingExample(
        "please set up the cohort Data-03 for the March intake", "admin", (BACK_OFFICE,), "back_office_cohort"
    ),
    RoutingExample("archive cohort Backend-01", "authority", (BACK_OFFICE,), "back_office_cohort"),
    RoutingExample(
        "deactivate the cohort Frontend-02, the programme ended", "admin", (BACK_OFFICE,), "back_office_cohort"
    ),
    RoutingExample("create a cohort", "learner", (LEARNER,), "back_office_cohort_denied_role"),
    RoutingExample("archive cohort Backend-01", "learner", (LEARNER,), "back_office_cohort_denied_role"),
    RoutingExample(
        "I want to create a cohort for the new intake", "learner", (LEARNER,), "back_office_cohort_denied_role"
    ),
    RoutingExample("create cohort Backend-01", "anonymous", (LEARNER,), "back_office_cohort_denied_role"),
    # ---- role administration -------------------------------------------------------
    RoutingExample("make @bob the scrum master of Backend-01", "authority", (BACK_OFFICE,), "back_office_role"),
    RoutingExample("promote alice to tech lead", "authority", (BACK_OFFICE,), "back_office_role"),
    RoutingExample("assign the tech_lead role to @carol in Backend-01", "admin", (BACK_OFFICE,), "back_office_role"),
    RoutingExample("change bob's role to learner", "authority", (BACK_OFFICE,), "back_office_role"),
    RoutingExample("add @dave to cohort Backend-01 as a learner", "authority", (BACK_OFFICE,), "back_office_cohort"),
    RoutingExample("demote @erin from scrum master to learner", "authority", (BACK_OFFICE,), "back_office_role"),
    RoutingExample("give @frank the ops support role in Data-03", "admin", (BACK_OFFICE,), "back_office_role"),
    RoutingExample("promote me to tech lead", "learner", (LEARNER,), "back_office_role_denied_role"),
    RoutingExample("make @bob the scrum master of Backend-01", "learner", (LEARNER,), "back_office_role_denied_role"),
    # ---- sprint administration -----------------------------------------------------
    RoutingExample("open sprint 2 for Backend-01", "authority", (BACK_OFFICE,), "back_office_sprint"),
    RoutingExample("start sprint 3 on Monday for cohort Data-03", "admin", (BACK_OFFICE,), "back_office_sprint"),
    RoutingExample("kick off a new sprint called Sprint-4", "authority", (BACK_OFFICE,), "back_office_sprint"),
    RoutingExample("close sprint 1 for Backend-01", "authority", (BACK_OFFICE,), "back_office_sprint"),
    RoutingExample("mark sprint 2 as complete", "authority", (BACK_OFFICE,), "back_office_sprint"),
    RoutingExample("open sprint 2", "learner", (LEARNER,), "back_office_sprint_denied_role"),
    # ---- ceremony scheduling -------------------------------------------------------
    RoutingExample(
        "schedule the standup for tomorrow at 9am for cohort Backend-01",
        "authority",
        (BACK_OFFICE,),
        "back_office_schedule",
    ),
    RoutingExample("book sprint planning for Monday 2pm", "authority", (BACK_OFFICE,), "back_office_schedule"),
    RoutingExample("set up a q&a session next Thursday at 4pm", "authority", (BACK_OFFICE,), "back_office_schedule"),
    RoutingExample(
        "put office hours on the calendar for Friday 3pm", "authority", (BACK_OFFICE,), "back_office_schedule"
    ),
    RoutingExample("schedule the demo for Friday at 3pm", "admin", (BACK_OFFICE,), "back_office_schedule"),
    RoutingExample("reschedule the retro to Wednesday 11am", "authority", (BACK_OFFICE,), "back_office_schedule"),
    RoutingExample("move tomorrow's standup to 10", "authority", (BACK_OFFICE,), "back_office_schedule"),
    RoutingExample("cancel the sprint review on Thursday", "authority", (BACK_OFFICE,), "back_office_schedule"),
    RoutingExample(
        "amend the planning session: make it 90 minutes", "authority", (BACK_OFFICE,), "back_office_schedule"
    ),
    RoutingExample("postpone the retrospective by a day", "authority", (BACK_OFFICE,), "back_office_schedule"),
    RoutingExample(
        "update the agenda for tomorrow's planning to: API design, testing strategy",
        "authority",
        (BACK_OFFICE,),
        "back_office_schedule",
    ),
    RoutingExample(
        "set the retro agenda to 'what slowed us down'", "authority", (BACK_OFFICE,), "back_office_schedule"
    ),
    RoutingExample("let's move the retro to 3pm", "authority", (BACK_OFFICE,), "back_office_schedule"),
    RoutingExample(
        "hey @sprintflow-assistant schedule the standup for tomorrow 9am",
        "authority",
        (BACK_OFFICE,),
        "back_office_schedule",
    ),
    RoutingExample("schedule a retro for Friday", "learner", (LEARNER,), "back_office_schedule_denied_role"),
    RoutingExample("cancel tomorrow's standup", "learner", (LEARNER,), "back_office_schedule_denied_role"),
    # ---- calendar reads (open to any member, same route for everyone) --------------
    RoutingExample("when is the next standup?", "learner", (LEARNER,), "learner_calendar"),
    RoutingExample("when is the next standup?", "authority", (LEARNER,), "learner_calendar"),
    RoutingExample("what's on this week?", "learner", (LEARNER,), "learner_calendar"),
    RoutingExample("show me the calendar for Backend-01", "learner", (LEARNER,), "learner_calendar"),
    RoutingExample("is there a retro tomorrow?", "learner", (LEARNER,), "learner_calendar"),
    RoutingExample("what time is sprint planning?", "learner", (LEARNER,), "learner_calendar"),
    RoutingExample("when does the sprint end?", "learner", (LEARNER,), "learner_calendar"),
    RoutingExample("tell me when the next demo is", "learner", (LEARNER,), "learner_calendar"),
    RoutingExample("remind me about the next standup", "learner", (LEARNER,), "learner_calendar"),
    RoutingExample("what ceremonies are scheduled for Backend-01?", "admin", (LEARNER,), "learner_calendar"),
    RoutingExample("is the retro cancelled?", "authority", (LEARNER,), "learner_calendar"),
    # ---- learner questions ---------------------------------------------------------
    RoutingExample("what's the deadline for the assignment?", "learner", (LEARNER,), "learner_support"),
    RoutingExample("what is the leave policy?", "learner", (LEARNER,), "learner_support"),
    RoutingExample("I'm blocked on the docker setup, who do I ask?", "learner", (LEARNER,), "learner_support"),
    RoutingExample("how do I submit my project?", "learner", (LEARNER,), "learner_support"),
    RoutingExample("which cohort am I in?", "learner", (LEARNER,), "learner_support"),
    RoutingExample("what's my role?", "learner", (LEARNER,), "learner_support"),
    RoutingExample("who is my tech lead?", "learner", (LEARNER,), "learner_support"),
    RoutingExample("can I take a day off next week?", "learner", (LEARNER,), "learner_support"),
    RoutingExample("what is a sprint?", "learner", (LEARNER,), "learner_support"),
    # ---- directory reads (gated like the member-listing tool) ----------------------
    RoutingExample("list cohorts", "authority", (LEARNER,), "cohort_directory"),
    RoutingExample("who is in cohort Backend-01?", "authority", (LEARNER,), "cohort_directory"),
    RoutingExample("show me the members of Backend-01", "admin", (LEARNER,), "cohort_directory"),
    RoutingExample("who is the scrum master of Backend-01?", "learner", (LEARNER,), "cohort_directory"),
    # ---- workspace administration (pre-Sprint-1 tools, general route) --------------
    RoutingExample("add alice@x.com to team Growth", "admin", (GENERAL,), "workspace_admin"),
    RoutingExample("create a team called Growth", "admin", (GENERAL,), "workspace_admin"),
    RoutingExample("invite bob to the Growth team", "admin", (GENERAL,), "workspace_admin"),
    RoutingExample(
        "add alice@x.com to team Growth",
        "learner",
        (GENERAL,),
        "workspace_admin",
        note="routing is not the security boundary: the workspace tools refuse non-superadmins in code",
    ),
    # ---- general fallback ----------------------------------------------------------
    RoutingExample("hello there", "learner", (GENERAL,), "general_fallback"),
    RoutingExample("thanks!", "authority", (GENERAL,), "general_fallback"),
    RoutingExample("what's the weather like in Cairo today?", "learner", (GENERAL,), "general_fallback"),
    RoutingExample("tell me a joke", "authority", (GENERAL,), "general_fallback"),
    RoutingExample("asdfgh ???", "learner", (GENERAL,), "general_fallback"),
    RoutingExample("hello there", "anonymous", (GENERAL,), "general_fallback"),
    # ---- multi-intent --------------------------------------------------------------
    RoutingExample(
        "open sprint 2 for Backend-01 and tell me when the retro is",
        "authority",
        (BACK_OFFICE, LEARNER),
        "back_office_sprint",
    ),
    RoutingExample(
        "what's on this week and open sprint 2 for Backend-01",
        "authority",
        (BACK_OFFICE, LEARNER),
        "back_office_sprint",
        hard=True,
        note="plan order follows rule consequence (mutation first, read second), not word order",
    ),
    RoutingExample(
        "open sprint 2 for Backend-01 and tell me when the retro is",
        "learner",
        (LEARNER,),
        "back_office_sprint_denied_role",
        note="both halves collapse to learner support; the denial label is kept for observability",
    ),
    RoutingExample(
        "create cohort Growth-01 and make @bob its scrum master",
        "admin",
        (BACK_OFFICE,),
        "back_office_cohort",
        note="two back-office actions are one route: the specialist handles both with its tools",
    ),
    # ---- deliberately ambiguous ----------------------------------------------------
    RoutingExample(
        "when should we schedule the retro?",
        "authority",
        (LEARNER,),
        "learner_calendar",
        hard=True,
        note="question-shaped: an admin verb inside a question is a read, not an order",
    ),
    RoutingExample(
        "what's the schedule for the retro?",
        "authority",
        (LEARNER,),
        "learner_calendar",
        hard=True,
        note="'schedule' after a determiner is a noun; only the verb form is an order",
    ),
    RoutingExample(
        "can you open sprint 2 for Backend-01?",
        "authority",
        (BACK_OFFICE,),
        "back_office_sprint",
        hard=True,
        note="a polite order is still an order; 'can you' is not an interrogative opener",
    ),
    RoutingExample(
        "how do I schedule a retro?",
        "learner",
        (LEARNER,),
        "learner_support",
        hard=True,
        note="a how-to question routes to support even though it names an admin action",
    ),
    RoutingExample(
        "add @bob to cohort Backend-01",
        "authority",
        (BACK_OFFICE,),
        "back_office_cohort",
        hard=True,
        note="cohort membership is back office; 'team' (Mattermost) is workspace admin",
    ),
    RoutingExample(
        "schedule the standup for tomorrow and the day after",
        "authority",
        (BACK_OFFICE,),
        "back_office_schedule",
        hard=True,
        note="'and' inside one order must not split it into a second read route",
    ),
    RoutingExample(
        "did you cancel the retro?",
        "authority",
        (LEARNER,),
        "learner_calendar",
        hard=True,
        note="a question about a past action is a read",
    ),
)


__all__ = ["BACK_OFFICE", "GENERAL", "LEARNER", "REQUESTERS", "ROUTING_EXAMPLES", "RoutingExample"]
