"""Rule-based routing table for the Sprint 1 supervisor.

The single source of truth for how a message's text, together with the
requester's stored authority, maps to a capability route. No model is ever
consulted here: matching is a few dozen compiled regular expressions, so the
common path adds microseconds, not a network call.

How a decision is made:

1. The text is normalised (whitespace, curly apostrophes) and tested against
   every rule in ``ROUTING_RULES``, in order of consequence: mutations first
   (cohort, role, sprint, ceremony scheduling), then directory and calendar
   reads, then learner support, then workspace administration.
2. A *mutation* rule matches only imperative phrasing. A message that is
   question-shaped ("when should we schedule the retro?") and carries no second
   clause never routes to a mutation — it is a read, however many admin verbs
   it contains. Polite orders ("can you open sprint 2?") are not questions.
3. Mutation rules are gated on stored authority. For a requester with no cohort
   authority the match is kept but redirected to ``learner_support`` with the
   rule name suffixed ``_denied_role``, so the denial is observable and the
   learner-support specialist — which has no mutating tools at all — can say so.
   The tools re-check authority in code regardless of routing.
4. Every distinct route that matched is returned, de-duplicated by route, in
   rule order. Two or more routes make a multi-step plan. Nothing matched means
   ``general`` — the behaviour that worked before the supervisor existed.

The labelled sentences in ``routing_examples.py`` are the executable
specification of this table; extend both together.
"""

import re
from typing import (
    List,
    NamedTuple,
    Optional,
    Sequence,
)

from app.core.requester import RequesterContext
from app.schemas.graph import CapabilityRoute

# --- Vocabulary fragments ----------------------------------------------------

# Words that name a ceremony, reused across rules.
_CEREMONY = (
    r"(?:stand-?ups?|dailys?|dailies|plannings?|sprint\s+plannings?|reviews?|sprint\s+reviews?|"
    r"retros?(?:pectives?)?|demos?|q\s*&\s*a|q\s*and\s*a|qa\s+sessions?|office\s+hours|"
    r"ceremon(?:y|ies)|meetings?|sessions?)"
)
# Words that name a cohort role.
_ROLE_WORD = r"(?:learners?|students?|tech\s*-?leads?|scrum\s*-?masters?|ops\s*-?support|ops|operations)"
# Up to three words between a verb and its object noun ("open a new sprint", "start Backend-01's next sprint").
_GAP3 = r"(?:\s+[\w'@&./-]+){0,3}?\s+"
# Verbs that make a scheduling sentence an order rather than a description. They must not
# follow a determiner: "what's the schedule for the retro" describes, "schedule the retro" orders.
_NOT_AFTER_DETERMINER = (
    r"(?<!\bthe\s)(?<!\ba\s)(?<!\ban\s)(?<!\bour\s)(?<!\bmy\s)(?<!\byour\s)(?<!\bthis\s)(?<!\bthat\s)"
    r"(?<!\bof\s)(?<!\bon\s)(?<!\bfor\s)(?<!\bin\s)(?<!'s\s)(?<!\bits\s)(?<!\bthe\s\s)"
)

# Words that name the conversation itself rather than its subject. A question
# only counts as being ABOUT the discussion when one of these appears, which is
# what keeps "summarise this report" with the report.
_DISCUSSION_WORD = (
    r"\b(?:discussion|conversation|thread|chat|messages?|channel|standup\s+notes|above|"
    r"what\s+(?:was|we(?:'ve| have)?)\s+said)\b"
)
_DISCUSSION_WORD_AR = (
    r"(?:الكلام|الرسائل|النقاش|المناقشة|المحادثة|المحادثه|الحوار|الشات|الثريد|"
    r"الموضوع اللي فوق|اللي فوق|اللي اتقال|اللي حصل|كلامنا|كلامكم)"
)

# Leading chatter that says nothing about intent: greetings, mentions, punctuation.
_LEAD_IN = re.compile(
    r"^(?:\s*(?:hi|hey|hello|yo|ok|okay|so|please|kindly|hey\s+there|hi\s+there|@\S+|[,!.:\-–—])\s*)*",
    re.IGNORECASE,
)
# A message that opens with one of these is a question about the world, not an order to change it.
_INTERROGATIVE_OPENER = re.compile(
    r"^(?:when|what|which|where|who|whose|why|how|is|are|was|were|do|does|did|has|have|should)\b",
    re.IGNORECASE,
)
# A second clause can carry a second intent ("what's on this week and open sprint 2").
# A trailing "?" closes the question; a "?" with more text after it starts a second clause.
_CLAUSE_JOINER = re.compile(r"\b(?:and|then|also|plus|after\s+that)\b|;|\?\s*\S", re.IGNORECASE)
_APOSTROPHES = str.maketrans({"’": "'", "‘": "'", "`": "'"})


class Rule(NamedTuple):
    """One routing rule.

    Attributes:
        name: Stable identifier, exported as the ``matched_rule`` label.
        route: Where a match sends the turn.
        patterns: Any pattern matching means the rule matches.
        requires_cohort_authority: Gate on stored authority (superadmin or an admin role somewhere).
        denied_route: Where the turn goes when the gate fails.
        mutation: Whether the rule describes an order to change something. Mutations never
            match question-shaped single-clause messages.
        confidence: Reported confidence for a positive match.
    """

    name: str
    route: CapabilityRoute
    patterns: Sequence[re.Pattern[str]]
    requires_cohort_authority: bool = False
    denied_route: Optional[CapabilityRoute] = None
    mutation: bool = False
    confidence: float = 0.9


def _compile(*patterns: str) -> List[re.Pattern[str]]:
    return [re.compile(p, re.IGNORECASE) for p in patterns]


ROUTING_RULES: List[Rule] = [
    # ---- Back office: mutations, authority-gated ---------------------------------
    Rule(
        name="back_office_cohort",
        route=CapabilityRoute.BACK_OFFICE,
        patterns=_compile(
            rf"\b(?:create|open|start|set\s*up|make|add|new|register|launch|spin\s+up|deactivate|"
            rf"archive|close|reactivate|activate|rename|delete|remove|retire){_GAP3}cohorts?\b",
            r"\bcohorts?\b.{0,30}\b(?:create|created|set\s*up|deactivate|deactivated|archive|archived|"
            r"close|closed|retire|retired)\b",
        ),
        requires_cohort_authority=True,
        denied_route=CapabilityRoute.LEARNER_SUPPORT,
        mutation=True,
        confidence=0.95,
    ),
    Rule(
        name="back_office_role",
        route=CapabilityRoute.BACK_OFFICE,
        patterns=_compile(
            rf"\b(?:assign|give|grant|promote|demote|make|set|change|update|switch|remove|revoke|appoint|"
            rf"add|enrol|enroll|put|register|upgrade|downgrade|name)\b.{{0,60}}\b(?:"
            rf"(?:as|to|into)\s+(?:a\s+|an\s+|the\s+)?{_ROLE_WORD}\b"
            rf"|(?:the\s+|a\s+|an\s+)?{_ROLE_WORD}\s+(?:of|in|for|on|role)\b"
            rf"|{_ROLE_WORD}\s*(?:[.!?,]|$)"
            rf"|roles?\b)",
            rf"\broles?\b.{{0,30}}\b(?:should\s+be|is\s+now|becomes?|to|->|:)\s*(?:a\s+|an\s+|the\s+)?{_ROLE_WORD}\b",
        ),
        requires_cohort_authority=True,
        denied_route=CapabilityRoute.LEARNER_SUPPORT,
        mutation=True,
        confidence=0.95,
    ),
    Rule(
        name="back_office_sprint",
        route=CapabilityRoute.BACK_OFFICE,
        patterns=_compile(
            rf"\b(?:open|start|create|begin|kick\s*off|launch|close|end|complete|finish|wrap\s+up|"
            rf"extend|mark|reopen){_GAP3}sprints?\b",
        ),
        requires_cohort_authority=True,
        denied_route=CapabilityRoute.LEARNER_SUPPORT,
        mutation=True,
        confidence=0.95,
    ),
    Rule(
        name="back_office_schedule",
        route=CapabilityRoute.BACK_OFFICE,
        patterns=_compile(
            rf"{_NOT_AFTER_DETERMINER}\b(?:schedul(?:e|ing)|book|set\s*up|plan|arrange|organi[sz]e|put|add|"
            rf"create|hold|host|fix)\b.{{0,80}}\b{_CEREMONY}\b",
            rf"\b(?:move|reschedule|postpone|shift|bring\s+forward|delay|cancel|amend|update|change|edit|"
            rf"rename|push|drop|call\s+off|scrap|extend|shorten|reorganise|reorganize)\b.{{0,80}}\b{_CEREMONY}\b",
            rf"\b{_CEREMONY}\b.{{0,40}}\b(?:schedule|book|reschedule|move|cancel|postpone|shift|amend|update|"
            rf"change|push)\s+(?:it|them|this|that|one)\b",
            r"\b(?:update|change|set|add|amend|edit|put|attach|replace|revise)\b.{0,40}\bagenda\b",
            r"\bagenda\b.{0,40}(?:\bshould\s+be\b|\bis\s+now\b|\bto\s+be\b|:|=|->)",
        ),
        requires_cohort_authority=True,
        denied_route=CapabilityRoute.LEARNER_SUPPORT,
        mutation=True,
        confidence=0.95,
    ),
    # ---- Directory reads: who is in what. Open to any member (the read tools
    # refuse non-members in code), so they route to learner support ---------------
    Rule(
        name="cohort_directory",
        route=CapabilityRoute.LEARNER_SUPPORT,
        patterns=_compile(
            r"\b(?:list|show|see|view|display)\b(?:\s+(?:me|us|all|the|every|active|our|current))*"
            r"\s+(?:cohorts?|members?|people|learners|roster)\b",
            rf"\bwho(?:'s|\s+is|\s+are|\s+belongs)\b(?!\s+my\b)(?!\s+our\b).{{0,30}}\b(?:cohort|{_ROLE_WORD}|members?)\b",
            r"\b(?:members?|people|learners|roster)\s+(?:of|in)\s+(?:the\s+)?cohort\b",
            r"\b(?:which|what|how\s+many)\s+cohorts\b",
        ),
        confidence=0.85,
    ),
    # ---- Learner support: calendar reads, open to any member --------------------
    Rule(
        name="learner_calendar",
        route=CapabilityRoute.LEARNER_SUPPORT,
        patterns=_compile(
            rf"\b(?:when|what\s+time|what\s+day|which\s+day|is\s+there|are\s+there|do\s+we\s+have|"
            rf"will\s+there\s+be|any)\b.{{0,60}}\b(?:scheduled|upcoming|next|calendar|schedule|{_CEREMONY}|sprints?)\b",
            r"\b(?:what|which)\b.{0,30}\b(?:scheduled|upcoming|on\s+the\s+calendar|ceremonies)\b",
            r"\bwhat'?s\s+(?:on|happening|coming\s+up|planned|scheduled|next|the\s+(?:plan|schedule|calendar|agenda))\b",
            rf"\b(?:show|list|see|view|tell\s+me|give\s+me|remind\s+me|check|pull\s+up)\b.{{0,40}}"
            rf"\b(?:calendar|schedule|ceremonies|upcoming|agenda|{_CEREMONY})\b",
            rf"\b{_CEREMONY}\b.{{0,30}}\b(?:when|what\s+time)\b",
            rf"\b(?:is|are)\s+(?:the|our|tomorrow's|today's)\s+{_CEREMONY}\b.{{0,30}}\b(?:cancelled|canceled|still\s+on|"
            rf"happening|on|moved|rescheduled)\b",
            rf"\b(?:did|have|has|was|were)\b.{{0,20}}\b(?:cancel|cancell?ed|move|moved|reschedule|rescheduled|"
            rf"schedule|scheduled|book|booked)\b.{{0,40}}\b{_CEREMONY}\b",
        ),
        confidence=0.9,
    ),
    # ---- Conversation context: questions about what was said --------------------
    # Before learner support so "what did we agree in the retro?" is answered from
    # the discussion rather than from cohort data. Every pattern needs a word that
    # names the conversation ("the discussion", "الكلام", "above") or an explicit
    # decision reference ("what did we agree"), so "summarise this document" and
    # "لخص الكتاب" stay with the attachment they are about.
    Rule(
        name="conversation_context",
        route=CapabilityRoute.CONVERSATION_CONTEXT,
        patterns=_compile(
            rf"\b(?:summar(?:ise|ize|y|ising|izing)|recap|tl;?dr|catch\s+(?:me\s+)?up)\b.{{0,40}}{_DISCUSSION_WORD}",
            rf"{_DISCUSSION_WORD}.{{0,30}}\b(?:summar(?:ise|ize|y)|recap|about|so\s+far)\b",
            r"\b(?:what|which|who)\b.{0,40}\b(?:we|they|everyone|the\s+team|you\s+all)\b.{0,25}"
            r"\b(?:agree|agreed|decide|decided|conclude|concluded|settle|settled|say|said)\b",
            r"\b(?:what|who)\b.{0,25}\b(?:was|were)\b.{0,20}\b(?:decided|agreed|discussed|said|concluded)\b",
            r"\bwhat(?:'?s| is| was)\s+the\s+(?:decision|conclusion|outcome|consensus|agreement)\b",
            r"\bwho\s+(?:said|suggested|proposed|raised|asked)\b",
            r"\bwhat\s+did\s+[\w@.'-]+\s+(?:say|mean|suggest|propose)\b",
            rf"\b(?:above|earlier|previous|preceding|last\s+few)\b.{{0,20}}{_DISCUSSION_WORD}",
            rf"{_DISCUSSION_WORD}\s+(?:above|so\s+far|up\s+there|before\s+this)\b",
            # Arabic. No \b anchors: the article and conjunctions attach to the
            # front of a word, so "والكلام" carries no boundary around the stem.
            rf"(?:لخص|لخّص|ملخص|تلخيص|اختصر|إختصر|راجع|لملم).{{0,30}}{_DISCUSSION_WORD_AR}",
            rf"{_DISCUSSION_WORD_AR}.{{0,25}}(?:لخص|لخّص|ملخص|تلخيص|إيه|ايه|فوق|السابق|اللي فات|قبل كده)",
            r"(?:اتفقنا|إتفقنا|قررنا|اتفقوا|قرروا|الاتفاق|القرار اللي|القرارات اللي|خلصنا على|استقرينا)",
            r"(?:مين قال|من قال|قال مين|مين اقترح|من اقترح|مين طلب)",
            r"(?:الكلام|الرسائل|النقاش|المحادثة|المحادثه|الحوار|الشات|الثريد)\s*(?:اللي\s*)?(?:فوق|قبل|السابق|فات)",
        ),
        confidence=0.88,
    ),
    # ---- Learner support: academic and process questions ------------------------
    Rule(
        name="learner_support",
        route=CapabilityRoute.LEARNER_SUPPORT,
        patterns=_compile(
            rf"\b(?:my|our)\b.{{0,20}}\b(?:role|cohort|sprint|standups?|schedule|calendar|team|{_ROLE_WORD})\b",
            r"\b(?:which|what)\b.{0,20}\b(?:cohort|sprint|role|team)\b.{0,20}\b(?:am\s+i|i'?m|are\s+we|do\s+i|is\s+mine)\b",
            r"\b(?:assignment|deadline|due\s+date|submission|submit|grade|grading|quiz|lecture|course|"
            r"curriculum|syllabus|module|exam|project|homework|feedback)\b",
            r"\b(?:policy|policies|allowed|permitted|rule|rules|guideline|guidelines|leave|holiday|vacation|"
            r"absence|absent|day\s+off|time\s+off|sick|late)\b",
            r"\b(?:blocked|blocker|stuck|struggling|confused|help\s+me|how\s+do\s+i|how\s+can\s+i|how\s+should\s+i|"
            r"where\s+do\s+i|where\s+can\s+i|who\s+do\s+i\s+ask|who\s+should\s+i\s+ask|who\s+can\s+i\s+ask|"
            r"can\s+i\s+get\s+help)\b",
            r"\b(?:what\s+is|what\s+are|what'?s|explain|meaning\s+of)\b.{0,30}\b(?:a\s+|an\s+|the\s+)?"
            r"(?:sprints?|stand-?ups?|retros?|retrospectives?|ceremon(?:y|ies)|cohorts?|scrum|plannings?|reviews?)\b",
            # Arabic. Routing that only knows English sends every Arabic
            # question to the general fallback, which has no cohort tools at
            # all — so the same vocabulary is matched in both languages.
            #
            # These patterns carry no \b anchors on purpose. Arabic attaches
            # its article and conjunctions to the front of a word and its
            # plurals to the back, so "الكوهورتس" is one token with no boundary
            # around the stem: \bكوهورت\b does not match it, which is exactly
            # how a request for cohort data first reached the renderer with no
            # data tools bound.
            r"(?:سبرنت|سبرينت|ستاندب|ريترو|اجتماع|سيشن|محاضر|كوهورت|دفعة|دفعات|فريقي|فريقنا|مهامي|مهمتي)",
            r"(?:سياس|غياب|إجاز|اجاز|تأخير|حضور)",
            r"(?:واجب|تسليم|تقييم|درج|امتحان|مشروع)",
            r"(?:إنجاز|انجاز|تقدم)",
            r"(?:عالق|متعطل|مشكلة|ساعدني|إزاي|ازاي|كيف|ليه|فين|إمتى|امتى)",
        ),
        confidence=0.85,
    ),
    # ---- Workspace administration: the pre-Sprint-1 Mattermost tools -------------
    Rule(
        name="workspace_admin",
        route=CapabilityRoute.GENERAL,
        patterns=_compile(
            r"\b(?:add|invite|join|put)\b.{0,80}\b(?:to|into|onto)\s+(?:the\s+|a\s+)?(?:\S+\s+)?teams?\b",
            r"\b(?:create|make|set\s*up|open|start|new)\b.{0,30}\bteams?\b",
            r"\bwelcome\s+(?:message|dm)\b",
            r"\b(?:remove|kick)\b.{0,40}\b(?:from\s+(?:the\s+)?team|user)\b",
        ),
        confidence=0.9,
    ),
    # ---- Rich media: LAST on purpose ---------------------------------------------
    # Rule order is precedence: the first match runs first and later matches
    # become the plan. A request like "show the sprint's ceremonies as a chart"
    # must read the data BEFORE it can draw it, so this rule sits at the end and
    # composition runs as the continuation that writes the final reply.
    Rule(
        name="rich_media",
        route=CapabilityRoute.RICH_MEDIA,
        patterns=_compile(
            # English
            r"\b(?:draw|sketch|diagram|flow-?chart|chart|graph|plot|visuali[sz]e|mind-?map|"
            r"timeline|gantt|burn-?down|infographic)\b",
            r"\b(?:generate|create|make|render|design)\b.{0,24}\b(?:an?\s+)?"
            r"(?:image|picture|illustration|poster|logo|avatar)\b",
            r"\b(?:interactive|calculator|simulator|estimator)\b",
            # Arabic. No \b anchors: the article and conjunctions attach to the
            # front of the word ("والرسمة") and plurals to the back.
            r"(?:^|\s|و|ف)(?:ا|إ|أ)رسم(?:لي|لنا|ها|ه)?",
            # The trailing lookahead is what keeps "الرسمية" (official) out of
            # this rule: Arabic has no word boundary, so "رسم" sits inside
            # perfectly ordinary words, and a request for "ساعات العمل الرسمية"
            # was being read as a request for a drawing.
            r"رسم(?:ة|ه|ات)?(?![\u0621-\u064a])\s*(?:بياني|توضيحي)?",
            r"(?:مخطط|تشارت|شارت|جراف|إنفوجرافيك)",
            r"(?:خريطة|خارطة)\s*(?:ذهنية|طريق)",
            r"(?:جدول|خط)\s*زمني",
            # Gated behind a generation verb so "بصورة عامة" ("generally") does
            # not read as a request for a picture.
            r"(?:ولّد|ولد|اعمل|إعمل|أنشئ|انشئ|صمم|صمّم|ارسم)\s+(?:لي\s+|لنا\s+)?(?:صورة|صوره)",
            r"صورة\s*(?:توضيحية|تعبيرية)",
            r"(?:حاسبة|آلة\s*حاسبة|واجهة تفاعلية|تفاعلي)",
        ),
        confidence=0.85,
    ),
]

FALLBACK_RULE = "general_fallback"
DENIED_SUFFIX = "_denied_role"


class RoutingResult(NamedTuple):
    """A routing decision: which route, how confident, and which rule decided."""

    route: CapabilityRoute
    matched_rule: str
    confidence: float


def normalise_text(text: str) -> str:
    """Collapse whitespace and straighten apostrophes so patterns see one shape.

    Args:
        text: Raw message text.

    Returns:
        str: The normalised text.
    """
    return " ".join(text.translate(_APOSTROPHES).split())


def is_question_shaped(text: str) -> bool:
    """Whether a message reads as a single question rather than an order.

    Leading greetings and mentions are skipped. A message that opens with an
    interrogative and has no second clause is a question; polite orders
    ("can you", "could you", "please") are not.

    Args:
        text: Normalised message text.

    Returns:
        bool: True for a single-clause question.
    """
    stripped = _LEAD_IN.sub("", text, count=1)
    if not _INTERROGATIVE_OPENER.match(stripped):
        return False
    return _CLAUSE_JOINER.search(stripped) is None


def _matches(rule: Rule, text: str) -> bool:
    return any(pattern.search(text) for pattern in rule.patterns)


def detect_intents(text: str, requester: Optional[RequesterContext] = None) -> List[RoutingResult]:
    """Return every distinct route a message calls for, in rule order.

    A message can legitimately span more than one route ("open sprint 2 and
    tell me when the retro is"). Every rule is checked and results are
    de-duplicated by route, so the supervisor can plan a multi-step turn.
    Always returns at least one result (``general`` when nothing matched).

    A mutation rule that matches for a requester without stored cohort
    authority is not silently routed to the back office: it becomes a
    learner-support result with ``matched_rule`` suffixed ``_denied_role`` so
    the denial is observable and the reply can say so plainly. The tools
    themselves re-check authority in code regardless of routing.

    Args:
        text: The message text.
        requester: The bound requester, or None for an anonymous turn.

    Returns:
        List[RoutingResult]: Ordered, de-duplicated by route.
    """
    normalised = normalise_text(text or "")
    results: List[RoutingResult] = []
    seen: set[CapabilityRoute] = set()
    has_authority = bool(requester and requester.has_any_cohort_authority())
    question = is_question_shaped(normalised)

    for rule in ROUTING_RULES:
        if rule.mutation and question:
            continue
        if not _matches(rule, normalised):
            continue
        if rule.requires_cohort_authority and not has_authority:
            result = RoutingResult(
                route=rule.denied_route or CapabilityRoute.GENERAL,
                matched_rule=f"{rule.name}{DENIED_SUFFIX}",
                confidence=0.3,
            )
        else:
            result = RoutingResult(route=rule.route, matched_rule=rule.name, confidence=rule.confidence)
        if result.route in seen:
            continue
        seen.add(result.route)
        results.append(result)

    if not results:
        results.append(RoutingResult(route=CapabilityRoute.GENERAL, matched_rule=FALLBACK_RULE, confidence=0.0))
    return results


def classify_text(text: str, requester: Optional[RequesterContext] = None) -> RoutingResult:
    """Classify a message into its single primary route (the first ``detect_intents`` result).

    Args:
        text: The message text.
        requester: The bound requester, or None.

    Returns:
        RoutingResult: The primary decision.
    """
    return detect_intents(text, requester)[0]
