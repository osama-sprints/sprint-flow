# Sprint 4 — Automated Testing Report

## Scope

Automated coverage was added for three Sprint 4 capabilities:

1. Data model and migrations
2. Escalation — handing a question to a human
3. Calendar integration for scheduled ceremonies

Tests are deterministic and do not require live external services. PostgreSQL
integration tests use a real database with Alembic migrations applied.

## Data Model and Migrations

`tests/test_schema.py` covers migration metadata, domain tables, seed
consistency/idempotency, cohort-scoped roles, membership constraints,
timestamps, externally owned tables, and migration behavior.

`tests/integration/test_schema_db.py` verifies these behaviors against real
PostgreSQL, including tables, columns, constraints, persistence, and
migration behavior.

**Result: 33 unit tests passed, 8 PostgreSQL integration tests passed.**

## Escalation

`tests/test_escalation_dispatch.py` covers:

- human routing
- private Mattermost handoff
- correlation metadata
- escalation references
- duplicate/idempotent escalation handling
- already-open and resolved tickets
- missing-human behavior
- Mattermost failure fallback
- invalid escalation context
- learner-facing results

Mattermost operations are mocked, so no live Mattermost server is required.

**Result: 9 tests passed.**

## Calendar Integration

`tests/integration/test_calendar_integration_db.py` uses real PostgreSQL while
mocking the Google API boundary.

It verifies:

- ceremony creation
- rescheduling
- cancellation
- provider failure isolation
- bounded retries
- disabled integration behavior
- local persistence when the provider fails
- avoiding unnecessary external calls when disabled

**Result: 5 PostgreSQL integration tests passed.**

## Single command (full offline suite)

cd ai-core && uv run pytest -q -rs tests/test_schema.py tests/test_escalation_dispatch.py tests/integration/test_schema_db.py tests/integration/test_calendar_integration_db.py

Offline result: 42 passed, 13 skipped. The 13 are PostgreSQL integration tests and
run with SPRINTFLOW_INTEGRATION_DB=1 against a migrated throwaway database (55 passed total).
CI runs the offline command on every PR. A Postgres-backed CI job is deferred: `alembic upgrade head`
currently fails on a fresh database (empty announcements migration, reported separately).

## Invariants and edge cases

Escalation
- Routing is channel-scoped: only the requesting channel's tech lead is contacted.
- The learner-facing reply never reveals the human's identity.
- No human in the role: the ticket is still opened and no DM is sent.
- DM failure: the ticket stays open and assigned, never lost.
- Idempotent per thread: a replay does not reopen the ticket or re-DM.
- Concurrency: a concurrent trigger loses the insert race and reports the winner.
- Invalid input (empty question, missing channel, unsynced learner) fails before any side effect.

Calendar integration (PostgreSQL is the source of truth)
- Create stores the meet link and external event id.
- Reschedule and cancel reuse the stored event id (no new external event).
- Provider outage never blocks local persistence; retries are bounded.
- Disabled mode: fields stay null and no external call is made.

Data model and migrations
- Tables match model metadata, in dependency order; downgrade drops exactly the domain tables.
- Every datetime column is timezone-aware; constraints are named.
- One role per person per channel is enforced by the database.
- Outbox key treats a null channel as a value; checkpointer tables are never touched.
- Concurrency: identity upserts, step enqueue and first-role assignment each converge on one row.
- SKIP LOCKED gives concurrent workers disjoint claims; escalation references stay unique.
- Ceremony/sprint overlap boundaries and case-insensitive sprint names are enforced.


## Exact Commands

Sprint 4 capability tests:

`uv run pytest -q tests/test_escalation_dispatch.py tests/integration/test_calendar_integration_db.py tests/integration/test_schema_db.py`

PostgreSQL integration tests:

`SPRINTFLOW_INTEGRATION_DB=1 uv run pytest -q tests/integration/test_calendar_integration_db.py tests/integration/test_schema_db.py`

Schema unit tests:

`uv run pytest -q tests/test_schema.py`

The verified Sprint 4 suites contain **55 passing tests** in total.

## Uncovered Areas

The automated tests do not verify:

- real Google Calendar/Meet API calls with production credentials
- a real Mattermost websocket/API handoff
- external provider availability beyond deterministic failure scenarios
- unrelated capabilities outside the three Sprint 4 areas

The full repository command is:

`uv run pytest -q tests/`

The full repository currently has unrelated pre-existing failures outside the
Sprint 4 capabilities, so the verified acceptance scope is the Sprint 4
capability-focused unit and PostgreSQL integration suites.