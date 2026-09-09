import pytest
from unittest.mock import patch, AsyncMock, MagicMock
from langchain_core.messages import HumanMessage, AIMessage

from app.schemas.graph import CapabilityRoute
from app.core.langgraph.routing_rules import classify_text
from app.services.policy_retrieval import get_grounded_answer_or_refusal
from app.core.langgraph.nodes import policy_retrieval_node, _extract_last_text
from app.services.domain.escalations import EscalationType
from app.core.requester import RequesterContext
import app.core.langgraph.specialists as specialists_module
import app.core.langgraph.graph as graph_module


def test_extract_last_text():
    human_msg = HumanMessage(content="What is the refund policy?")
    dict_msg = {"content": "What is the refund policy?"}
    str_msg = "What is the refund policy?"

    assert _extract_last_text([human_msg]) == "What is the refund policy?"
    assert _extract_last_text([dict_msg]) == "What is the refund policy?"
    assert _extract_last_text([str_msg]) == "What is the refund policy?"
    assert _extract_last_text([]) == ""


def test_routing_rules():
    query = "what is the leave policy?"
    res = classify_text(query)
    assert res.route == CapabilityRoute.POLICY_SUPPORT
    assert res.matched_rule == "policy_support"


@pytest.mark.asyncio
async def test_policy_retrieval_service():
    mock_docs = [{
        "document_id": "DOC-POL-001",
        "section_title": "Annual Leave Guidelines",
        "page_number": 4,
        "content": "Regular employees receive 21 days of paid leave per year.",
        "similarity_score": 0.89
    }]

    with patch("app.services.policy_retrieval.similarity_search", return_value=mock_docs):
        status, docs = await get_grounded_answer_or_refusal("what is the leave policy?", audience="learner")
        assert status == "grounded" and len(docs) == 1
        assert docs[0]["content"] == "Regular employees receive 21 days of paid leave per year."

    with patch("app.services.policy_retrieval.similarity_search", return_value=[]):
        status, docs = await get_grounded_answer_or_refusal("how to cook pizza?", audience="learner")
        assert status == "no_match" and len(docs) == 0

    with patch("app.services.policy_retrieval.similarity_search", side_effect=Exception("DB Error")):
        status, docs = await get_grounded_answer_or_refusal("what is the leave policy?", audience="learner")
        assert status == "error" and len(docs) == 0


@pytest.mark.asyncio
async def test_policy_retrieval_node_security_and_escalation():
    state_with_human_message = {
        "messages": [HumanMessage(content="What is the leave policy?")]
    }

    mock_docs = [{
        "document_id": "DOC-POL-001",
        "section_title": "Annual Leave Guidelines",
        "page_number": 4,
        "content": "Regular employees receive 21 days of paid leave per year.",
        "similarity_score": 0.89
    }]

    # Case A: Learner Role (Audience = 'learner')
    learner_requester = MagicMock(spec=RequesterContext)
    learner_requester.is_superadmin = False
    learner_requester.has_any_channel_authority.return_value = False

    with patch("app.core.langgraph.nodes.current_requester") as mock_ctx, \
         patch("app.core.langgraph.nodes.get_grounded_answer_or_refusal", new_callable=AsyncMock) as mock_ret:

        mock_ctx.get.return_value = learner_requester
        mock_ret.return_value = ("grounded", mock_docs)

        cmd = await policy_retrieval_node(state_with_human_message)
        mock_ret.assert_called_once_with(query="What is the leave policy?", audience="learner")
        assert cmd.goto == "policy_support_llm_node"
        assert "21 days of paid leave" in cmd.update["policy_context"]

    # Case B: Admin / Operator Role (Audience = None)
    admin_requester = MagicMock(spec=RequesterContext)
    admin_requester.is_superadmin = True

    with patch("app.core.langgraph.nodes.current_requester") as mock_ctx, \
         patch("app.core.langgraph.nodes.get_grounded_answer_or_refusal", new_callable=AsyncMock) as mock_ret:

        mock_ctx.get.return_value = admin_requester
        mock_ret.return_value = ("grounded", mock_docs)

        cmd = await policy_retrieval_node(state_with_human_message)
        mock_ret.assert_called_once_with(query="What is the leave policy?", audience=None)

    # Case C: No Match -> Escalation Contract & AIMessage wrapping
    with patch("app.core.langgraph.nodes.current_requester") as mock_ctx, \
         patch("app.core.langgraph.nodes.get_grounded_answer_or_refusal", new_callable=AsyncMock) as mock_ret, \
         patch("app.core.langgraph.nodes.open_escalation", new_callable=AsyncMock) as mock_esc:

        mock_ctx.get.return_value = learner_requester
        mock_ret.return_value = ("no_match", [])

        mock_result = MagicMock()
        mock_result.message = "Ticket ESC-OPS-101 created successfully."
        mock_esc.return_value = mock_result

        cmd = await policy_retrieval_node(state_with_human_message)

        mock_esc.assert_called_once_with(
            question="What is the leave policy?",
            ticket_type=EscalationType.OPS,
            requester=learner_requester
        )

        assert cmd.goto in ("__end__", "END")

        returned_msg = cmd.update["messages"][0]
        assert isinstance(returned_msg, AIMessage)
        assert returned_msg.content == "Ticket ESC-OPS-101 created successfully."


def test_specialists_and_graph_registration():
    assert CapabilityRoute.POLICY_SUPPORT.value in specialists_module.SPECIALISTS
    assert hasattr(graph_module, "policy_retrieval_node")