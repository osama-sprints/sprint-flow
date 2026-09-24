"""In-container state lookup for the live authorisation checks: one JSON line.

Given a Mattermost user id, reports the stored (``users``) facts the live
prompt-injection check asserts in the channels era: whether the account is
synced, whether it is a superadmin, and how many ``channel_roles`` rows it
holds. There is no ``cohorts``/``cohort_memberships`` table to check.
"""

import asyncio
import json
import os
import sys

for _candidate in ("/app", os.path.join(os.getcwd(), "ai-core"), os.getcwd()):
    if os.path.isdir(os.path.join(_candidate, "app")):
        sys.path.insert(0, _candidate)
        break

from sqlalchemy import text  # noqa: E402

from app.services.database import database_service  # noqa: E402


async def main(mattermost_user_id: str) -> int:
    """Print the DB state for the account as one JSON line."""
    async with database_service.engine.connect() as conn:
        row = (
            await conn.execute(
                text("SELECT is_superadmin FROM users WHERE mattermost_user_id = :m"), {"m": mattermost_user_id}
            )
        ).first()
        roles = (
            await conn.execute(
                text(
                    "SELECT count(*) FROM channel_roles WHERE user_id IN "
                    "(SELECT id FROM users WHERE mattermost_user_id = :m)"
                ),
                {"m": mattermost_user_id},
            )
        ).scalar_one()
    print(
        json.dumps(
            {
                "user_synced": row is not None,
                "is_superadmin": bool(row[0]) if row else False,
                "channel_roles": roles,
            }
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1])))