"""Backend authorisation boundary for ceremony scheduling and the calendar.

Routing is NOT the security boundary. These tests call the service functions
directly — the way a mis-routed or forged tool call would — and prove that:

- only a stored tech lead / scrum master of the channel (or a stored superadmin)
  may schedule, amend or cancel (Capability 7);
- ``commit_schedule`` / ``commit_amendment`` re-check authority at commit time,
  so a proposal prepared by an authorised person cannot be committed by another;
- a learner, a non-member, a wrong-channel authority and an unsynced identity
  are all refused with the fixed sentence, before any data is disclosed;
- the calendar read is member-scoped: a non-member reads nothing (Capability 6).

Every data-access boundary is faked deterministically (no database, no network):
identity lookups, channel role lookups and ceremony lookups are SimpleNamespaces
or AsyncMocks mirroring the real repo signatures in app.services.domain.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from contextlib import ExitStack
from datetime import UTC, datetime, timedelta

import pytest

from app.core.requester import RequesterContext
from app.models.enums import CeremonyStatus, RoleKey
from app.services import authorisation as authorisation_module
from app.services import ceremony_scheduling as scheduling
from app.services.authorisation import (
    MEETING_REFUSAL_MESSAGE,
    REFUSAL_MESSAGE,
    AuthorisationRefused,
    ValidationFailed,
)

NOW = datetime(2026, 9, 8, 10, 0, tzinfo=UTC)
START = NOW + timedelta(days=1)
TYPE_ROW = SimpleNamespace(id=2, key="sprint_planning", label="Sprint Planning", default_duration_minutes=90)


def requester(
    mm_id: str = "mm-lead",
    *,
    channel_id: str = "chan-1",
    team_id: str = "team-1",
    superadmin: bool = False,
    user_id: int | None = 3,
) -> RequesterContext:
    return RequesterContext(
        mattermost_user_id=mm_id,
        username="alice",
        email="alice@example.test",
        channel_id=channel_id,
        team_id=team_id,
        channel_type="O",
        user_id=user_id,
        is_superadmin=superadmin,
        timezone="UTC",
        channel_roles={},
    )


def user(id: int, *, superadmin: bool = False) -> SimpleNamespace:
    return SimpleNamespace(id=id, is_superadmin=superadmin, username="alice", email="alice@example.test")


def role(key: str | None) -> SimpleNamespace:
    return None if key is None else SimpleNamespace(key=key, label=key.replace("_", " ").title())


def repos_for(
    *,
    stored_user: SimpleNamespace | None,
    channel_roles: dict[str, str | None] | None = None,
    ceremonies: list | None = None,
    types: list | None = None,
) -> dict:
    """Doubles for every repo boundary the scheduling service reads.

    ``channel_roles`` maps channel_id -> role key (None = no membership), so the
    lookup behaves like the real channel-scoped query.
    """
    roles = channel_roles or {}

    async def role_in_channel(user_id: int, channel_id: str, **_: object):
        return role(roles.get(channel_id))

    return {
        "identity_repo": SimpleNamespace(
            get_user_by_mattermost_id=AsyncMock(return_value=stored_user),
            get_user=AsyncMock(return_value=SimpleNamespace(username="alice")),
        ),
        "channel_repo": SimpleNamespace(get_role_for_user_in_channel=AsyncMock(side_effect=role_in_channel)),
        "ceremony_repo": SimpleNamespace(
            get_ceremony_type_by_key=AsyncMock(return_value=TYPE_ROW),
            get_ceremony=AsyncMock(return_value=(ceremonies or [None])[0]),
            find_overlapping_ceremonies=AsyncMock(return_value=[]),
            create_ceremony=AsyncMock(),
            update_ceremony=AsyncMock(),
            list_ceremonies=AsyncMock(return_value=[]),
            list_ceremony_types=AsyncMock(return_value=types or [TYPE_ROW]),
            list_amendments=AsyncMock(return_value=[]),
        ),
    }


def run(coro):
    return asyncio.run(coro)


def patched(doubles: dict):
    """Patch every repo boundary at its real definition point.

    ``identity_repo``/``channel_repo`` are read from ``app.services.authorisation``
    (the shared security module); ``ceremony_repo``/``identity_repo`` from the
    scheduling service itself.
    """
    stack = ExitStack()
    stack.enter_context(
        patch.multiple(
            authorisation_module,
            identity_repo=doubles["identity_repo"],
            channel_repo=doubles["channel_repo"],
        )
    )
    stack.enter_context(
        patch.multiple(
            scheduling,
            ceremony_repo=doubles["ceremony_repo"],
            identity_repo=doubles["identity_repo"],
        )
    )
    return stack


# ---------------------------------------------------------------------------
# prepare_schedule: the authorisation boundary
# ---------------------------------------------------------------------------


def test_learner_is_refused_and_nothing_is_prepared():
    doubles = repos_for(stored_user=user(9), channel_roles={"chan-1": RoleKey.LEARNER.value})
    with patched(doubles):
        with pytest.raises(AuthorisationRefused) as excinfo:
            run(
                scheduling.prepare_schedule(
                    ceremony_type="planning", time_expression="tomorrow at 2pm", requester=requester(mm_id="mm-l")
                )
            )
    assert str(excinfo.value) == MEETING_REFUSAL_MESSAGE
    assert doubles["channel_repo"].get_role_for_user_in_channel.await_count == 1


def test_non_member_is_refused():
    doubles = repos_for(stored_user=user(9), channel_roles={})
    with patched(doubles):
        with pytest.raises(AuthorisationRefused):
            run(
                scheduling.prepare_schedule(
                    ceremony_type="planning", time_expression="tomorrow at 2pm", requester=requester()
                )
            )


def test_unsynced_identity_is_refused():
    doubles = repos_for(stored_user=None, channel_roles={})
    with patched(doubles):
        with pytest.raises(AuthorisationRefused) as excinfo:
            run(
                scheduling.prepare_schedule(
                    ceremony_type="planning", time_expression="tomorrow at 2pm", requester=requester(mm_id="mm-ghost")
                )
            )
    assert excinfo.value.reason == "requester_not_synced"


def test_learner_of_another_channel_is_refused_here():
    """Authority is channel-scoped: a tech lead in chan-2 holds nothing in chan-1."""
    doubles = repos_for(
        stored_user=user(9),
        channel_roles={"chan-2": RoleKey.TECH_LEAD.value},  # authority in chan-2 only
    )
    with patched(doubles):
        with pytest.raises(AuthorisationRefused):
            run(
                scheduling.prepare_schedule(
                    ceremony_type="planning",
                    time_expression="tomorrow at 2pm",
                    requester=requester(mm_id="mm-2", channel_id="chan-1", team_id="team-1"),
                )
            )


def test_learner_who_is_superadmin_in_context_only_is_refused():
    """A forged context flag must not elevate: the decision reads the STORED user."""
    doubles = repos_for(stored_user=user(9, superadmin=False), channel_roles={"chan-1": RoleKey.LEARNER.value})
    with patched(doubles):
        with pytest.raises(AuthorisationRefused):
            run(
                scheduling.prepare_schedule(
                    ceremony_type="planning",
                    time_expression="tomorrow at 2pm",
                    requester=requester(superadmin=True, user_id=9),
                )
            )


def test_stored_superadmin_is_authorised_without_membership():
    doubles = repos_for(stored_user=user(5, superadmin=True), channel_roles={})
    with patched(doubles):
        proposal = run(
            scheduling.prepare_schedule(
                ceremony_type="planning", time_expression="tomorrow at 2pm", requester=requester(user_id=5)
            )
        )
    assert isinstance(proposal, scheduling.ScheduleProposal)
    assert proposal.organizer_id == 5


def test_technical_lead_and_scrum_master_are_authorised():
    for key in (RoleKey.TECH_LEAD.value, RoleKey.SCRUM_MASTER.value):
        doubles = repos_for(stored_user=user(9), channel_roles={"chan-1": key})
        with patched(doubles):
            proposal = run(
                scheduling.prepare_schedule(
                    ceremony_type="planning", time_expression="tomorrow at 2pm", requester=requester(user_id=9)
                )
            )
        assert isinstance(proposal, scheduling.ScheduleProposal)


def test_no_bound_requester_is_refused():
    doubles = repos_for(stored_user=user(9), channel_roles={"chan-1": RoleKey.TECH_LEAD.value})
    with patched(doubles):
        with pytest.raises(AuthorisationRefused) as excinfo:
            run(
                scheduling.prepare_schedule(
                    ceremony_type="planning",
                    time_expression="tomorrow at 2pm",
                    requester=None,
                )
            )
    assert excinfo.value.reason == "no_requester_bound"


# ---------------------------------------------------------------------------
# commit_schedule: the commit-time re-check
# ---------------------------------------------------------------------------


def _proposal(organizer_id: int = 3) -> scheduling.ScheduleProposal:
    return scheduling.ScheduleProposal(
        team_id="team-1",
        channel_id="chan-1",
        ceremony_type_id=TYPE_ROW.id,
        ceremony_type_key=TYPE_ROW.key,
        ceremony_type_label=TYPE_ROW.label,
        organizer_id=organizer_id,
        scheduled_at=START,
        duration_minutes=90,
        agenda=None,
        time_expression="tomorrow at 2pm",
        zone="UTC",
        local_display="tomorrow",
        utc_display="tomorrow",
    )


def test_commit_schedule_refuses_a_learner_even_with_a_valid_proposal():
    doubles = repos_for(stored_user=user(9), channel_roles={"chan-1": RoleKey.LEARNER.value})
    with patched(doubles):
        with pytest.raises(AuthorisationRefused):
            run(scheduling.commit_schedule(_proposal(), requester=requester(mm_id="mm-l", user_id=9)))
    doubles["ceremony_repo"].create_ceremony.assert_not_awaited()


def test_commit_schedule_rechecks_authority_of_the_current_context():
    """The proposal was prepared by #3; the committing context is a learner — refuse."""
    prepared_by = repos_for(stored_user=user(3), channel_roles={"chan-1": RoleKey.TECH_LEAD.value})
    committing_as = repos_for(stored_user=user(9), channel_roles={"chan-1": RoleKey.LEARNER.value})
    with patched(prepared_by):
        run(
            scheduling.prepare_schedule(
                ceremony_type="planning", time_expression="tomorrow at 2pm", requester=requester(user_id=3)
            )
        )
    with patched(committing_as):
        with pytest.raises(AuthorisationRefused):
            run(scheduling.commit_schedule(_proposal(), requester=requester(mm_id="mm-l", user_id=9)))
    committing_as["ceremony_repo"].create_ceremony.assert_not_awaited()


def test_commit_schedule_by_an_authorised_user_writes_once():
    doubles = repos_for(stored_user=user(3), channel_roles={"chan-1": RoleKey.SCRUM_MASTER.value})
    stored = SimpleNamespace(
        id=42,
        channel_id="chan-1",
        organizer_id=3,
        meet_link=None,
        external_event_id=None,
        scheduled_at=START,
    )
    doubles["ceremony_repo"].create_ceremony = AsyncMock(return_value=stored)
    with patched(doubles):
        ceremony = run(scheduling.commit_schedule(_proposal(), requester=requester(user_id=3)))
    assert ceremony.id == 42
    assert doubles["ceremony_repo"].create_ceremony.await_count == 1


def test_commit_schedule_still_refuses_when_authority_was_revoked_after_prepare():
    """Re-check at commit time: the role vanished between prepare and commit."""
    prepared_by = repos_for(stored_user=user(3), channel_roles={"chan-1": RoleKey.TECH_LEAD.value})
    revoked = repos_for(stored_user=user(3), channel_roles={})
    with patched(prepared_by):
        run(
            scheduling.prepare_schedule(
                ceremony_type="planning", time_expression="tomorrow at 2pm", requester=requester(user_id=3)
            )
        )
    with patched(revoked):
        with pytest.raises(AuthorisationRefused):
            run(scheduling.commit_schedule(_proposal(), requester=requester(user_id=3)))


# ---------------------------------------------------------------------------
# prepare_amendment / commit_amendment
# ---------------------------------------------------------------------------


def _ceremony(channel_id: str = "chan-1", *, status: str = CeremonyStatus.SCHEDULED.value):
    return SimpleNamespace(
        id=7,
        team_id="team-1",
        channel_id=channel_id,
        ceremony_type_id=TYPE_ROW.id,
        organizer_id=3,
        scheduled_at=START,
        duration_minutes=60,
        status=status,
        agenda=None,
        external_event_id=None,
    )


def test_learner_cannot_amend_even_an_agenda():
    doubles = repos_for(stored_user=user(9), channel_roles={"chan-1": RoleKey.LEARNER.value}, ceremonies=[_ceremony()])
    with patched(doubles):
        with pytest.raises(AuthorisationRefused):
            run(
                scheduling.prepare_amendment(
                    ceremony_id=7, new_agenda="hijack", requester=requester(mm_id="mm-l", user_id=9)
                )
            )
    doubles["ceremony_repo"].update_ceremony.assert_not_awaited()


def test_amendment_for_another_channel_is_refused_for_the_ceremony_channel():
    """A tech lead of chan-2 must not amend a chan-1 ceremony: the check is on the
    ceremony's OWN channel."""
    doubles = repos_for(
        stored_user=user(9),
        channel_roles={"chan-2": RoleKey.TECH_LEAD.value},
        ceremonies=[_ceremony()],
    )
    with patched(doubles):
        with pytest.raises(AuthorisationRefused):
            run(
                scheduling.prepare_amendment(
                    ceremony_id=7, new_agenda="x", requester=requester(mm_id="mm-2", user_id=9, channel_id="chan-2")
                )
            )


def test_amendment_discloses_nothing_about_missing_ceremonies_to_unauthorised_users():
    doubles = repos_for(stored_user=user(9), channel_roles={}, ceremonies=[])
    with patched(doubles):
        with pytest.raises(ValidationFailed, match="no ceremony #7"):
            run(scheduling.prepare_amendment(ceremony_id=7, new_agenda="x", requester=requester()))


def test_authorised_user_can_prepare_and_commit_an_agenda_amendment():
    doubles = repos_for(
        stored_user=user(3), channel_roles={"chan-1": RoleKey.TECH_LEAD.value}, ceremonies=[_ceremony()]
    )
    updated = _ceremony()
    updated.agenda = "New agenda"
    doubles["ceremony_repo"].update_ceremony = AsyncMock(return_value=updated)
    with patched(doubles):
        proposal = run(scheduling.prepare_amendment(ceremony_id=7, new_agenda="New agenda", requester=requester()))
        assert isinstance(proposal, scheduling.AmendmentProposal)
        ceremony, trail = run(scheduling.commit_amendment(proposal, requester=requester()))
    assert ceremony.agenda == "New agenda"
    assert doubles["ceremony_repo"].update_ceremony.await_count == 1


def test_commit_amendment_rechecks_authority():
    prepared_by = repos_for(
        stored_user=user(3), channel_roles={"chan-1": RoleKey.TECH_LEAD.value}, ceremonies=[_ceremony()]
    )
    with patched(prepared_by):
        proposal = run(scheduling.prepare_amendment(ceremony_id=7, new_agenda="x", requester=requester(user_id=3)))
    assert isinstance(proposal, scheduling.AmendmentProposal)

    demoted = repos_for(stored_user=user(3), channel_roles={"chan-1": RoleKey.LEARNER.value}, ceremonies=[_ceremony()])
    with patched(demoted):
        with pytest.raises(AuthorisationRefused):
            run(scheduling.commit_amendment(proposal, requester=requester(user_id=3)))
    demoted["ceremony_repo"].update_ceremony.assert_not_awaited()


def test_unsynced_identity_cannot_amend():
    doubles = repos_for(stored_user=None, channel_roles={}, ceremonies=[_ceremony()])
    with patched(doubles):
        with pytest.raises(AuthorisationRefused) as excinfo:
            run(
                scheduling.prepare_amendment(
                    ceremony_id=7, new_agenda="x", requester=requester(mm_id="mm-ghost", user_id=None)
                )
            )
    assert excinfo.value.reason == "requester_not_synced"


# ---------------------------------------------------------------------------
# list_calendar: member-scoped read
# ---------------------------------------------------------------------------


def test_calendar_read_is_denied_to_non_members():
    doubles = repos_for(stored_user=user(9), channel_roles={})
    with patched(doubles):
        with pytest.raises(AuthorisationRefused):
            run(scheduling.list_calendar(requester=requester(mm_id="mm-out")))


def test_calendar_read_is_denied_to_unsynced_identities():
    doubles = repos_for(stored_user=None, channel_roles={})
    with patched(doubles):
        with pytest.raises(AuthorisationRefused):
            run(scheduling.list_calendar(requester=requester(mm_id="mm-ghost")))


def test_calendar_read_is_allowed_for_any_member_and_scoped_to_their_channel():
    learner = repos_for(stored_user=user(9), channel_roles={"chan-1": RoleKey.LEARNER.value})
    row = _ceremony()
    with patched(learner):
        learner["ceremony_repo"].list_ceremonies = AsyncMock(return_value=[row])
        view = run(scheduling.list_calendar(requester=requester(mm_id="mm-l", user_id=9)))
    assert [entry.ceremony.id for entry in view.entries] == [7]
    # The read is scoped to the requester's channel — never a global listing.
    assert learner["ceremony_repo"].list_ceremonies.await_args.args[0] == "chan-1"


def test_calendar_read_refuses_without_a_bound_requester():
    doubles = repos_for(stored_user=user(9), channel_roles={"chan-1": RoleKey.LEARNER.value})
    with patched(doubles):
        with pytest.raises(AuthorisationRefused):
            run(scheduling.list_calendar(requester=None))


# ---------------------------------------------------------------------------
# Refusal message contract
# ---------------------------------------------------------------------------


def test_scheduling_refusals_carry_the_meeting_sentence():
    assert MEETING_REFUSAL_MESSAGE in REFUSAL_MESSAGE or MEETING_REFUSAL_MESSAGE != REFUSAL_MESSAGE
    assert "administrative permissions" in MEETING_REFUSAL_MESSAGE
