"""Seed (or re-seed) the reference data. Safe to run any number of times.

Run inside the container, from any working directory:

    docker compose exec -T ai-core /app/.venv/bin/python /app/scripts/seed_reference_data.py

or via the root Makefile: ``make seed``. The migration that creates the lookup
tables already seeds them; this exists to refresh labels or restore a row that
was removed by hand.
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.database import database_service  # noqa: E402
from app.services.domain.reference_data import seed_reference_data  # noqa: E402


async def main() -> int:
    """Seed and report.

    Returns:
        int: Process exit code.
    """
    summary = await seed_reference_data()
    await database_service.close()
    print(f"Reference data seeded: {summary.roles} roles, {summary.ceremony_types} ceremony types.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
