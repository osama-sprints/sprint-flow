import asyncio
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


async def run_comprehensive_tests():
    print("==========================================================")
    print("      POLICY SUPPORT FEATURE: COMPLETE VERIFICATION TEST   ")
    print("==========================================================\n")

    print("--- Testing Message Content Extraction ---")
    human_msg = HumanMessage(content="What is the refund policy?")
    dict_msg = {"content": "What is the refund policy?"}
    str_msg = "What is the refund policy?"

    assert _extract_last_text([human_msg]) == "What is the refund policy?"
    assert _extract_last_text([dict_msg]) == "What is the refund policy?"
    assert _extract_last_text([str_msg]) == "What is the refund policy?"
    assert _extract_last_text([]) == ""
    print("   Helper function _extract_last_text PASSED.\n")

    print("---  Testing Routing Rules & Precedence ---")
    query = "what is the leave policy?"
    res = classify_text(query)
    assert res.route == CapabilityRoute.POLICY_SUPPORT
    assert res.matched_rule == "policy_support"
    print("   Routing Rules PASSED.\n")

    print("---  Testing Grounded Answer Retrieval ---")
    mock_docs = [
        {
            "document_id": "DOC-POL-001",
            "section_title": "Annual Leave Guidelines",
            "page_number": 4,
            "content": "Regular employees receive 21 days of paid leave per year.",
            "similarity_score": 0.89,
        }
    ]

    with patch("app.services.policy_retrieval.similarity_search", return_value=mock_docs):
        status, docs = await get_grounded_answer_or_refusal("what is the leave policy?", audience="learner")
        assert status == "grounded" and len(docs) == 1
        assert docs[0]["content"] == "Regular employees receive 21 days of paid leave per year."
        print("  - State 1 (Grounded & 'content' field): OK")

    with patch("app.services.policy_retrieval.similarity_search", return_value=[]):
        status, docs = await get_grounded_answer_or_refusal("how to cook pizza?", audience="learner")
        assert status == "no_match" and len(docs) == 0
        print("  - State 2 (No Match): OK")

    with patch("app.services.policy_retrieval.similarity_search", side_effect=Exception("DB Error")):
        status, docs = await get_grounded_answer_or_refusal("what is the leave policy?", audience="learner")
        assert status == "error" and len(docs) == 0
        print("  - State 3 (Error Handling): OK")

    print("   Retrieval Service PASSED.\n")

    print("--- Testing Policy Retrieval Node Security & Escalation ---")

    state_with_human_message = {"messages": [HumanMessage(content="What is the leave policy?")]}

    learner_requester = MagicMock(spec=RequesterContext)
    learner_requester.is_superadmin = False
    learner_requester.has_any_cohort_authority.return_value = False

    with (
        patch("app.core.langgraph.nodes.current_requester") as mock_ctx,
        patch("app.core.langgraph.nodes.get_grounded_answer_or_refusal", new_callable=AsyncMock) as mock_ret,
    ):
        mock_ctx.get.return_value = learner_requester
        mock_ret.return_value = ("grounded", mock_docs)

        cmd = await policy_retrieval_node(state_with_human_message)

        mock_ret.assert_called_once_with(query="What is the leave policy?", audience="learner")
        assert cmd.goto == "policy_support_llm_node"
        assert "21 days of paid leave" in cmd.update["policy_context"]
        print("  - Branch A (Learner Security & Content Check): OK")

    admin_requester = MagicMock(spec=RequesterContext)
    admin_requester.is_superadmin = True

    with (
        patch("app.core.langgraph.nodes.current_requester") as mock_ctx,
        patch("app.core.langgraph.nodes.get_grounded_answer_or_refusal", new_callable=AsyncMock) as mock_ret,
    ):
        mock_ctx.get.return_value = admin_requester
        mock_ret.return_value = ("grounded", mock_docs)

        cmd = await policy_retrieval_node(state_with_human_message)

        mock_ret.assert_called_once_with(query="What is the leave policy?", audience=None)
        print("  - Branch B (Admin Security Privilege): OK")

    with (
        patch("app.core.langgraph.nodes.current_requester") as mock_ctx,
        patch("app.core.langgraph.nodes.get_grounded_answer_or_refusal", new_callable=AsyncMock) as mock_ret,
        patch("app.core.langgraph.nodes.open_escalation", new_callable=AsyncMock) as mock_esc,
    ):
        mock_ctx.get.return_value = learner_requester
        mock_ret.return_value = ("no_match", [])

        mock_result = MagicMock()
        mock_result.message = "Ticket ESC-OPS-101 created successfully."
        mock_esc.return_value = mock_result

        cmd = await policy_retrieval_node(state_with_human_message)

        mock_esc.assert_called_once_with(
            question="What is the leave policy?", ticket_type=EscalationType.OPS, requester=learner_requester
        )

        assert cmd.goto in ("__end__", "END")

        returned_msg = cmd.update["messages"][0]
        assert isinstance(returned_msg, AIMessage)
        assert returned_msg.content == "Ticket ESC-OPS-101 created successfully."
        print("  - Branch C (Escalation Contract & AIMessage Wrapping): OK")

    print("   Policy Retrieval Node PASSED.\n")

    print("--- Testing Graph Registration & Specialists ---")
    assert CapabilityRoute.POLICY_SUPPORT.value in specialists_module.SPECIALISTS
    assert hasattr(graph_module, "policy_retrieval_node")
    print("   Graph Registration PASSED.\n")

    print("==========================================================")
    print("  ALL COMPREHENSIVE TESTS PASSED SUCCESSFULLY!          ")
    print("==========================================================")


if __name__ == "__main__":
    asyncio.run(run_comprehensive_tests())
