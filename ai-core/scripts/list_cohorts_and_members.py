"""List real cohorts (sprints) and their channel members.

Finds real IDs
for scripts/verify_announcement_bot_flow.py.

Usage: uv run python scripts/list_cohorts_and_members.py
"""

import asyncio

from sqlmodel import select

from app.models import Role, User
from app.models.enums import MembershipStatus
from app.models.sprint import Sprint
from app.services.database import session_scope

try:
    from app.models import ChannelRole
except ImportError:
    ChannelRole = None


async def main():
    async with session_scope() as session:
        result = await session.exec(select(Sprint))
        sprints = list(result.all())

        if not sprints:
            print("No cohorts/sprints exist in the database yet.")
            print("Create one for real in Mattermost before running the verification script.")
            return

        print(f"Found {len(sprints)} cohort(s):\n")
        for sprint in sprints:
            print(f"  id={sprint.id}  name={sprint.name!r}  status={sprint.status}  channel_id={sprint.channel_id}")

        if ChannelRole is None:
            print("\n(Could not import ChannelRole to list members — check app/models/__init__.py)")
            return

        print("\n--- Members per cohort ---")
        for sprint in sprints:
            stmt = (
                select(ChannelRole, User, Role)
                .join(User, User.id == ChannelRole.user_id)
                .join(Role, Role.id == ChannelRole.role_id)
                .where(
                    ChannelRole.channel_id == sprint.channel_id,
                    ChannelRole.status == MembershipStatus.ACTIVE.value,
                )
            )
            result = await session.exec(stmt)
            rows = list(result.all())
            print(f"\nCohort id={sprint.id} ({sprint.name!r}), channel_id={sprint.channel_id}:")
            if not rows:
                print("  (no active members)")
            for membership, user, role in rows:  # noqa: B007
                print(f"  user_id={user.id}  mattermost_user_id={user.mattermost_user_id}  role={role.key}")


if __name__ == "__main__":
    asyncio.run(main())
