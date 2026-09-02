#!/usr/bin/env python3
"""Verify the SprintFlow domain schema."""

import sys

from sqlalchemy import inspect
from sqlmodel import Session

from app.services.database import database_service


EXPECTED_TABLES = {
    "user",
    "role",
    "cohort",
    "cohortmembership",
    "sprint",
    "ceremony",
    "ceremonytype",
    "dailyprogress",
    "escalation",
    "session",
    "thread",
}


def main() -> int:
    inspector = inspect(database_service.engine)
    tables = set(inspector.get_table_names())

    missing = EXPECTED_TABLES - tables
    if missing:
        print(f"FAIL: missing tables: {sorted(missing)}")
        return 1

    if "person" in tables:
        print("FAIL: obsolete 'person' table still exists")
        return 1

    required_columns = {
        "user": {"mattermost_user_id", "handle"},
        "cohortmembership": {"user_id"},
        "dailyprogress": {
            "what_i_did",
            "what_i_will_do",
            "blockers",
        },
        "escalation": {
            "question",
            "assigned_human_id",
            "original_thread_id",
            "human_dm_thread_id",
        },
        "ceremony": {
            "organizer",
            "agenda",
            "scheduled_at",
            "duration_mins",
        },
    }

    for table, expected in required_columns.items():
        actual = {column["name"] for column in inspector.get_columns(table)}
        missing_columns = expected - actual

        if missing_columns:
            print(
                f"FAIL: {table} missing columns: "
                f"{sorted(missing_columns)}"
            )
            return 1

    with Session(database_service.engine):
        pass

    print("SCHEMA VERIFICATION PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())