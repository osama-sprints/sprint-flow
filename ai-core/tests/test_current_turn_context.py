"""The person's message and this turn's tool results always reach the model.

Regression for the failed turn where a book was attached with the request
"هاتلي ملخص 20 سطر للكتاب ده بيتكلم عن ايه": after the agent read pages, the
history trim dropped the oversized tool results together with the person's
message, the model saw only the system prompt, and answered with a greeting.
"""

from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    ToolMessage,
)
from langchain_core.tools import tool

from langgraph.checkpoint.memory import MemorySaver

from app.core import i18n
from app.core.config import settings
from app.core.langgraph.graph import LangGraphAgent
from app.core.langgraph.routing_rules import classify_text
from app.core.langgraph.specialists import specialist_for
from app.core.langgraph.tools import TOOL_GROUPS
from app.core.requester import RequesterContext
from app.utils.graph import (
    prepare_messages,
    was_cut_short,
)

REQUEST = "هاتلي ملخص 20 سطر للكتاب ده بيتكلم عن ايه\n\n[Attachments: agentic ai.pdf (PDF, 34 pages, id iurf8n7b)]"


def _history(pairs: int) -> list:
    messages = []
    for n in range(pairs):
        messages.append(HumanMessage(content=f"Earlier question {n} " + "filler " * 40))
        messages.append(AIMessage(content=f"Earlier answer {n} " + "filler " * 40))
    return messages


def _kinds(prepared) -> list[str]:
    return [getattr(m, "type", None) or getattr(m, "role", "") for m in prepared]


def test_the_current_turn_survives_the_history_trim_whole(monkeypatch):
    monkeypatch.setattr(settings, "MAX_HISTORY_TOKENS", 6000)
    tool_call = {
        "name": "read_pdf_pages",
        "args": {"document_id": "iurf8n7b", "start_page": 2, "end_page": 10},
        "id": "c1",
    }
    current = [
        HumanMessage(content=REQUEST),
        AIMessage(content="", tool_calls=[tool_call]),
        ToolMessage(content="[PDF_PAGES] " + ("page text " * 6000), tool_call_id="c1", name="read_pdf_pages"),
    ]
    prepared = prepare_messages(_history(60) + current, "system prompt")  # type: ignore[arg-type]

    kinds = _kinds(prepared)
    assert kinds[0] == "system"
    assert kinds[-3:] == ["human", "ai", "tool"], kinds[-5:]
    assert REQUEST in str(prepared[-3].content)
    assert len(str(prepared[-1].content)) > 50_000  # the tool result was not shortened
    # Older history was trimmed, and what remains starts on a human message.
    assert len(prepared) < 60 * 2 + 4 and kinds[1] == "human"


def test_a_short_turn_keeps_as_much_history_as_the_budget_allows(monkeypatch):
    monkeypatch.setattr(settings, "MAX_HISTORY_TOKENS", 6000)
    prepared = prepare_messages(_history(3) + [HumanMessage(content=REQUEST)], "system prompt")  # type: ignore[arg-type]
    assert _kinds(prepared) == ["system"] + ["human", "ai"] * 3 + ["human"]


def test_a_turn_with_no_human_message_is_left_to_the_ordinary_trim(monkeypatch):
    monkeypatch.setattr(settings, "MAX_HISTORY_TOKENS", 6000)
    prepared = prepare_messages([AIMessage(content="orphan")], "system prompt")  # type: ignore[arg-type]
    assert _kinds(prepared)[0] == "system"


def test_colloquial_arabic_with_an_attachment_reaches_a_specialist_that_can_read_pdfs():
    result = classify_text(REQUEST)
    spec = specialist_for(result.route.value)
    names = {tool.name for tool in TOOL_GROUPS[spec.tool_group]}
    assert {"inspect_pdf", "search_pdf", "read_pdf_pages", "ask_pdf_pages"} <= names, (result.route, spec.tool_group)


def test_a_reply_stopped_at_the_completion_ceiling_is_recognised():
    """The failed turn's final generation: 1,996 completion tokens, 1,917 of them reasoning, 79 visible."""
    cut = AIMessage(content="أهلاً بك! لقد قمت بقراءة كتاب", response_metadata={"finish_reason": "length"})
    assert was_cut_short(cut) is True
    complete = AIMessage(content="…", response_metadata={"finish_reason": "stop"})
    assert was_cut_short(complete) is False
    more_work = AIMessage(
        content="",
        tool_calls=[{"name": "read_pdf_pages", "args": {}, "id": "c"}],
        response_metadata={"finish_reason": "length"},
    )
    assert was_cut_short(more_work) is False
    assert was_cut_short(AIMessage(content="no metadata")) is False


# ---------------------------------------------------------------------------
# The turn is kept whole, but not unbounded
# ---------------------------------------------------------------------------


def _turn_with_reads(pages_per_read: int, reads: int) -> list:
    """A turn like a book summary: the request, then read after read."""
    messages: list = [HumanMessage(content=REQUEST)]
    for n in range(reads):
        call = {"name": "read_pdf_pages", "args": {"document_id": "iurf8n7b", "start_page": n * 12 + 1}, "id": f"c{n}"}
        head = (
            f"[PDF_PAGES] agentic ai.pdf (id iurf8n7b) — pages {n * 12 + 1}–{n * 12 + 12} of 400; served 12 page(s).\n"
        )
        messages.append(AIMessage(content="", tool_calls=[call]))
        messages.append(
            ToolMessage(content=head + ("page text " * pages_per_read), tool_call_id=f"c{n}", name="read_pdf_pages")
        )
    return messages


def _pairing_is_valid(prepared) -> bool:
    """Every tool result answers a tool call that precedes it, and none is orphaned."""
    open_calls: set[str] = set()
    for message in prepared:
        for call in getattr(message, "tool_calls", None) or []:
            open_calls.add(call["id"])
        call_id = getattr(message, "tool_call_id", None)
        if call_id is not None:
            if call_id not in open_calls:
                return False
            open_calls.discard(call_id)
    return True


def test_an_enormous_turn_is_bounded_by_shortening_results_not_by_dropping_them(monkeypatch):
    monkeypatch.setattr(settings, "MAX_HISTORY_TOKENS", 6000)
    # Tight enough that one pass over the results is not enough, so the
    # shrinking has to work through them oldest first.
    monkeypatch.setattr(settings, "MAX_TURN_TOKENS", 8_000)
    turn = _turn_with_reads(pages_per_read=6000, reads=8)  # ~120k tokens of results
    prepared = prepare_messages(_history(20) + turn, "system prompt")  # type: ignore[arg-type]

    body = prepared[1:]
    assert _kinds(body).count("human") >= 1 and _pairing_is_valid(body)
    # Nothing was removed from the turn, and the request is verbatim.
    assert _kinds(body)[-len(turn) :] == ["human"] + ["ai", "tool"] * 8
    assert str(body[-len(turn)].content) == REQUEST
    from app.utils.graph import _count_tokens_tiktoken, dump_messages

    assert _count_tokens_tiktoken(dump_messages(body[-len(turn) :])) <= settings.MAX_TURN_TOKENS

    results = [m for m in body if getattr(m, "tool_call_id", None)]
    shortened = [m for m in results if "shortened to fit this turn" in str(m.content)]
    assert shortened, "no result was shortened"
    # A shortened result still names its subject and says how to read the rest.
    assert "agentic ai.pdf (id iurf8n7b)" in str(shortened[0].content)
    assert "Call the same tool again" in str(shortened[0].content)
    # Oldest first: a newer result is never shortened harder than an older one.
    lengths = [len(str(m.content)) for m in results]
    assert lengths == sorted(lengths)


def test_only_as_much_is_shortened_as_the_budget_needs(monkeypatch):
    """One huge old result is enough to fit the turn, so the newest is left whole."""
    monkeypatch.setattr(settings, "MAX_HISTORY_TOKENS", 6000)
    monkeypatch.setattr(settings, "MAX_TURN_TOKENS", 6_000)
    turn: list = [HumanMessage(content=REQUEST)]
    for index, size in enumerate((6000, 200)):
        turn.append(AIMessage(content="", tool_calls=[{"name": "read_pdf_pages", "args": {}, "id": f"c{index}"}]))
        turn.append(ToolMessage(content="page text " * size, tool_call_id=f"c{index}", name="read_pdf_pages"))

    prepared = prepare_messages(turn, "system prompt")  # type: ignore[arg-type]
    results = [m for m in prepared if getattr(m, "tool_call_id", None)]
    assert "shortened to fit this turn" in str(results[0].content)
    assert str(results[1].content) == "page text " * 200  # untouched, byte for byte


def test_a_turn_inside_the_ceiling_is_untouched(monkeypatch):
    monkeypatch.setattr(settings, "MAX_HISTORY_TOKENS", 6000)
    monkeypatch.setattr(settings, "MAX_TURN_TOKENS", 48_000)
    turn = _turn_with_reads(pages_per_read=200, reads=2)
    prepared = prepare_messages(turn, "system prompt")  # type: ignore[arg-type]
    assert all("shortened to fit this turn" not in str(m.content) for m in prepared)


# ---------------------------------------------------------------------------
# The length retry: no tools, no second side effect, a bounded ceiling
# ---------------------------------------------------------------------------


class RecordingLLM:
    """Returns a scripted reply per call and records what each call was given."""

    def __init__(self, script):
        self.script = list(script)
        self.calls: list[dict] = []

    def bind_tools(self, tools):
        return self

    def get_llm(self):
        return None

    async def call(self, messages, tools=None, **kwargs):
        self.calls.append({"tools": [getattr(t, "name", t) for t in (tools or [])], **kwargs})
        return self.script.pop(0) if self.script else AIMessage(content="ok")


def _cut(content: str) -> AIMessage:
    return AIMessage(content=content, response_metadata={"finish_reason": "length"})


async def _run_one_turn(agent, text: str):
    graph = agent.build_graph(MemorySaver())
    token = current_requester_token = __import__(
        "app.core.requester", fromlist=["current_requester"]
    ).current_requester
    reset = current_requester_token.set(
        RequesterContext(mattermost_user_id="u1", username="tester", channel_id="c1", channel_type="D")
    )
    try:
        return await graph.ainvoke(
            {"messages": [HumanMessage(content=text)], "long_term_memory": ""},
            {"configurable": {"thread_id": "t-cut"}, "metadata": {"username": "tester"}},
        )
    finally:
        current_requester_token.reset(reset)
        del token


async def test_a_cut_reply_is_retried_without_tools_and_within_the_configured_ceiling(monkeypatch):
    monkeypatch.setattr(settings, "MAX_TOKENS", 2000)
    monkeypatch.setattr(settings, "MAX_TOKENS_RETRY", 32768)
    called: list[str] = []

    @tool
    async def duckduckgo_search(query: str) -> str:
        """Search the web (fake, records the call)."""
        called.append(query)
        return "[OK] results"

    llm = RecordingLLM([_cut("أهلاً بك! لقد قمت بقراءة كتاب"), AIMessage(content="ملخص كامل في 20 سطرًا …")])
    agent = LangGraphAgent(llm=llm, tool_groups={"general": [duckduckgo_search]})
    state = await _run_one_turn(agent, "summarise the attached book in 20 lines")

    assert len(llm.calls) == 2, llm.calls
    assert llm.calls[0]["tools"] == ["duckduckgo_search"]
    # The retry binds no tool at all, so no tool can run a second time.
    assert llm.calls[1]["tools"] == [] and called == []
    assert llm.calls[1]["max_completion_tokens"] == 32768
    assert str(state["messages"][-1].content) == "ملخص كامل في 20 سطرًا …"


async def test_a_reply_still_cut_after_the_retry_says_so_in_the_persons_language(monkeypatch):
    monkeypatch.setattr(settings, "MAX_TOKENS", 2000)
    monkeypatch.setattr(settings, "MAX_TOKENS_RETRY", 32768)
    llm = RecordingLLM([_cut("نصف الإجابة"), _cut("نصف الإجابة الأطول")])
    agent = LangGraphAgent(llm=llm, tool_groups={"general": []})
    token = i18n.current_language.set("ar")
    try:
        state = await _run_one_turn(agent, "لخّص الكتاب")
    finally:
        i18n.current_language.reset(token)
    assert str(state["messages"][-1].content).endswith("_(تم اختصار الرد بسبب حد الطول.)_")


async def test_no_retry_is_paid_for_when_the_ceiling_cannot_widen(monkeypatch):
    monkeypatch.setattr(settings, "MAX_TOKENS", 32768)
    monkeypatch.setattr(settings, "MAX_TOKENS_RETRY", 8192)
    llm = RecordingLLM([_cut("half an answer")])
    agent = LangGraphAgent(llm=llm, tool_groups={"general": []})
    state = await _run_one_turn(agent, "summarise the attached book")
    assert len(llm.calls) == 1  # widening is impossible, so nothing is spent
    assert str(state["messages"][-1].content).endswith("_(The reply was cut short by the length limit.)_")
