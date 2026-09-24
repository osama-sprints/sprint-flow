"""Create test data for verify_announcement_bot_flow.py.

two users, one
tech_lead and one learner, both active members of Cohort 1's channel.

Uses only real, already-confirmed functions — upsert_mattermost_user (safe to
re-run, keyed on mattermost_user_id) and upsert_channel_role (also safe to
re-run, idempotent per the docstring in channels.py).

Usage: uv run python scripts/create_test_data.py
"""

import asyncio

from app.services.database import session_scope
from app.services.domain import channels as channel_repo
from app.services.domain import identity as identity_repo

# EDIT if your test cohort's channel_id differs from what
# list_cohorts_and_members.py showed you.
TEST_CHANNEL_ID = "test-channel-1"
TEST_TEAM_ID = "test-team-1"  # not a real Mattermost team; only used as a DB column here

TECH_LEAD_MATTERMOST_ID = "mm-user-techlead-1"
LEARNER_MATTERMOST_ID = "mm-user-learner-1"
OTHER_TECH_LEAD_MATTERMOST_ID = "mm-user-techlead-2"


async def main():
    async with session_scope() as session:
        tech_lead_role = await channel_repo.get_role_by_key("tech_lead", session=session)
        learner_role = await channel_repo.get_role_by_key("learner", session=session)

        if tech_lead_role is None or learner_role is None:
            print("ERROR: 'tech_lead' or 'learner' role not found in the roles table.")
            print("These should have been seeded already — check scripts/seed_reference_data.py.")
            return

        tech_lead = await identity_repo.upsert_mattermost_user(
            mattermost_user_id=TECH_LEAD_MATTERMOST_ID,
            username="test_techlead",
            email=None,
            display_name="Test Tech Lead",
            timezone=None,
            is_superadmin=False,
            session=session,
        )
        learner = await identity_repo.upsert_mattermost_user(
            mattermost_user_id=LEARNER_MATTERMOST_ID,
            username="test_learner",
            email=None,
            display_name="Test Learner",
            timezone=None,
            is_superadmin=False,
            session=session,
        )
        other_tech_lead = await identity_repo.upsert_mattermost_user(
            mattermost_user_id=OTHER_TECH_LEAD_MATTERMOST_ID,
            username="test_techlead_2",
            email=None,
            display_name="Test Tech Lead 2",
            timezone=None,
            is_superadmin=False,
            session=session,
        )

        assert tech_lead.id is not None
        assert learner.id is not None
        assert other_tech_lead.id is not None

        await channel_repo.upsert_channel_role(
            user_id=tech_lead.id,
            team_id=TEST_TEAM_ID,
            channel_id=TEST_CHANNEL_ID,
            role_id=tech_lead_role.id,
            assigned_by_id=None,
            session=session,
        )
        await channel_repo.upsert_channel_role(
            user_id=learner.id,
            team_id=TEST_TEAM_ID,
            channel_id=TEST_CHANNEL_ID,
            role_id=learner_role.id,
            assigned_by_id=None,
            session=session,
        )
        await channel_repo.upsert_channel_role(
            user_id=other_tech_lead.id,
            team_id=TEST_TEAM_ID,
            channel_id=TEST_CHANNEL_ID,
            role_id=tech_lead_role.id,
            assigned_by_id=None,
            session=session,
        )

        print("Done. Use these in verify_announcement_bot_flow.py:")
        print(f"  TECH_LEAD_MATTERMOST_ID = {TECH_LEAD_MATTERMOST_ID!r}   (user_id={tech_lead.id})")
        print(f"  LEARNER_MATTERMOST_ID   = {LEARNER_MATTERMOST_ID!r}   (user_id={learner.id})")
        print(f"  OTHER_TECH_LEAD (scenario 4) = {OTHER_TECH_LEAD_MATTERMOST_ID!r}   (user_id={other_tech_lead.id})")


if __name__ == "__main__":
    asyncio.run(main())
