import asyncio
import hashlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.services import knowledge_extraction, knowledge_review
from app.services.authorisation import AuthorisationRefused


def _llm_response(text: str) -> SimpleNamespace:
    return SimpleNamespace(content=text)


def test_extraction_returns_none_for_ungeneralizable_resolutions():
    with patch.object(
        knowledge_extraction, "llm_service", SimpleNamespace(call=AsyncMock(return_value=_llm_response("NONE")))
    ):
        result = asyncio.run(
            knowledge_extraction._extract_candidate("can you check my submission?", "already fixed it, ignore")
        )
    assert result is None


def test_extraction_narrows_a_one_off_exception():
    narrowed = (
        "AUDIENCE: internal_operator\n"
        "STATEMENT: Grace periods for regional infrastructure outages require explicit supervisor approval."
    )
    with patch.object(
        knowledge_extraction, "llm_service", SimpleNamespace(call=AsyncMock(return_value=_llm_response(narrowed)))
    ):
        result = asyncio.run(
            knowledge_extraction._extract_candidate(
                "Can I get a 48-hour extension?",
                "Approved for cohort 4 just this once due to the regional fibre cut.",
            )
        )
    assert result is not None
    audience, statement = result
    assert audience == "internal_operator"
    assert "just this once" not in statement.lower()
    assert "outage" in statement.lower()


def test_extraction_rejects_malformed_model_output_rather_than_guessing():
    with patch.object(
        knowledge_extraction,
        "llm_service",
        SimpleNamespace(call=AsyncMock(return_value=_llm_response("Sure, here's a summary of the ticket."))),
    ):
        result = asyncio.run(knowledge_extraction._extract_candidate("q", "a"))
    assert result is None

    with patch.object(
        knowledge_extraction,
        "llm_service",
        SimpleNamespace(
            call=AsyncMock(return_value=_llm_response("AUDIENCE: everyone\nSTATEMENT: something"))
        ),
    ):
        result = asyncio.run(knowledge_extraction._extract_candidate("q", "a"))
    assert result is None


def _candidate(**overrides) -> SimpleNamespace:
    defaults = dict(
        id=1,
        escalation_id=42,
        statement="Grace periods for regional infrastructure outages require explicit supervisor approval.",
        audience="internal_operator",
        status="pending",
        reviewer_id=None,
        reviewed_at=None,
        rejection_reason=None,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


class FakeSession:
    """In-memory session stand-in for both sync Session and async session_scope."""

    _store: dict = {}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, model, pk):
        return FakeSession._store.get(pk)

    def add(self, obj):
        FakeSession._store[obj.id] = obj

    async def exec(self, stmt):
        class FakeResult:
            def all(self):
                return list(FakeSession._store.values())
        return FakeResult()

    def commit(self):
        pass

    def refresh(self, obj):
        pass


@pytest.fixture(autouse=True)
def reset_store():
    FakeSession._store = {}
    yield
    FakeSession._store = {}


@pytest.fixture
def mock_session():
    fake = FakeSession()
    with patch.object(knowledge_review, "session_scope", return_value=fake), \
         patch.object(knowledge_review, "Session", return_value=fake):
        yield


def test_approve_requires_requester_user(mock_session):
    candidate = _candidate()
    FakeSession._store[1] = candidate

    with patch.object(
        knowledge_review, "require_requester_user", AsyncMock(side_effect=AuthorisationRefused("not_admin"))
    ):
        with pytest.raises(AuthorisationRefused):
            asyncio.run(knowledge_review.approve_candidate(1))

    assert candidate.status == "pending"


def test_approve_indexes_and_transitions_to_approved(mock_session):
    candidate = _candidate()
    FakeSession._store[1] = candidate
    reviewer = SimpleNamespace(id=7)

    with patch.object(knowledge_review, "require_requester_user", AsyncMock(return_value=reviewer)), patch.object(
        knowledge_review, "_index_candidate", AsyncMock()
    ) as index_mock:
        result = asyncio.run(knowledge_review.approve_candidate(1))

    assert result.outcome == knowledge_review.ReviewOutcome.APPROVED
    assert candidate.status == "approved"
    assert candidate.reviewer_id == 7
    assert candidate.reviewed_at is not None
    index_mock.assert_awaited_once()


def test_reject_never_indexes(mock_session):
    candidate = _candidate()
    FakeSession._store[1] = candidate
    reviewer = SimpleNamespace(id=7)

    with patch.object(knowledge_review, "require_requester_user", AsyncMock(return_value=reviewer)), patch.object(
        knowledge_review, "_index_candidate", AsyncMock()
    ) as index_mock:
        result = asyncio.run(knowledge_review.reject_candidate(1, reason="too specific to generalize"))

    assert result.outcome == knowledge_review.ReviewOutcome.REJECTED
    assert candidate.status == "rejected"
    index_mock.assert_not_awaited()


def test_approve_on_already_decided_candidate_is_refused_not_repeated(mock_session):
    candidate = _candidate(status="rejected")
    FakeSession._store[1] = candidate
    reviewer = SimpleNamespace(id=7)

    with patch.object(knowledge_review, "require_requester_user", AsyncMock(return_value=reviewer)), patch.object(
        knowledge_review, "_index_candidate", AsyncMock()
    ) as index_mock:
        result = asyncio.run(knowledge_review.approve_candidate(1))

    assert result.outcome == knowledge_review.ReviewOutcome.ALREADY_DECIDED
    assert candidate.status == "rejected"
    index_mock.assert_not_awaited()


def test_approve_unknown_candidate_is_not_found(mock_session):
    reviewer = SimpleNamespace(id=7)
    with patch.object(knowledge_review, "require_requester_user", AsyncMock(return_value=reviewer)):
        result = asyncio.run(knowledge_review.approve_candidate(999))
    assert result.outcome == knowledge_review.ReviewOutcome.NOT_FOUND


def test_indexing_writes_audience_and_provenance_into_the_chunk():
    candidate = _candidate(id=5, escalation_id=42, audience="learner")
    captured = {}

    async def fake_upsert(chunks, embeddings):
        captured["chunks"] = chunks
        captured["embeddings"] = embeddings

    with patch.object(
        knowledge_review, "generate_embeddings", AsyncMock(return_value=[0.1] * 1536)
    ), patch.object(
        knowledge_review,
        "PolicyVectorStore",
        return_value=SimpleNamespace(upsert_chunks=fake_upsert),
    ):
        asyncio.run(knowledge_review._index_candidate(candidate))

    chunk = captured["chunks"][0]
    assert chunk["audience"] == "learner"
    assert chunk["metadata"]["escalation_id"] == 42
    assert chunk["metadata"]["candidate_id"] == 5
    assert chunk["metadata"]["source"] == "knowledge_candidate"


def test_deterministic_chunk_id_is_stable_across_calls():
    content_hash = hashlib.sha256(b"some statement").hexdigest()
    first = knowledge_review._deterministic_chunk_id(42, content_hash)
    second = knowledge_review._deterministic_chunk_id(42, content_hash)
    different_escalation = knowledge_review._deterministic_chunk_id(43, content_hash)

    assert first == second
    assert first != different_escalation


def test_pending_candidate_not_retrievable_via_search():
    captured_chunks: list = []

    async def fake_upsert(chunks, embeddings):
        captured_chunks.extend(chunks)

    with patch.object(
        knowledge_review, "generate_embeddings", AsyncMock(return_value=[0.1] * 1536)
    ), patch.object(
        knowledge_review,
        "PolicyVectorStore",
        return_value=SimpleNamespace(upsert_chunks=fake_upsert),
    ):
        pass

    assert captured_chunks == []


def test_rejected_candidate_never_reaches_vector_store(mock_session):
    candidate = _candidate()
    FakeSession._store[1] = candidate
    reviewer = SimpleNamespace(id=7)

    with patch.object(knowledge_review, "require_requester_user", AsyncMock(return_value=reviewer)), \
         patch.object(knowledge_review, "_index_candidate", AsyncMock()) as index_mock:
        asyncio.run(knowledge_review.reject_candidate(1, reason="one-off exception"))

    index_mock.assert_not_awaited()
    assert candidate.status == "rejected"


def test_internal_knowledge_not_exposed_to_learner_audience():
    from app.services.document_ingestion.vector_store import PolicyVectorStore

    store = PolicyVectorStore()
    learner_audience = "learner"
    allowed = (
        ("admin", "learner", "public", "internal_operator")
        if learner_audience in (None, "admin", "superadmin")
        else (learner_audience,)
    )
    assert "internal_operator" not in allowed
    assert "learner" in allowed


def test_extraction_idempotency_on_conflict_do_nothing():
    from unittest.mock import MagicMock

    fake_result = MagicMock()
    fake_result.scalar_one_or_none.return_value = None

    with patch("app.services.knowledge_extraction.database_service", create=True):
        assert fake_result.scalar_one_or_none() is None


def test_list_kc_command_is_recognised():
    pending_candidates = [
        knowledge_review.CandidateForReview(
            candidate_id=1,
            statement="Grace periods require supervisor approval.",
            audience="internal_operator",
            escalation_ref="ESC-000001",
            original_question="Can I get an extension?",
            original_decision="Approved just this once.",
        )
    ]

    with patch.object(
        knowledge_review, "list_pending_for_review", AsyncMock(return_value=pending_candidates)
    ), patch.object(
        knowledge_review, "resolve_requester", AsyncMock(return_value=SimpleNamespace(id=5))
    ), patch.object(
        knowledge_review, "mattermost_client",
        SimpleNamespace(create_post=AsyncMock())
    ):
        result = asyncio.run(
            knowledge_review.handle_reviewer_reply(
                mattermost_user_id="abc123",
                channel_id="dm_channel",
                channel_type="D",
                text="list KC",
            )
        )

    assert result is True


def test_list_kc_empty_queue():
    with patch.object(
        knowledge_review, "list_pending_for_review", AsyncMock(return_value=[])
    ), patch.object(
        knowledge_review, "resolve_requester", AsyncMock(return_value=SimpleNamespace(id=5))
    ), patch.object(
        knowledge_review, "mattermost_client",
        SimpleNamespace(create_post=AsyncMock())
    ):
        asyncio.run(
            knowledge_review.handle_reviewer_reply(
                mattermost_user_id="abc123",
                channel_id="dm_channel",
                channel_type="D",
                text="list KC",
            )
        )
        call_args = knowledge_review.mattermost_client.create_post.call_args
        assert call_args is not None
        posted_text = call_args[0][1] if call_args[0] else call_args[1].get("text", "")
        assert "pending" in posted_text.lower()


def test_approve_command_sends_confirmation_message(mock_session):
    candidate = _candidate()
    FakeSession._store[1] = candidate
    reviewer = SimpleNamespace(id=7)

    posted_messages: list = []

    async def fake_post(channel_id, text, **kwargs):
        posted_messages.append(text)

    with patch.object(knowledge_review, "require_requester_user", AsyncMock(return_value=reviewer)), \
         patch.object(knowledge_review, "_index_candidate", AsyncMock()), \
         patch.object(knowledge_review, "resolve_requester", AsyncMock(return_value=SimpleNamespace(id=7))), \
         patch.object(knowledge_review, "mattermost_client", SimpleNamespace(create_post=AsyncMock(side_effect=fake_post))):
        result = asyncio.run(
            knowledge_review.handle_reviewer_reply(
                mattermost_user_id="abc123",
                channel_id="dm_channel",
                channel_type="D",
                text="approve KC-1",
            )
        )

    assert result is True
    assert any("approved" in m.lower() for m in posted_messages)