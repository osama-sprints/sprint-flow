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

from app.core.config import settings
from app.core.langgraph.routing_rules import classify_text
from app.core.langgraph.specialists import specialist_for
from app.core.langgraph.tools import TOOL_GROUPS
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
