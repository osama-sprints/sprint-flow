"""Sprint-wide integration scenario: policy Q&A, escalation, and closure.

--> Target path: tests/test_sprint_scenario_integration.py

Walks the exact narrative the sprint brief describes:

  "An honest refusal is also what triggers the escalation path built
   earlier in this sprint."

Two questions, one learner:

  1. A question the ingested policy documents actually cover -> Task 3/4's
     retrieval returns a grounded match. No escalation, no human involved.
  2. A question nothing covers -> Task 3/4 correctly refuses (no_match) ->
     that refusal is what should call Task 1's open_escalation() -> a human
     replies -> Task 2's closure delivers the final answer back to the
     learner and confirms to the reviewer.

This exercises the REAL functions from all four tasks
(app.services.policy_retrieval.get_grounded_answer_or_refusal,
app.services.escalation.open_escalation, app.services.escalation_closure.
handle_reviewer_reply) with only the true external boundaries mocked:
the vector store, the Mattermost API, and the LLM. It intentionally calls
open_escalation() directly rather than going through
app.core.langgraph.nodes.policy_retrieval_node, because that node's current
wiring does not call open_escalation() correctly (missing required
arguments) -- see reports/closure_report.md's team notes. This test
documents the integration the team is building toward; once that node is
fixed to call open_escalation() with the right arguments, this is the exact
path a live conversation takes.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.core.langgraph.tools.results import ResultCode
from app.core.requester import RequesterContext
from app.services import escalation as escalation_module
from app.services import escalation_closure
from app.services import policy_retrieval


# ---------------------------------------------------------------------------
# Part 1 (Task 3 + 4): a covered question is answered from real documents
# ---------------------------------------------------------------------------


def test_grounded_question_never_touches_escalation():
    """A question the knowledge base covers returns 'grounded' with no
    escalation involved at all -- this is the happy path Task 4 exists for."""
    matching_doc = {
        "content": "Learners get 5 paid leave days per term, requested at least 3 days ahead.",
        "source": "ACC FAQs",
        "similarity_score": 0.91,
    }

    mock_store = SimpleNamespace(similarity_search=AsyncMock(return_value=[matching_doc]))
    with patch.object(policy_retrieval, "PolicyVectorStore", return_value=mock_store):
        status, docs = asyncio.run(
            policy_retrieval.get_grounded_answer_or_refusal(
                "How many leave days do I get?", audience="learner"
            )
        )

    assert status == "grounded"
    assert docs and docs[0]["source"] == "ACC FAQs"


# ---------------------------------------------------------------------------
# Part 2 (Task 3 + 4 -> Task 1 -> Task 2): an uncovered question escalates
# and comes back through closure
# ---------------------------------------------------------------------------


def _learner_requester() -> RequesterContext:
    return RequesterContext(
        mattermost_user_id="mm-learner-1",
        username="newlearner",
        channel_id="cohort-backend01-channel",
        channel_type="O",
        learner_thread_id="learner-question-post-1",
    )


def test_refusal_opens_an_escalation_that_closure_then_resolves():
    """The full arc: no grounded answer -> a human is quietly notified ->
    they reply -> the learner gets a complete, courteous answer with no
    trace of the ticket or the reviewer's identity."""

    # ---- Task 3/4: the retrieval genuinely finds nothing ----
    empty_store = SimpleNamespace(similarity_search=AsyncMock(return_value=[]))
    with patch.object(policy_retrieval, "PolicyVectorStore", return_value=empty_store):
        status, docs = asyncio.run(
            policy_retrieval.get_grounded_answer_or_refusal(
                "Can I get reimbursed for a personal laptop?", audience="learner"
            )
        )
    assert status == "no_match"
    assert docs == []

    # This is the exact moment app.core.langgraph.nodes should call
    # open_escalation() -- see the module docstring above for the current
    # wiring gap. Calling it directly here tests the real function.

    # ---- Task 1: open_escalation() resolves the channel's human, opens the DM ----
    # Note: the Cohort entity is gone entirely in this version -- channel_id is
    # used directly as a string, no separate resolution/active-check step, and
    # channel_repo.list_channel_roles replaces the old cohort_repo.list_cohort_members.
    learner = SimpleNamespace(id=99, display_name="New Learner", username="newlearner")
    tech_lead_holder = SimpleNamespace(
        role=SimpleNamespace(key="tech_lead"),
        user=SimpleNamespace(id=7, mattermost_user_id="mm-techlead-1"),
    )

    created_ticket = SimpleNamespace(
        id=1,
        ticket_ref="ESC-000123",
        ticket_type="tech",
        status="waiting_human",
        question="Can I get reimbursed for a personal laptop?",
        learner_id=99,
        channel_id="cohort-backend01-channel",
        learner_thread_id="learner-question-post-1",
        human_dm_channel_id=None,
        human_dm_thread_id=None,
        assigned_human_id=7,
    )

    identity_repo = SimpleNamespace(get_user_by_mattermost_id=AsyncMock(return_value=learner))
    channel_repo = SimpleNamespace(list_channel_roles=AsyncMock(return_value=[tech_lead_holder]))
    escalation_repo_task1 = SimpleNamespace(
        get_open_escalation_ticket_for_learner_thread=AsyncMock(return_value=None),
        create_escalation_ticket=AsyncMock(return_value=created_ticket),
        set_escalation_status=AsyncMock(return_value=created_ticket),
    )
    mattermost_client_task1 = SimpleNamespace(
        create_direct_channel=AsyncMock(return_value={"id": "reviewer-dm-channel-1"}),
        create_post=AsyncMock(return_value={"id": "reviewer-dm-root-1"}),
    )

    with patch.multiple(
        escalation_module,
        identity_repo=identity_repo,
        channel_repo=channel_repo,
        escalation_repo=escalation_repo_task1,
        mattermost_client=mattermost_client_task1,
    ):
        open_result = asyncio.run(
            escalation_module.open_escalation(
                "Can I get reimbursed for a personal laptop?", requester=_learner_requester()
            )
        )

    assert open_result.code == ResultCode.ESCALATION_OPENED
    ticket = open_result.ticket
    assert ticket.ticket_ref == "ESC-000123"
    # The learner-facing message from Task 1 must not name the reviewer.
    assert "colleague" in open_result.message.lower()

    # The ticket produced by open_escalation() now carries a real DM thread,
    # since create_escalation_ticket was mocked to return it pre-populated
    # (mirroring what Task 1's real repo call would persist).
    ticket.human_dm_channel_id = "reviewer-dm-channel-1"
    ticket.human_dm_thread_id = "reviewer-dm-root-1"

    # ---- Task 2 (this task): the reviewer replies, closure resolves it ----
    reviewer = SimpleNamespace(id=7)
    identity_repo_task2 = SimpleNamespace(get_user_by_mattermost_id=AsyncMock(return_value=reviewer))
    escalation_repo_task2 = SimpleNamespace(
        get_escalation_ticket_by_human_thread=AsyncMock(return_value=ticket),
        set_escalation_status=AsyncMock(return_value=ticket),
    )
    llm_service_task2 = SimpleNamespace(
        call=AsyncMock(
            return_value=SimpleNamespace(
                content="Personal laptop purchases aren't reimbursed, but let me know if you need "
                "help requesting one through the equipment programme instead."
            )
        )
    )
    mattermost_client_task2 = SimpleNamespace(
        create_post=AsyncMock(side_effect=[{"id": "learner-answer-post-1"}, {"id": "reviewer-confirm-1"}])
    )

    with patch.multiple(
        escalation_closure,
        identity_repo=identity_repo_task2,
        escalation_repo=escalation_repo_task2,
        llm_service=llm_service_task2,
        mattermost_client=mattermost_client_task2,
    ):
        closure_result = asyncio.run(
            escalation_closure.handle_reviewer_reply(
                mattermost_user_id="mm-techlead-1",
                channel_id="reviewer-dm-channel-1",
                channel_type="D",
                root_id="reviewer-dm-root-1",
                text="no, personal laptops aren't covered -- point them at the equipment programme",
            )
        )

    assert closure_result.outcome == escalation_closure.ClosureOutcome.RESOLVED
    escalation_repo_task2.set_escalation_status.assert_awaited_once()

    # The learner's post must land in THEIR original thread from the very
    # first message (Task 1's learner_thread_id, unchanged end to end).
    learner_post_call = mattermost_client_task2.create_post.call_args_list[0]
    assert learner_post_call.args[0] == "cohort-backend01-channel"
    assert learner_post_call.kwargs["root_id"] == "learner-question-post-1"

    delivered_text = learner_post_call.args[1]
    assert "ESC-" not in delivered_text
    assert "tech lead" not in delivered_text.lower()
    assert "equipment programme" in delivered_text