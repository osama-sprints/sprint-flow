# Calendar Integration for Scheduled Ceremonies — Technical Report

## 1. Integration Architecture

Ceremonies already had a `meeting_link.py` provider abstraction with two backends:
`jitsi` (a deterministic URL formula, no external API) and `google_meet` (a real
Google Calendar event with Meet conferencing, via `google_meet.py`). This task
extends the `google_meet` path rather than introducing a new integration layer,
per the existing repo convention of reusing established patterns over adding
parallel infrastructure.

The integration client (`app/services/google_meet.py`) now exposes three
operations, one per lifecycle stage:

- `create_meet_event` — creates a Calendar event with Meet conferencing.
- `update_meet_event` — moves an existing event's time (reschedule).
- `cancel_meet_event` — deletes an existing event (cancellation).

`meeting_link.py` wraps these behind provider-agnostic functions
(`create_meeting_link`, `update_meeting_link`, `cancel_meeting_link`) that the
ceremony scheduling service calls without needing to know which provider (or
none) is active.

**Scope decision:** calendar sync (event id tracking, update, cancel) applies
only to the `google_meet` provider. `jitsi` has no real external event — its
link is a pure function of title and time — so it is left untouched, exactly
as before this task. Confirmed with the mentor before implementation.

## 2. Data Model Extension

Added a nullable `external_event_id` column to `Ceremony` (migration
`d00000000001`, reversible — verified both `upgrade head` and `downgrade -1`
apply cleanly). `meet_link` already existed from a prior task and is reused
as-is. Without a stored event id, reschedule/cancel would have no way to
target the correct external event — this column is what makes lifecycle sync
possible at all.

## 3. Transaction / Failure-Isolation Strategy

Local ceremony persistence and external sync are two separate operations,
never the same transaction:

- **Create:** `commit_schedule` calls `ceremony_repo.create_ceremony` (commits
  to Postgres) *before* calling `create_meeting_link`. A Meet failure at this
  point cannot roll back the ceremony — it already exists.
- **Reschedule/cancel:** `commit_amendment` calls `ceremony_repo.update_ceremony`
  (commits the DB change) *before* calling `update_meeting_link` /
  `cancel_meeting_link`. Same guarantee, same shape as create.

This mirrors the pattern that already existed for ceremony creation prior to
this task — the amendment path simply needed the same discipline applied to
it, since it previously had no calendar sync at all.

Failures on the external side are caught internally by the client functions
(`google_meet.py`) and logged via `logger.exception`/`logger.warning`; they
never propagate up to interrupt the calling service. `commit_amendment`
deliberately does not branch on the boolean return value of
`update_meeting_link`/`cancel_meeting_link` — the local write already
succeeded either way, and the failure is already logged at the source.

## 4. Retry and Backoff Mechanics

Uses `tenacity`, per `AGENTS.md`'s repo-wide retry convention (also used in
`mattermost.py`). Each API call (`_create_with_retry`, `_patch_with_retry`,
`_delete_with_retry`) is wrapped with:

- `stop_after_attempt(3)`
- `wait_exponential(multiplier=1, min=1, max=8)` (≈1s, 2s, 4s between attempts)
- A custom retry predicate, `_is_retryable`

**Retry predicate — a deliberate departure from existing precedent:**
`mattermost.py`'s predicate retries on *any* `HTTPStatusError`, including 4xx,
despite a comment suggesting otherwise. For calendar sync, `_is_retryable`
checks the actual HTTP status code and retries only on `429, 500, 502, 503,
504`, plus genuine network/timeout errors. A `401`/`403`/`400` fails
immediately — retrying an auth or malformed-request error can't succeed and
only delays the (already-logged, already-isolated) failure.

Verified live: a persistent `503` results in exactly 3 attempts, then a clean
`(None, None)` return — not an unhandled exception, not an infinite loop.

## 5. Idempotency Strategy

Idempotency is handled differently per operation, matching what each Calendar
API verb actually guarantees:

- **Create:** a deterministic `requestId` (`sprintflow-{start_iso}`) inside
  `conferenceData.createRequest`. If a create is retried — by our own tenacity
  wrapper, or because a response was lost after Google had already created the
  event — the same `requestId` prevents a second Meet conference from being
  minted for one ceremony. This existed before this task; this task's addition
  is capturing the returned event id so later operations have something to
  target.
- **Reschedule:** a `PATCH` addressed by the stored `external_event_id`. PATCH
  is naturally idempotent — applying the same time change twice (a genuine
  retry, or a duplicate call) converges to the same end state, never a second
  event.
- **Cancel:** a `DELETE` addressed by the stored `external_event_id`, where a
  `404`/`410` response ("already gone") is treated as **success**, not
  failure. This makes a retried or duplicate cancel call safe — deleting an
  already-deleted event is exactly the desired end state.

## 6. Lifecycle Synchronization

| Action | Local (Postgres) | External (Google Calendar) |
|---|---|---|
| Create | `ceremony_repo.create_ceremony` | `create_meet_event` → store `meet_link` + `external_event_id` |
| Reschedule | `ceremony_repo.update_ceremony` (new `scheduled_at`) | `update_meet_event` (PATCH by stored id) |
| Cancel | `ceremony_repo.update_ceremony` (`status=cancelled`) | `cancel_meet_event` (DELETE by stored id) |

All three verified against a live database via the automated verification
script, and separately confirmed manually through the actual Mattermost bot
(real Google Meet link created, reschedule and re-listing reflected correctly).

## 7. Disabled / Unconfigured Mode

Two independent guards, so a ceremony that never had calendar sync enabled
never triggers an API call at any later lifecycle stage:

- `settings.GOOGLE_MEET_ENABLED` gates every function in `google_meet.py` and
  `meeting_link.py` at entry.
- `if updated.external_event_id:` in `commit_amendment` gates the
  reschedule/cancel calls specifically — a ceremony with no stored event id
  (disabled at creation time, or using the `jitsi` provider) skips the block
  entirely, with no exception, no attempted call.

Verified both by the automated script and live: scheduling with the
integration disabled succeeds normally, all external fields stay `null`, and
the mocked/real client is never invoked.

## 8. Failure Contracts

| Scenario | Contract |
|---|---|
| Transient error (429/5xx/timeout) during create | Retried up to 3 times, then ceremony persists with `meet_link`/`external_event_id` = `null`; failure logged |
| Fatal error (4xx other than 429) | Fails immediately, no retry; same null-field persistence |
| Reschedule/cancel sync failure | Local change already committed; failure logged; external event may remain stale until a later action touches the ceremony (see trade-offs below) |
| Integration disabled/unconfigured | No API call attempted at any stage; fields stay null; zero behavioral difference to the user beyond the missing link |

## 9. Security / Credential Handling

No changes needed to credential handling conventions — `GOOGLE_SERVICE_ACCOUNT_CREDENTIALS`,
`GOOGLE_IMPERSONATE_EMAIL`, and `GOOGLE_CALENDAR_ID` were already sourced
strictly from environment variables by the pre-existing `google_meet.py`.
Confirmed `.env` is not staged/committed in this branch's history.

## 10. Verification Coverage

`scripts/verify_calendar_integration.py` (host-side) pipes
`scripts/_calendar_probe.py` into the running `ai-core` container over stdin,
exercising `commit_schedule`/`commit_amendment` against the live database with
the Google API client mocked only at the `build()` boundary — meaning the real
retry, idempotency, and wiring code all execute for real; only the actual
network call to Google is faked.

15 assertions across the 5 required scenarios, all passing:

1. **Create** — persists; `meet_link` and `external_event_id` stored correctly.
2. **Reschedule** — `update_meeting_link` called exactly once, with the
   *original* event id (proving no duplicate), and the new time is reflected.
3. **Cancellation** — `cancel_meeting_link` called exactly once with the
   original event id; ceremony status becomes `cancelled`.
4. **Provider outage** — a persistent simulated `503` results in exactly 3
   attempts, then the ceremony persists regardless, with null external fields.
5. **Disabled mode** — scheduling succeeds, zero external calls made, fields
   stay null.

Additionally validated manually end-to-end through the live Mattermost bot: a
real ceremony was created with a genuine Google Meet link attached, listed
correctly via `render_calendar`, and successfully rescheded through the
conversational interface.

Full regression suite re-run after all changes: no new failures introduced;
the only failures present (`test_complete_policy_tasks.py`,
`test_supervisor.py`, both related to intent-routing, plus a missing `trio`
dependency) are pre-existing and unrelated to this task, and have been
reported to the team separately.

## 11. Design Trade-offs

- **Inline bounded retry over a durable outbox/dispatcher.** The repo already
  has a heavier pattern for this class of problem (`onboarding_steps` +
  `onboarding_dispatcher`: a persisted, leased, backoff-retried queue). That
  pattern was deliberately not reused here. Rationale: a failed calendar sync
  leaves the ceremony itself fully correct in Postgres — only the *mirrored*
  external link/event is stale or missing. That is a materially softer
  failure than onboarding's "message never sent, no fallback," which is why
  onboarding justifies the added complexity of durable, cross-restart retry
  and this task does not. This was discussed with and confirmed by the
  mentor before implementation.

  **Accepted limitation:** if the Calendar API is unavailable for longer than
  the ~7 seconds three bounded retries can absorb, the external event stays
  stale or missing until *some other action* touches that ceremony again —
  there is no automatic background recovery. This is a conscious trade-off,
  not an oversight, made in favor of matching the repo's existing retry
  idiom and staying within scope for the sprint timeline.

- **Retry predicate narrower than existing precedent.** `mattermost.py`
  retries on any HTTP status error; this integration only retries on
  429/5xx/timeouts. This is an intentional improvement for this integration,
  not a criticism of the existing code — simply a stricter interpretation of
  "bounded retry semantics for transient errors... fatal client/authentication
  errors should fail cleanly" from the task brief.

- **Google Meet provider only.** `jitsi` remains untouched. If the team later
  wants full lifecycle tracking for `jitsi` too, note that a rescheduled
  ceremony's `jitsi` link is *not* currently regenerated on reschedule (it is
  deterministically derived from title + start time, so a changed start time
  silently produces a stale link) — this predates this task and was out of
  scope per the confirmed scoping decision, but is worth flagging for
  awareness.
