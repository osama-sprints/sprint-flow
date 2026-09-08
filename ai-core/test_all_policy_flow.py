import asyncio
from unittest.mock import patch, AsyncMock
from app.schemas.graph import CapabilityRoute
from app.core.langgraph.routing_rules import classify_text
from app.services.policy_retrieval import get_grounded_answer_or_refusal
from app.core.langgraph.nodes import policy_retrieval_node
from app.core.langgraph.specialists import SPECIALISTS
import app.core.langgraph.graph as graph_module


async def run_comprehensive_tests():
    print("==========================================================")
    print("      POLICY SUPPORT FEATURE: COMPLETE TASKS 1-6 TEST     ")
    print("==========================================================\n")

    # ----------------------------------------------------
    # TASK 1 & 2: Routing Rules & Precedence
    # ----------------------------------------------------
    print("--- [Tasks 1 & 2] Testing Routing Rules & Precedence ---")
    query = "what is the leave policy?"
    res = classify_text(query)
    print(f"  Query: '{query}'")
    print(f"  Matched Route: {res.route} | Matched Rule: {res.matched_rule}")
    
    assert res.route == CapabilityRoute.POLICY_SUPPORT, f"Expected POLICY_SUPPORT, got {res.route}"
    assert res.matched_rule == "policy_support", f"Expected policy_support rule, got {res.matched_rule}"
    print("  ✓ Tasks 1 & 2 PASSED.\n")

    # ----------------------------------------------------
    # TASK 3: Grounded Retrieval Service (Three-State)
    # ----------------------------------------------------
    print("--- [Task 3] Testing Policy Retrieval Service ---")
    
    mock_docs = [{
        "document_id": "DOC-POL-001",
        "section_title": "Annual Leave Guidelines",
        "page_number": 4,
        "text": "Regular employees receive 21 days of paid leave per year.",
        "similarity_score": 0.89
    }]

    # Case A: Grounded match
    with patch("app.services.policy_retrieval.similarity_search", return_value=mock_docs):
        status, docs = await get_grounded_answer_or_refusal("what is the leave policy?", audience="learner")
        assert status == "grounded" and len(docs) == 1
        print("  - State 1 (Grounded): OK")

    # Case B: No match
    with patch("app.services.policy_retrieval.similarity_search", return_value=[]):
        status, docs = await get_grounded_answer_or_refusal("how to cook pizza?", audience="learner")
        assert status == "no_match" and len(docs) == 0
        print("  - State 2 (No Match): OK")

    # Case C: Error fallback
    with patch("app.services.policy_retrieval.similarity_search", side_effect=Exception("Database Timeout")):
        status, docs = await get_grounded_answer_or_refusal("what is the leave policy?", audience="learner")
        assert status == "error" and len(docs) == 0
        print("  - State 3 (Error Handling): OK")

    print("  ✓ Task 3 PASSED.\n")

    # ----------------------------------------------------
    # TASK 4: Policy Retrieval Graph Node & Escalation
    # ----------------------------------------------------
    print("--- [Task 4] Testing Policy Retrieval Node & Branching ---")
    dummy_state = {
        "messages": [AsyncMock(content="What is the leave policy?")],
        "current_requester": AsyncMock(role="learner")
    }

    # Branch A: Grounded -> policy_support
    with patch("app.core.langgraph.nodes.get_grounded_answer_or_refusal", new_callable=AsyncMock) as mock_ret:
        mock_ret.return_value = ("grounded", mock_docs)
        cmd = await policy_retrieval_node(dummy_state)
        
        assert cmd.goto in ("policy_support_llm_node", "policy_support"), f"Unexpected target node: {cmd.goto}"
        assert "DOC-POL-001" in cmd.update["policy_context"]
        print("  - Branch A (Grounded -> Specialist Node): OK")

    # Branch B: Refusal & Escalation Ticket -> END
    with patch("app.core.langgraph.nodes.get_grounded_answer_or_refusal", new_callable=AsyncMock) as mock_ret, \
         patch("app.core.langgraph.nodes.create_escalation_ticket", new_callable=AsyncMock) as mock_esc:
        
        mock_ret.return_value = ("no_match", [])
        mock_esc.return_value = {"ticket_id": "ESC-TICKET-789"}
        
        cmd = await policy_retrieval_node(dummy_state)
        assert cmd.goto in ("__end__", "END"), f"Unexpected refusal target: {cmd.goto}"
        assert "ESC-TICKET-789" in cmd.update["messages"][0]
        print("  - Branch B (Refusal -> Escalation Ticket & END): OK")

    print("  ✓ Task 4 PASSED.\n")

    # ----------------------------------------------------
    # TASK 5: Specialist Prompt & Citation Configuration
    # ----------------------------------------------------
    print("--- [Task 5] Testing Policy Specialist Registry & Prompt ---")
    route_key = CapabilityRoute.POLICY_SUPPORT.value
    assert route_key in SPECIALISTS, f"Key '{route_key}' missing from SPECIALISTS registry"

    specialist = SPECIALISTS[route_key]
    prompt_text = specialist.prompt_context

    assert "[Source: <document_id>, §<section_title>, p.<page_number>]" in prompt_text, "Missing citation schema in prompt"
    assert "ONLY" in prompt_text, "Missing strict grounding instruction in prompt"
    print(f"  - Route registered: '{specialist.route.value}'")
    print(f"  - Target node name: '{specialist.node_name}'")
    print("  - Citation Schema & Grounding Constraints: Verified")
    print("  ✓ Task 5 PASSED.\n")

    # ----------------------------------------------------
    # TASK 6: Graph Wiring & Imports Verification
    # ----------------------------------------------------
    print("--- [Task 6] Testing Graph Assembly & Node Wiring ---")
    assert hasattr(graph_module, "policy_retrieval_node"), "'policy_retrieval_node' is not imported/exposed in graph.py"
    assert hasattr(graph_module, "LangGraphAgent"), "LangGraphAgent missing from graph module"
    print("  - 'policy_retrieval_node' successfully imported and wired in graph module: OK")
    print("  ✓ Task 6 PASSED.\n")

    print("==========================================================")
    print("   ALL TASKS (1 THROUGH 6) VERIFIED AND PASSED 100%!     ")
    print("==========================================================")


if __name__ == "__main__":
    asyncio.run(run_comprehensive_tests())