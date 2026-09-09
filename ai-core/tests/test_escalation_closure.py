"""Tests for escalation closure (Task 2).

--> Target path: tests/test_escalation_closure.py

Follows the repo's existing mocking convention (see test_onboarding.py):
no live database or Mattermost connection. Async calls are driven with
asyncio.run() inside plain sync test functions since pytest-asyncio is not
a declared dependency here.

Reviewers and tickets are represented with SimpleNamespace rather than real
model instances -- escalation_closure.py only ever reads a fixed set of
attributes off them, and this keeps the tests decoupled from unrelated
required fields on the real SQLModel classes.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.services import escalation_closure


def _reviewer(user_id: int = 1) -> SimpleNamespace:
    return SimpleNamespace(id=user_id)


def _ticket(**overrides) -> SimpleNamespace:
    defaults = dict(
        id=1,
        ticket_ref="ESC-000042",
        status="waiting_human",
        question="Can I take next Friday off?",
        learner_id=99,
        learner_channel_id="learner-channel-1",
        learner_thread_id="learner-root-post-1",
        human_dm_channel_id="reviewer-dm-channel-1",
        human_dm_thread_id="reviewer-dm-root-1",
        assigned_human_id=1,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _llm_response(text: str) -> SimpleNamespace:
    return SimpleNamespace(content=text)


def _patched(**mocks):
    """Patch every collaborator escalation_closure imports by name."""
    return patch.multiple(escalation_closure, **mocks)


def test_round_trip_success_delivers_and_closes():
    """A threaded reply to a waiting ticket: synthesize, deliver to the
    learner's thread, confirm to the reviewer, close the record."""
    ticket = _ticket()
    reviewer = _reviewer()

    identity_repo = SimpleNamespace(get_user_by_mattermost_id=AsyncMock(return_value=reviewer))
    escalation_repo = SimpleNamespace(
        get_escalation_ticket_by_human_thread=AsyncMock(return_value=ticket),
        set_escalation_status=AsyncMock(return_value=ticket),
        get_escalation_ticket=AsyncMock(return_value=None),
        list_waiting_tickets_for_human=AsyncMock(return_value=[]),
    )
    llm_service = SimpleNamespace(
        call=AsyncMock(return_value=_llm_response("Yes, that's approved -- up to one week."))
    )
    mattermost_client = SimpleNamespace(
        create_post=AsyncMock(side_effect=[{"id": "learner-post-1"}, {"id": "confirmation-post-1"}])
    )

    with _patched(
        identity_repo=identity_repo,
        escalation_repo=escalation_repo,
        llm_service=llm_service,
        mattermost_client=mattermost_client,
    ):
        result = asyncio.run(
            escalation_closure.handle_reviewer_reply(
                mattermost_user_id="mm-reviewer-1",
                channel_id="reviewer-dm-channel-1",
                channel_type="D",
                root_id="reviewer-dm-root-1",
                text="yes, approve it, one week max",
            )
        )

    assert result.outcome == escalation_closure.ClosureOutcome.RESOLVED
    escalation_repo.set_escalation_status.assert_awaited_once()
    _, kwargs = escalation_repo.set_escalation_status.call_args
    assert kwargs["answer"] == "Yes, that's approved -- up to one week."

    # First post must land in the LEARNER's thread, not the reviewer's.
    first_call = mattermost_client.create_post.call_args_list[0]
    assert first_call.args[0] == ticket.learner_channel_id
    assert first_call.kwargs["root_id"] == ticket.learner_thread_id

    # The learner-facing text must never leak the ticket ref or "reviewer".
    delivered_text = first_call.args[1]
    assert "ESC-" not in delivered_text
    assert "reviewer" not in delivered_text.lower()


def test_ambiguous_when_unthreaded_and_uncited_even_with_one_open_ticket():
    """The specific risk flagged in review: exactly one open ticket must NOT
    be auto-resolved by an unthreaded, unreferenced reply."""
    reviewer = _reviewer()
    only_ticket = _ticket()

    identity_repo = SimpleNamespace(get_user_by_mattermost_id=AsyncMock(return_value=reviewer))
    escalation_repo = SimpleNamespace(
        get_escalation_ticket_by_human_thread=AsyncMock(return_value=None),
        get_escalation_ticket=AsyncMock(return_value=None),
        list_waiting_tickets_for_human=AsyncMock(return_value=[only_ticket]),
        set_escalation_status=AsyncMock(),
    )
    mattermost_client = SimpleNamespace(create_post=AsyncMock(return_value={"id": "reply-1"}))

    with _patched(
        identity_repo=identity_repo,
        escalation_repo=escalation_repo,
        mattermost_client=mattermost_client,
    ):
        result = asyncio.run(
            escalation_closure.handle_reviewer_reply(
                mattermost_user_id="mm-reviewer-1",
                channel_id="reviewer-dm-channel-1",
                channel_type="D",
                root_id="",
                text="sounds fine to me",
            )
        )

    assert result.outcome == escalation_closure.ClosureOutcome.AMBIGUOUS
    escalation_repo.set_escalation_status.assert_not_awaited()
    reply_text = mattermost_client.create_post.call_args.args[1]
    assert only_ticket.ticket_ref in reply_text


def test_ambiguous_with_multiple_open_tickets_lists_all_refs():
    reviewer = _reviewer()
    tickets = [_ticket(id=1, ticket_ref="ESC-000001"), _ticket(id=2, ticket_ref="ESC-000002")]

    identity_repo = SimpleNamespace(get_user_by_mattermost_id=AsyncMock(return_value=reviewer))
    escalation_repo = SimpleNamespace(
        get_escalation_ticket_by_human_thread=AsyncMock(return_value=None),
        get_escalation_ticket=AsyncMock(return_value=None),
        list_waiting_tickets_for_human=AsyncMock(return_value=tickets),
        set_escalation_status=AsyncMock(),
    )
    mattermost_client = SimpleNamespace(create_post=AsyncMock(return_value={"id": "reply-1"}))

    with _patched(
        identity_repo=identity_repo,
        escalation_repo=escalation_repo,
        mattermost_client=mattermost_client,
    ):
        result = asyncio.run(
            escalation_closure.handle_reviewer_reply(
                mattermost_user_id="mm-reviewer-1",
                channel_id="reviewer-dm-channel-1",
                channel_type="D",
                root_id="",
                text="approve it",
            )
        )

    assert result.outcome == escalation_closure.ClosureOutcome.AMBIGUOUS
    reply_text = mattermost_client.create_post.call_args.args[1]
    assert "ESC-000001" in reply_text and "ESC-000002" in reply_text
    escalation_repo.set_escalation_status.assert_not_awaited()


def test_explicit_ticket_reference_resolves_without_threading():
    reviewer = _reviewer()
    ticket = _ticket()

    identity_repo = SimpleNamespace(get_user_by_mattermost_id=AsyncMock(return_value=reviewer))
    escalation_repo = SimpleNamespace(
        get_escalation_ticket_by_human_thread=AsyncMock(return_value=None),
        get_escalation_ticket=AsyncMock(return_value=ticket),
        list_waiting_tickets_for_human=AsyncMock(return_value=[]),
        set_escalation_status=AsyncMock(return_value=ticket),
    )
    llm_service = SimpleNamespace(call=AsyncMock(return_value=_llm_response("Approved for up to a week.")))
    mattermost_client = SimpleNamespace(
        create_post=AsyncMock(side_effect=[{"id": "learner-post-1"}, {"id": "confirm-1"}])
    )

    with _patched(
        identity_repo=identity_repo,
        escalation_repo=escalation_repo,
        llm_service=llm_service,
        mattermost_client=mattermost_client,
    ):
        result = asyncio.run(
            escalation_closure.handle_reviewer_reply(
                mattermost_user_id="mm-reviewer-1",
                channel_id="reviewer-dm-channel-1",
                channel_type="D",
                root_id="",
                text="re ESC-000042: yes, approve it",
            )
        )

    assert result.outcome == escalation_closure.ClosureOutcome.RESOLVED
    escalation_repo.set_escalation_status.assert_awaited_once()


def test_cited_ticket_belonging_to_someone_else_is_refused():
    reviewer = _reviewer(user_id=1)
    someone_elses_ticket = _ticket(assigned_human_id=2)

    identity_repo = SimpleNamespace(get_user_by_mattermost_id=AsyncMock(return_value=reviewer))
    escalation_repo = SimpleNamespace(
        get_escalation_ticket_by_human_thread=AsyncMock(return_value=None),
        get_escalation_ticket=AsyncMock(return_value=someone_elses_ticket),
        list_waiting_tickets_for_human=AsyncMock(return_value=[]),
        set_escalation_status=AsyncMock(),
    )
    mattermost_client = SimpleNamespace(create_post=AsyncMock(return_value={"id": "reply-1"}))

    with _patched(
        identity_repo=identity_repo,
        escalation_repo=escalation_repo,
        mattermost_client=mattermost_client,
    ):
        result = asyncio.run(
            escalation_closure.handle_reviewer_reply(
                mattermost_user_id="mm-reviewer-1",
                channel_id="reviewer-dm-channel-1",
                channel_type="D",
                root_id="",
                text="ESC-000042 approved",
            )
        )

    assert result.outcome == escalation_closure.ClosureOutcome.WRONG_OWNER
    escalation_repo.set_escalation_status.assert_not_awaited()


def test_unknown_cited_ticket_reference():
    reviewer = _reviewer()

    identity_repo = SimpleNamespace(get_user_by_mattermost_id=AsyncMock(return_value=reviewer))
    escalation_repo = SimpleNamespace(
        get_escalation_ticket_by_human_thread=AsyncMock(return_value=None),
        get_escalation_ticket=AsyncMock(return_value=None),
        list_waiting_tickets_for_human=AsyncMock(return_value=[]),
        set_escalation_status=AsyncMock(),
    )
    mattermost_client = SimpleNamespace(create_post=AsyncMock(return_value={"id": "reply-1"}))

    with _patched(
        identity_repo=identity_repo,
        escalation_repo=escalation_repo,
        mattermost_client=mattermost_client,
    ):
        result = asyncio.run(
            escalation_closure.handle_reviewer_reply(
                mattermost_user_id="mm-reviewer-1",
                channel_id="reviewer-dm-channel-1",
                channel_type="D",
                root_id="",
                text="ESC-999999 approved",
            )
        )

    assert result.outcome == escalation_closure.ClosureOutcome.TICKET_NOT_FOUND
    escalation_repo.set_escalation_status.assert_not_awaited()


def test_post_closure_reply_is_handled_gracefully():
    """A reply landing in an already-resolved ticket's thread must not crash
    or reopen anything -- just an informational notice."""
    reviewer = _reviewer()
    resolved_ticket = _ticket(status="resolved")

    identity_repo = SimpleNamespace(get_user_by_mattermost_id=AsyncMock(return_value=reviewer))
    escalation_repo = SimpleNamespace(
        get_escalation_ticket_by_human_thread=AsyncMock(return_value=resolved_ticket),
        set_escalation_status=AsyncMock(),
    )
    mattermost_client = SimpleNamespace(create_post=AsyncMock(return_value={"id": "reply-1"}))

    with _patched(
        identity_repo=identity_repo,
        escalation_repo=escalation_repo,
        mattermost_client=mattermost_client,
    ):
        result = asyncio.run(
            escalation_closure.handle_reviewer_reply(
                mattermost_user_id="mm-reviewer-1",
                channel_id="reviewer-dm-channel-1",
                channel_type="D",
                root_id="reviewer-dm-root-1",
                text="thanks!",
            )
        )

    assert result.outcome == escalation_closure.ClosureOutcome.ALREADY_RESOLVED
    escalation_repo.set_escalation_status.assert_not_awaited()


def test_non_dm_channel_is_never_intercepted():
    """Escalation replies only ever arrive in a DM/group DM; a public-channel
    message must pass through untouched, with zero lookups performed."""
    identity_repo = SimpleNamespace(get_user_by_mattermost_id=AsyncMock())

    with _patched(identity_repo=identity_repo):
        result = asyncio.run(
            escalation_closure.handle_reviewer_reply(
                mattermost_user_id="mm-someone",
                channel_id="public-channel-1",
                channel_type="O",
                root_id="",
                text="approve it",
            )
        )

    assert result.outcome == escalation_closure.ClosureOutcome.NOT_ESCALATION
    identity_repo.get_user_by_mattermost_id.assert_not_awaited()


def test_ordinary_dm_chat_passes_through_when_nothing_is_pending():
    """A reviewer with zero open tickets just chatting must not be
    mistaken for an escalation reply."""
    reviewer = _reviewer()
    identity_repo = SimpleNamespace(get_user_by_mattermost_id=AsyncMock(return_value=reviewer))
    escalation_repo = SimpleNamespace(
        get_escalation_ticket_by_human_thread=AsyncMock(return_value=None),
        get_escalation_ticket=AsyncMock(return_value=None),
        list_waiting_tickets_for_human=AsyncMock(return_value=[]),
    )
    mattermost_client = SimpleNamespace(create_post=AsyncMock())

    with _patched(
        identity_repo=identity_repo,
        escalation_repo=escalation_repo,
        mattermost_client=mattermost_client,
    ):
        result = asyncio.run(
            escalation_closure.handle_reviewer_reply(
                mattermost_user_id="mm-reviewer-1",
                channel_id="reviewer-dm-channel-1",
                channel_type="D",
                root_id="",
                text="hey, how's it going?",
            )
        )

    assert result.outcome == escalation_closure.ClosureOutcome.NOT_ESCALATION
    mattermost_client.create_post.assert_not_awaited()


def test_delivery_failure_keeps_the_ticket_open():
    """If the learner post fails, the ticket must NOT be marked resolved --
    the answer would otherwise be silently lost."""
    ticket = _ticket()
    reviewer = _reviewer()

    identity_repo = SimpleNamespace(get_user_by_mattermost_id=AsyncMock(return_value=reviewer))
    escalation_repo = SimpleNamespace(
        get_escalation_ticket_by_human_thread=AsyncMock(return_value=ticket),
        set_escalation_status=AsyncMock(),
    )
    llm_service = SimpleNamespace(call=AsyncMock(return_value=_llm_response("Approved.")))
    mattermost_client = SimpleNamespace(create_post=AsyncMock(return_value=None))

    with _patched(
        identity_repo=identity_repo,
        escalation_repo=escalation_repo,
        llm_service=llm_service,
        mattermost_client=mattermost_client,
    ):
        result = asyncio.run(
            escalation_closure.handle_reviewer_reply(
                mattermost_user_id="mm-reviewer-1",
                channel_id="reviewer-dm-channel-1",
                channel_type="D",
                root_id="reviewer-dm-root-1",
                text="approved",
            )
        )

    assert result.outcome == escalation_closure.ClosureOutcome.DELIVERY_FAILED
    escalation_repo.set_escalation_status.assert_not_awaited()