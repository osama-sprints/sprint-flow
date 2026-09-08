import asyncio
from unittest.mock import patch
from app.services.policy_retrieval import get_grounded_answer_or_refusal

async def run_tests():
    print("--- Starting Task Tests (Policy Retrieval) ---\n")

    # 1. Test case: grounded (valid results above the 0.75 threshold)
    mock_grounded_data = [
        {
            "document_id": "DOC-101",
            "section_title": "Leave Policy",
            "page_number": 3,
            "text": "Employees get 21 days of paid leave per year.",
            "similarity_score": 0.88
        }
    ]
    
    with patch("app.services.policy_retrieval.similarity_search", return_value=mock_grounded_data):
        status, docs = await get_grounded_answer_or_refusal("what is the leave policy?", audience="learner")
        print(f"1. Grounded Test  -> Status: {status} | Docs count: {len(docs)}")
        assert status == "grounded", f"Expected 'grounded', got '{status}'"

    # 2. Test case: no_match (out of scope query returning empty results)
    with patch("app.services.policy_retrieval.similarity_search", return_value=[]):
        status, docs = await get_grounded_answer_or_refusal("what's the capital of France?", audience="learner")
        print(f"2. No Match Test  -> Status: {status} | Docs count: {len(docs)}")
        assert status == "no_match", f"Expected 'no_match', got '{status}'"

    # 3. Test case: error (mocking database exception)
    with patch("app.services.policy_retrieval.similarity_search", side_effect=Exception("Database connection failed")):
        status, docs = await get_grounded_answer_or_refusal("what is the leave policy?", audience="learner")
        print(f"3. Exception Test -> Status: {status} | Docs count: {len(docs)}")
        assert status == "error", f"Expected 'error', got '{status}'"

    print("\nAll test cases executed and passed successfully.")

if __name__ == "__main__":
    asyncio.run(run_tests())