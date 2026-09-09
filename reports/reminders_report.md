# Proactive Ceremony Reminders Technical Write-up

## Overview

SprintFlow includes a proactive reminder system that sends direct messages to participants ahead of scheduled ceremonies. This document details the architectural decisions, worker implementation, and idempotency guarantees that ensure reliable delivery without duplicates.

## Architecture

The proactive reminder subsystem runs as an independent background loop (a `reminder_poller` asyncio task) within the main `ai-core` process. It polls the database every 5 minutes, checks for ceremonies nearing their scheduled time (24-hour and 1-hour windows), and dispatches Mattermost direct messages.

Unlike heavier solutions such as Celery or external cron jobs, this lightweight architecture avoids introducing new infrastructure dependencies while maintaining robust reliability.

## Durable Dispatch and Idempotency

To prevent duplicate notifications—especially in the event of pod restarts, concurrent process executions, or transient network failures—we use a PostgreSQL-backed idempotency guard.

### The `CeremonyReminder` Table

The `CeremonyReminder` table serves as the single source of truth for sent reminders. It records:
- `ceremony_id`: The ID of the ceremony.
- `recipient_mm_id`: The Mattermost user ID of the participant.
- `window`: The reminder window (e.g., `"24h"` or `"1h"`).
- `sent_at`: Timestamp of successful dispatch.

### Duplicate Prevention

A unique database constraint spans `(ceremony_id, recipient_mm_id, window)`.
Before dispatching a DM, the worker queries this table to see if a row already exists. If not, it attempts the dispatch and subsequently inserts the row. If two worker loops race to send the same reminder:
1. One succeeds in inserting the row.
2. The other encounters an `IntegrityError` (Unique Constraint Violation).
3. The worker gracefully catches this error and treats it as a successful bypass, ensuring only one DM is sent to the participant.

## Handling Edge Cases

### Cancelled Ceremonies
The reminder worker only fetches ceremonies that are actively scheduled. The underlying repository query (`list_upcoming_ceremonies`) inherently filters out ceremonies where `status != "scheduled"`. Thus, if a ceremony is cancelled before a 24-hour or 1-hour window, no reminder will be triggered.

### Inactive Cohorts (Channels)
With the migration from abstract cohorts to Mattermost channels, the system retrieves the target audience directly via channel memberships. When fetching recipients, the worker calls `list_channel_roles(channel_id, active_only=True)`. This ensures that:
- Users who have left the channel (or were removed) do not receive reminders.
- If a channel is archived or rendered inactive, there are no active members resolved, naturally halting any reminder dispatches for its ceremonies.

## Conclusion

This design achieves at-least-once evaluation and at-most-once delivery properties for ceremony reminders by relying on ACID database semantics rather than distributed locks. The architecture is resilient to crashes, requires no external task brokers, and scales effortlessly alongside the application servers.
