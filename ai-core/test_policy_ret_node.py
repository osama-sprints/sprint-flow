import asyncio
from unittest.mock import patch, AsyncMock
from app.core.langgraph.nodes import policy_retrieval_node


async def run_task4_tests():
    print("--- Starting Tests (Policy Retrieval Node) ---\n")

    dummy_state = {
        "messages": [AsyncMock(content="What is the leave policy?")],
        "current_requester": AsyncMock(role="learner"),
    }

    # 1. Grounded Test -> Must route to policy_support_llm_node
    mock_docs = [
        {
            "document_id": "DOC-101",
            "section_title": "Leave Policy",
            "page_number": 2,
            "text": "Paid leave is 21 days.",
            "similarity_score": 0.85,
        }
    ]
    with patch("app.core.langgraph.nodes.get_grounded_answer_or_refusal", new_callable=AsyncMock) as mock_ret:
        mock_ret.return_value = ("grounded", mock_docs)
        cmd = await policy_retrieval_node(dummy_state)
        print(f"1. Grounded Node Test -> Target node: {cmd.goto}")
        assert cmd.goto == "policy_support_llm_node"
        assert "DOC-101" in cmd.update["policy_context"]

    # 2. Refusal/Escalation Test -> Must trigger open_escalation and route to END
    with (
        patch("app.core.langgraph.nodes.get_grounded_answer_or_refusal", new_callable=AsyncMock) as mock_ret,
        patch("app.core.langgraph.nodes.create_escalation_ticket", new_callable=AsyncMock) as mock_esc,
    ):
        mock_ret.return_value = ("no_match", [])
        mock_esc.return_value = {"ticket_id": "ESC-999"}

        cmd = await policy_retrieval_node(dummy_state)
        print(f"2. Refusal Node Test  -> Target node: {cmd.goto}")
        assert cmd.goto in ("__end__", "END")
        assert "ESC-999" in cmd.update["messages"][0]

    print("\nTask policy retrieval node tests passed successfully.")


if __name__ == "__main__":
    asyncio.run(run_task4_tests())
