# Sprint 4 Test Report

## Covered capabilities

- Ceremony scheduling through conversation: authorization, timezone-aware interpretation, ambiguity handling, conflict and duplicate detection, strict confirmation before writes, amendment rules, and persisted UTC instants.
- Proactive ceremony reminders: 24-hour and 1-hour windows, downtime catch-up, timezone formatting and UTC fallback, cancelled/inactive/empty scopes, delivery failure behavior, and durable exactly-once rows across repeated polls.
- Proactive daily standups: timezone-aware dispatch, idempotent prompt creation, leases and retries, active learner filtering, reply attribution and duplicate/late handling, deterministic parsing, WebSocket interception before the general agent path, and concurrent delivery settlement.

## Execution

The single documented unit command is:

```bash
cd ai-core && uv run pytest tests/
```

PostgreSQL integration tests run against a migrated throwaway database with:

```bash
SPRINTFLOW_INTEGRATION_DB=1 uv run pytest tests/integration/
```

All external LLM, embedding, and Mattermost calls in these tests are replaced by fakes or mocks. Time and parser inputs are explicit and deterministic.

The offline suite currently reports `515 passed, 61 skipped, 16 xfailed, 3 xpassed`. The PostgreSQL integration suite passes against a fresh migrated database with `49 passed, 12 xfailed`. The 12 xfails are documented legacy back-office/orchestration tests outside this Sprint 4 scope.

## Remaining coverage

The suites cannot prove live Mattermost behavior. A process crash after Mattermost accepts a post but before database settlement can still cause a retry because the Mattermost API has no idempotency key in this path; database locks prevent concurrent-worker duplicates during normal execution. Live API behavior remains covered by the existing verification probes.