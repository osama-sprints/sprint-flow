"""Tests for the policy retrieval capability."""

import pytest
from unittest.mock import patch, AsyncMock

from app.services.policy_retrieval import get_grounded_answer_or_refusal


@pytest.fixture
def mock_generate_embeddings():
    with patch("app.services.policy_retrieval.generate_embeddings", new_callable=AsyncMock) as mock:
        mock.return_value = [0.1] * 1536
        yield mock


@pytest.fixture
def mock_policy_vector_store():
    with patch("app.services.policy_retrieval.PolicyVectorStore") as MockStore:
        store_instance = MockStore.return_value
        store_instance.similarity_search = AsyncMock(return_value=[])
        yield store_instance


@pytest.mark.asyncio
async def test_get_grounded_answer_or_refusal_above_threshold(mock_generate_embeddings, mock_policy_vector_store):
    mock_doc = {"page_content": "Leave policy details.", "audience": "learner"}
    mock_policy_vector_store.similarity_search.return_value = [(mock_doc, 0.8)]

    status, docs = await get_grounded_answer_or_refusal("leave policy", audience="learner")

    assert status == "grounded"
    assert len(docs) == 1
    assert docs[0]["page_content"] == "Leave policy details."
    assert docs[0]["similarity_score"] == 0.8
    mock_policy_vector_store.similarity_search.assert_called_once()


@pytest.mark.asyncio
async def test_get_grounded_answer_or_refusal_below_threshold(mock_generate_embeddings, mock_policy_vector_store):
    mock_doc = {"page_content": "Unrelated content.", "audience": "learner"}
    # Score 0.4 is below DEFAULT_SIMILARITY_THRESHOLD (0.50)
    mock_policy_vector_store.similarity_search.return_value = [(mock_doc, 0.4)]

    status, docs = await get_grounded_answer_or_refusal("leave policy")

    assert status == "no_match"
    assert len(docs) == 0


@pytest.mark.asyncio
async def test_get_grounded_answer_or_refusal_empty_store(mock_generate_embeddings, mock_policy_vector_store):
    mock_policy_vector_store.similarity_search.return_value = []

    status, docs = await get_grounded_answer_or_refusal("leave policy")

    assert status == "no_match"
    assert len(docs) == 0


@pytest.mark.asyncio
async def test_get_grounded_answer_or_refusal_error_handling(mock_generate_embeddings, mock_policy_vector_store):
    mock_policy_vector_store.similarity_search.side_effect = Exception("DB connection failed")

    status, docs = await get_grounded_answer_or_refusal("leave policy")

    assert status == "error"
    assert len(docs) == 0
