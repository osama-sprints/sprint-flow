"""READ-ONLY helper: report whether a cohort name exists and how many memberships a Mattermost user holds.

Usage (piped over stdin by ``scripts/verify_authorisation.py``):

    docker compose exec -T ai-core /app/.venv/bin/python - "<cohort name>" "<mattermost user id>" \
        < scripts/_cohort_lookup_probe.py

Prints one JSON object: ``{"cohort_exists": bool, "memberships": int}``.
"""

import asyncio
import json
import os
import sys

for _candidate in ("/app", os.path.join(os.getcwd(), "ai-core"), os.getcwd()):
    if os.path.isdir(os.path.join(_candidate, "app")):
        sys.path.insert(0, _candidate)
        break

from app.services.database import database_service  # noqa: E402
from app.services.domain import cohorts as cohort_repo  # noqa: E402
from app.services.domain import identity as identity_repo  # noqa: E402


async def main() -> None:
    """Look up the cohort and the person, print the answer, release the engine."""
    cohort_name = sys.argv[1] if len(sys.argv) > 1 else ""
    mattermost_user_id = sys.argv[2] if len(sys.argv) > 2 else ""
    try:
        cohort = await cohort_repo.get_cohort_by_name(cohort_name) if cohort_name else None
        memberships = 0
        if mattermost_user_id:
            user = await identity_repo.get_user_by_mattermost_id(mattermost_user_id)
            if user is not None and user.id is not None:
                memberships = len(await cohort_repo.list_memberships_for_user(user.id, active_only=False))
        print(json.dumps({"cohort_exists": cohort is not None, "memberships": memberships}))
    finally:
        await database_service.close()


asyncio.run(main())
