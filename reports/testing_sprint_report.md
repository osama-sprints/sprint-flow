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