# Sprint 4 Testing Report

**Owner:** Mohamed Ayman
**Branch verified:** `dev`
**Latest repository revision observed:** `cba2834`
**Date:** 24 September 2026

## 1. Executive Summary

Sprint 4 added automated regression protection for the three assigned capabilities:

1. Ceremony scheduling through conversation.
2. Proactive ceremony reminders.
3. Proactive daily standup collection and parsing.

The tests run offline and deterministically for unit coverage. PostgreSQL integration tests run against a fresh database with Alembic migrations applied. Mattermost, LLM, embedding, and Google API boundaries are replaced by fakes or mocks in pytest. The root CI workflow runs unit tests, PostgreSQL integration tests, Ruff, formatting checks, and Pyright on pull requests and pushes to `main`.

## 2. Verified Results

### Automated pytest results

```text
Offline suite:       515 passed, 61 skipped, 16 xfailed, 3 xpassed
PostgreSQL suite:     49 passed, 12 xfailed
```

The PostgreSQL suite was run against an isolated database named `sprintflow_sprint4_test`, migrated to Alembic head `d32cf4519a86`. The existing development database was not reset.

The 12 integration xfails are documented legacy back-office and orchestration tests outside the three Sprint 4 capabilities. They fail because older tests still expect the removed legacy channels registry or the previous orchestration route name.

### Live verification results

| Verifier | Result | Details |
|---|---:|---|
| `scripts/verify_standups.py` | PASS | 40/40 checks passed, including delivery, parsing, duplicates, late replies, timezone scheduling, inactive-member filtering, dispatcher delivery, and cleanup. |
| `scripts/verify_calendar_integration.py` | PASS | All calendar create, reschedule, cancellation, provider outage, retry, persistence, and disabled-mode checks passed. |
| `scripts/verify_scheduling.py` | PARTIAL | The service probe and real Mattermost confirmation flow passed. One calendar display assertion failed because the verifier expected different wording; the bot returned the correct amended ceremony and local/UTC time, and database persistence was correct. |

## 3. Capability A: Ceremony Scheduling Through Conversation

### Behavior covered

- Scheduling requires an authorized requester.
- Authority is channel-scoped.
- Learners and non-members cannot schedule ceremonies.
- Ceremony type names and aliases are normalized.
- Time expressions are interpreted with an explicit timezone.
- Profile timezone takes precedence over the configured default when available.
- Missing or ambiguous timezone/time input produces clarification instead of a write.
- Ambiguous values such as `tomorrow at 2` require AM/PM clarification.
- Confirmation displays both local time and UTC.
- No database row is written while waiting for confirmation.
- `no` and unclear confirmations do not write.
- `yes` creates exactly one ceremony.
- Duplicate ceremonies are refused.
- Overlapping ceremonies follow the configured conflict policy.
- Future amendments and cancellations require confirmation.
- Past ceremonies cannot be moved or cancelled.
- Amendment history is persisted for auditability.
- Stored scheduled instants are timezone-aware UTC values.
- Calendar create, update, cancel, outage, retry, and disabled modes are covered.

### Production files

- [ai-core/app/services/time_interpretation.py](../ai-core/app/services/time_interpretation.py): parses natural-language ceremony times and timezone input.
- [ai-core/app/services/ceremony_scheduling.py](../ai-core/app/services/ceremony_scheduling.py): authorization-aware preparation, conflict policy, confirmation proposals, persistence, amendments, and rendering.
- [ai-core/app/services/authorisation.py](../ai-core/app/services/authorisation.py): stored-data authorization and channel scope checks.
- [ai-core/app/services/domain/ceremonies.py](../ai-core/app/services/domain/ceremonies.py): ceremony and amendment database access.
- [ai-core/app/core/langgraph/tools/ceremonies.py](../ai-core/app/core/langgraph/tools/ceremonies.py): conversation tools and confirmation interrupts.
- [ai-core/app/models/ceremony.py](../ai-core/app/models/ceremony.py): ceremony database model.
- [ai-core/app/models/ceremony_type.py](../ai-core/app/models/ceremony_type.py): ceremony type model and labels.
- [ai-core/app/models/ceremony_amendment.py](../ai-core/app/models/ceremony_amendment.py): amendment audit model.
- [ai-core/app/services/google_meet.py](../ai-core/app/services/google_meet.py): optional meeting-link creation and provider retry handling.
- [ai-core/app/main.py](../ai-core/app/main.py): application lifecycle wiring.

### Schema and migration files

- [ai-core/alembic/versions/0001_sprintflow_domain_schema.py](../ai-core/alembic/versions/0001_sprintflow_domain_schema.py): core domain tables.
- [ai-core/alembic/versions/c00000000002_add_meet_link_to_ceremonies.py](../ai-core/alembic/versions/c00000000002_add_meet_link_to_ceremonies.py): meeting link column.
- [ai-core/alembic/versions/d00000000001_add_external_event_id_to_ceremonies.py](../ai-core/alembic/versions/d00000000001_add_external_event_id_to_ceremonies.py): external calendar event identity.
- [ai-core/alembic/versions/e40272b244ca_remove_sprint_id_from_ceremonies.py](../ai-core/alembic/versions/e40272b244ca_remove_sprint_id_from_ceremonies.py): current ceremony/channel relationship migration.

### Tests and probes

- [ai-core/tests/test_time_interpretation.py](../ai-core/tests/test_time_interpretation.py): deterministic time and timezone parsing.
- [ai-core/tests/test_ceremony_scheduling.py](../ai-core/tests/test_ceremony_scheduling.py): conflict policy, timezone precedence, amendments, and rendering.
- [ai-core/tests/test_ceremonies_confirm.py](../ai-core/tests/test_ceremonies_confirm.py): confirmation ownership and answer handling.
- [ai-core/tests/test_google_meet.py](../ai-core/tests/test_google_meet.py): meeting link and reminder message behavior.
- [ai-core/tests/integration/test_ceremony_scheduling_db.py](../ai-core/tests/integration/test_ceremony_scheduling_db.py): real PostgreSQL, LangGraph confirmation flow, authorization, persistence, duplicate and amendment behavior.
- [scripts/verify_scheduling.py](../scripts/verify_scheduling.py): service probe plus live Mattermost scheduling conversation.
- [scripts/_scheduling_probe.py](../scripts/_scheduling_probe.py): deterministic in-container scheduling probe.
- [scripts/verify_calendar_integration.py](../scripts/verify_calendar_integration.py): calendar provider integration verifier.
- [scripts/_calendar_probe.py](../scripts/_calendar_probe.py): mocked Google API boundary probe.
- [reports/scheduling_report.md](scheduling_report.md): scheduling feature report.

## 4. Capability B: Proactive Ceremony Reminders

### Behavior covered

- 24-hour and 1-hour reminder windows.
- Poll margin handling for a five-minute background polling interval.
- Downtime catch-up for ceremonies still in the future.
- Local timezone formatting with UTC fallback for invalid zones.
- Organizer, agenda, meeting-link, local-time, and UTC content in reminder messages.
- Cancelled ceremonies are excluded.
- Empty channels are skipped safely.
- Inactive memberships are excluded.
- Failed DM channel creation or post creation does not record a sent reminder.
- Sequential restart/re-poll deduplication.
- Concurrent worker deduplication using a PostgreSQL advisory transaction lock.
- Unique `(ceremony_id, recipient_mm_id, window)` database protection.

### Production files

- [ai-core/app/services/ceremony_reminders.py](../ai-core/app/services/ceremony_reminders.py): reminder windows, due queries, member filtering, message construction, advisory lock, send, and idempotency record.
- [ai-core/app/services/domain/ceremonies.py](../ai-core/app/services/domain/ceremonies.py): ceremony and type lookups.
- [ai-core/app/services/domain/channels.py](../ai-core/app/services/domain/channels.py): active channel-member lookup.
- [ai-core/app/models/ceremony_reminder.py](../ai-core/app/models/ceremony_reminder.py): durable sent-reminder model and unique constraint.
- [ai-core/app/models/ceremony.py](../ai-core/app/models/ceremony.py): scheduled ceremony source model.
- [ai-core/app/main.py](../ai-core/app/main.py): background poller lifecycle startup and shutdown.

### Schema file

- [ai-core/alembic/versions/c00000000001_add_ceremony_reminder_table.py](../ai-core/alembic/versions/c00000000001_add_ceremony_reminder_table.py): creates the reminder table and uniqueness constraint.

### Tests and documentation

- [ai-core/tests/test_ceremony_reminders.py](../ai-core/tests/test_ceremony_reminders.py): window boundaries, catch-up, cancellation filtering, duplicate protection, timezone formatting, and fallback behavior.
- [ai-core/tests/integration/test_ceremony_reminders_db.py](../ai-core/tests/integration/test_ceremony_reminders_db.py): real PostgreSQL poller, active/inactive scope, empty channels, failed posts, repeated polls, and concurrent workers.
- [reports/reminders_report.md](reminders_report.md): reminder feature report.
- [reports/sprint4_manual_test_script.md](sprint4_manual_test_script.md): manual Mattermost reminder steps.

There is no separate live Mattermost-only reminder verifier. Reminder persistence and delivery are covered by the PostgreSQL integration suite, while live scheduling and the running poller are exercised through the stack and manual script.

## 5. Capability C: Proactive Daily Standup Collection and Parsing

### Behavior covered

- Prompt creation at the learner's configured local hour.
- UTC dispatch conversion across timezones.
- Idempotent prompt creation per sprint, learner, and local date.
- Claim leases and retry backoff.
- Concurrent delivery settlement with a prompt-specific PostgreSQL advisory lock.
- Active learner and active sprint filtering.
- Inactive memberships do not receive prompts.
- DM-only reply attribution through the Mattermost WebSocket listener.
- Explicit bot questions continue to the normal assistant path.
- Structured numbered parsing (`1`, `2`, `3`).
- Labeled parsing (`Done`, `Plan`, `Blockers`).
- Flat fallback parsing.
- Raw reply preservation.
- First accepted submission wins.
- Duplicate same-day replies are retained and classified as duplicate.
- Replies after day closure are retained and classified as late.
- Redelivery of the same Mattermost post is idempotent.
- Missed days close without fabricating a standup entry.

### Production files

- [ai-core/app/services/standups.py](../ai-core/app/services/standups.py): timezone logic, prompt content, delivery, locking, parser, and reply ingestion.
- [ai-core/app/services/domain/standups.py](../ai-core/app/services/domain/standups.py): prompt claim/lease, delivery state, replies, and standup entry persistence.
- [ai-core/app/services/domain/sprints.py](../ai-core/app/services/domain/sprints.py): active sprint lookup.
- [ai-core/app/services/domain/channels.py](../ai-core/app/services/domain/channels.py): active learner membership lookup.
- [ai-core/app/workers/standup_dispatcher.py](../ai-core/app/workers/standup_dispatcher.py): ensure, claim, deliver, retry, and missed-day dispatch pass.
- [ai-core/app/services/mattermost_ws.py](../ai-core/app/services/mattermost_ws.py): WebSocket event interception and normal-agent fallback.
- [ai-core/app/core/langgraph/tools/standups.py](../ai-core/app/core/langgraph/tools/standups.py): standup summary conversation tool.
- [ai-core/app/core/config.py](../ai-core/app/core/config.py): standup feature flags, local prompt hour, lease, retry, and attempt settings.
- [ai-core/app/core/metrics.py](../ai-core/app/core/metrics.py): delivery outcome metrics.
- [ai-core/app/main.py](../ai-core/app/main.py): dispatcher and WebSocket lifecycle wiring.
- [ai-core/app/models/daily_standup.py](../ai-core/app/models/daily_standup.py): saved standup entry model.
- [ai-core/app/models/daily_standup_prompt.py](../ai-core/app/models/daily_standup_prompt.py): prompt state model.
- [ai-core/app/models/standup_reply.py](../ai-core/app/models/standup_reply.py): raw reply and attribution model.

### Schema file

- [ai-core/alembic/versions/0005_daily_standup_prompts.py](../ai-core/alembic/versions/0005_daily_standup_prompts.py): prompt, reply, and provenance schema.

### Tests, probes, and documentation

- [ai-core/tests/test_standups.py](../ai-core/tests/test_standups.py): timezone, parser, acknowledgement, and mention behavior.
- [ai-core/tests/test_standup_summary.py](../ai-core/tests/test_standup_summary.py): channel standup summary behavior.
- [ai-core/tests/test_mattermost_ws_standup.py](../ai-core/tests/test_mattermost_ws_standup.py): accepted standup interception and normal-DM fallback.
- [ai-core/tests/integration/test_standups_db.py](../ai-core/tests/integration/test_standups_db.py): real PostgreSQL lifecycle, leases, delivery, retries, duplicate/late replies, inactive filtering, and concurrent delivery.
- [scripts/verify_standups.py](../scripts/verify_standups.py): 40-check live database probe with fake Mattermost.
- [scripts/_standups_probe.py](../scripts/_standups_probe.py): deterministic in-container standup probe.
- [reports/standups_report.md](standups_report.md): standup feature report.
- [reports/standup_scenarios.md](standup_scenarios.md): scenario coverage.
- [reports/manual_standup_gui_test.md](manual_standup_gui_test.md): manual GUI scenarios.
- [reports/sprint4_manual_test_script.md](sprint4_manual_test_script.md): copy-paste manual test conversation.

## 6. Shared CI, Make, and Execution Files

### Root CI

- [.github/workflows/ci.yaml](../.github/workflows/ci.yaml): runs on pull requests and pushes to `main`; starts PostgreSQL and ai-core, applies migrations, runs unit tests, runs PostgreSQL integration tests, checks Ruff, checks formatting, and runs Pyright.

### ai-core CI

- [ai-core/.github/workflows/ci.yaml](../ai-core/.github/workflows/ci.yaml): runs Ruff, formatting, Pyright, and the offline unit suite when used directly.

### Makefiles

- [Makefile](../Makefile): root Docker-based `test`, `test-integration`, `lint`, `typecheck`, `verify-fast`, and full `verify` targets. The full verifier chain includes scheduling, calendar integration, and standups.
- [ai-core/Makefile](../ai-core/Makefile): local `uv run pytest`, integration, lint, format, typecheck, and container lifecycle targets.

### Primary commands

Offline tests:

```bash
cd ai-core
uv run pytest tests/
```

PostgreSQL integration tests:

```bash
cd ai-core
SPRINTFLOW_INTEGRATION_DB=1 uv run pytest tests/integration/
```

Fast local gate from the repository root:

```bash
make verify-fast
```

Full stack verification:

```bash
make verify
```

## 7. Manual Test Guide

The complete copy-paste Mattermost procedure is in [reports/sprint4_manual_test_script.md](sprint4_manual_test_script.md). It covers:

- Scheduling and confirmation.
- Declined and unclear confirmation.
- Timezone and UTC display.
- Duplicate and conflict handling.
- Amendments and cancellation.
- Reminder timing, restart, cancellation, and member scope.
- Standup prompt content.
- Numbered, labeled, and flat replies.
- Duplicate and late replies.
- Inactive learner filtering.
- SQL inspection of ceremonies, reminder rows, prompts, and standup entries.

## 8. Known Limitations and Out-of-Scope Results

1. The 12 integration xfails are legacy back-office/orchestration tests outside this Sprint 4 assignment. They are documented rather than silently skipped.
2. Unit-test skips are expected when database integration tests are run without `SPRINTFLOW_INTEGRATION_DB=1`; CI runs the integration job separately.
3. Tests use deterministic fakes and do not prove the behavior of a live external LLM, embedding provider, Google API, or Mattermost server. Live probes and the manual guide cover those boundaries.
4. Database advisory locks prevent duplicate delivery from concurrent workers during normal operation. A process crash after Mattermost accepts a post but before database settlement can still cause a retry because the Mattermost API path has no external idempotency key.
5. The live scheduling verifier has one stale presentation assertion for the calendar readback wording. The actual bot response contains the correct amended local and UTC time, and the persisted ceremony data is correct.
6. The existing development database volume contains an obsolete Alembic revision from an older branch. Verification used a fresh migrated database instead of resetting existing development data.

## 9. Final Assessment

The three assigned Sprint 4 capabilities have automated unit coverage, real PostgreSQL integration coverage, deterministic external-service fakes, manual copy-paste scenarios, and CI execution paths. The remaining failures are either outside the assigned scope, presentation-only verifier drift, or an external API crash-window limitation that requires an idempotency contract from Mattermost to eliminate completely.