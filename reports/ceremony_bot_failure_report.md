# Ceremony Bot Failure Report

**Date investigated:** 2026-09-02  
**Environment:** Local Docker Compose, development  
**Affected channel:** Mattermost direct message with `sprintflow-assistant`  
**Status:** Root cause identified; shared bot fixes still required

## Summary

After a ceremony scheduling request failed, every later message in the same
direct-message conversation—including `hello`—received:

> Sorry — I hit an error while working on that. Please try again in a moment.

The repeated failure is a bot conversation-state problem triggered by the
original ceremony database error. The saved LangGraph session remains at a
failed `tool_call`, and the bot incorrectly treats that state as a pending
human-interaction interrupt. Each new message therefore retries the old
ceremony operation instead of starting a new chat turn.

## User Impact

- The user cannot continue chatting in the affected DM.
- Unrelated messages retry an old ceremony request.
- A new requested time can be ignored in favor of details from the old request.
- The generic fallback hides the actual problem and gives the user no recovery
  instructions.

## Reproduction

1. Start a ceremony scheduling conversation.
2. Confirm a ceremony for a cohort ID that does not exist.
3. The ceremony insert fails with a foreign-key violation.
4. Send an unrelated message such as `hello` in the same DM.
5. The bot resumes the failed `tool_call`, retries the ceremony insert, and
   returns the generic error again.

## Evidence

At approximately 3:02 PM local time, the bot received a five-character DM:
`hello`. The logs then showed:

```text
mattermost_ws_message_received text_length=5
resuming_interrupted_graph next_nodes=('tool_call',)
ForeignKeyViolation: Key (cohort_id)=(202) is not present in table "cohort"
```

The failed operation still contained the older ceremony details:

```text
cohort_id: 202
scheduled_at: 2026-09-03 16:00:00 UTC
raw_input: September 3, 2026, 4:00 PM UTC
```

The local `cohort` table contained zero records. The `ai-core` server process
also started before the ceremony validation fix and runs Uvicorn without
`--reload`, so it continued using the old in-memory function.

## Root Causes

### 1. Failed graph state is mistaken for a human interrupt

In `ai-core/app/core/langgraph/graph.py`, `get_response()` checks only
`state.next`. Any pending node is treated as an interrupted graph and resumed
with `Command(resume=...)`.

However, `state.next` can also point to a node left unfinished by an exception.
In this incident it pointed to `tool_call`, while there was no valid user
confirmation waiting to be resumed.

### 2. Tool exceptions leave the conversation stuck

The ceremony insert raised an uncaught SQLAlchemy `IntegrityError`. The graph
checkpoint remained positioned before the failed tool node. Later turns loaded
that checkpoint and executed the same operation again.

### 3. Deterministic database errors are retried

The graph applies a general `RetryPolicy(max_attempts=3)` to the tool node. A
foreign-key violation cannot succeed without different data, but it was retried
three times on every message.

### 4. The running server does not reload source changes

The application directory is bind-mounted, but Uvicorn starts without
`--reload`. Updating the source file does not update tool objects already
imported by the running process. A controlled `ai-core` restart is required
after code changes.

### 5. Original ceremony request referenced a missing cohort

The first failure occurred because cohort `202` did not exist. The ceremony
tool previously allowed the database foreign key to detect this, resulting in
an exception rather than a useful tool response.

The ceremony branch now validates the cohort first and returns a clear error.
This prevents new missing-cohort requests from producing the original database
exception once the updated service is restarted.

## Required Fixes

### Shared bot/graph team — high priority

1. **Distinguish real interrupts from failed pending nodes.**
   Resume with `Command(resume=...)` only when the saved task contains an actual
   LangGraph interrupt. Do not use `state.next` alone as proof of an interrupt.

2. **Recover safely from failed tool nodes.**
   Convert expected tool failures into `ToolMessage` results, or move the graph
   into a recoverable state after an exception. A later user message must not
   silently retry the failed operation.

3. **Restrict retries to transient failures.**
   Do not retry validation errors, authorization failures, foreign-key
   violations, unique violations, or other deterministic integrity errors.

4. **Handle fresh commands during confirmation.**
   A new scheduling command must not automatically confirm an older request.
   Require an explicit confirmation response, or allow the new command to
   cancel/supersede the pending action.

5. **Provide a supported session-recovery mechanism.**
   Add an administrator-only command or endpoint that can clear a broken
   conversation checkpoint without direct database manipulation.

### Runtime/deployment team — high priority

1. Restart or recreate `ai-core` after deploying application changes.
2. For local development, consider enabling Uvicorn reload intentionally, or
   document that `make restart-ai` is required after source edits.
3. Expose the deployed commit/version in health or startup logs so stale code
   can be identified quickly.

### Ceremony scheduler team — completed/current scope

1. Validate that the requested cohort exists before creating ceremony lookup
   data or inserting the ceremony.
2. Return an actionable missing-cohort message without changing the database.
3. Keep a regression test for missing cohorts.
4. Do not automatically create cohorts from the scheduling tool; cohort
   creation and permissions belong to the administration/data domain.

### Environment/data setup team

1. Create or seed at least one valid cohort for end-to-end scheduling tests.
2. Make test users and their cohort permissions explicit in bootstrap fixtures.
3. Publish the valid test cohort IDs so developers do not rely on stale memory
   or invented identifiers.

## Acceptance Criteria

- Scheduling against a missing cohort returns a clear validation response and
  performs no insert.
- After any tool failure, sending `hello` produces a normal assistant response.
- A failed operation is not retried on unrelated later messages.
- A fresh scheduling command does not confirm an older pending request.
- With a valid cohort, `tomorrow at 6 PM UTC` is stored as 6 PM UTC—not a time
  from an earlier request.
- Deterministic integrity errors are attempted once, not three times.
- The running `ai-core` instance reports or demonstrably loads the deployed
  ceremony fix.

## Ownership Boundary

The missing-cohort validation and its tests belong to the ceremony scheduler
work. Interrupt detection, checkpoint recovery, generic fallback behavior, and
process reload behavior are shared bot/runtime concerns and should be fixed by
their owning teams rather than changed on the ceremony-only branch.
