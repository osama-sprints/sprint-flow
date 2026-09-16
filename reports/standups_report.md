# Proactive Daily Standups — Collection & Parsing

## What this covers

A daily *prompt gets sent* half and a *reply gets collected* half. Every
school morning, each active learner in an active sprint is DM'd a three-part
question (did / will do / blockers) and their reply is stored **verbatim** —
raw response kept losslessly — with a deterministic structural parse layered on
top. First answer of the day wins; a silent day is a fact about the prompt, not
a fabricated entry.

Entry points:

- `app/services/standups.py::deliver_prompt` —
  the typed delivery of one claimed prompt, re-checking the learner is still
  active right before sending (never prompts someone who was removed).
- `app/services/standups.py::ingest_standup_reply` —
  the typed intake of one incoming DM, from `app/services/mattermost_ws.py`'s
  receive hook.
- `app/workers/standup_dispatcher.py::StandupDispatcher` —
  the single background task owned by `app/main.py`'s lifespan, which ensures
  today's prompts, closes silent days, and delivers due ones.
- `app/services/standups.py::parse_standup_reply` —
  the parser behind `DailyStandup.what_i_did/will_do/blockers`.

## Design decisions

**Prompt the person's *local* day, at-most-once.** A prompt row is created
(`ensure_prompt`) *before* any DM is sent, with a database unique constraint on
`(sprint, learner, local day)` plus `ON CONFLICT DO NOTHING`. Two passes or two
containers can never prompt the same person twice for the same day, however
they race. The row's `dispatch_at` is derived from the learner's stored
timezone (`dispatch_at_for`), defaulting to UTC when the profile is garbage, so
someone whose zone was never updated still works.

**Exactly-once delivery is a row, not a send call.** `claim_due_prompts` is
`FOR UPDATE SKIP LOCKED` with a lease: a batch worker stamps `claimed_by` +
`claimed_at`, and only the claimant can mark `sent` (the `claimed_by == worker`
guard). A delivery that fails never marks sent — the lease expires and the row
becomes claimable again, with a geometric backoff capped by
`STANDUP_MAX_ATTEMPTS` (3). After the cap the row is `failed`, honestly. The
running app and any verification harness therefore partition work instead of
duplicating DMs.

**Reply attribution is thread-first.** A reply whose `root_id` is a dispatched
prompt's post is unambiguously that prompt — whether it is still open
(`duplicate`), or was already closed missed (`late`). Anything else in a DM
falls back to the newest *still-dispatched* prompt for that author in that
channel. Replies are collected in DMs only (`channel_type == "D"`), never in
channels, and a message that names `@<bot>` or is a bare acknowledgement
(`ok`, `thanks!`, `got it`, …) is NOT ours — it is handed back to the normal
chat pipeline untouched. The redelivery guard is a unique constraint on
`standup_replies.post_id`, so a replayed WebSocket event is a no-op.

**Accepted vs duplicate vs late is decided by the *date*, then the *status*.**
A reply arriving on the prompt's own local day, when the prompt is dispatched,
is accepted. Same day but already answered → duplicate (raw preserved, entry
never overwritten). Arriving on a different day — or the prompt was already
closed missed — → late, raw-recorded only. None of these ever rewrites or
opens the prompt again: late does not fabricate an entry, duplicate does not
touch the stored one.

**Deterministic parsing falls back honestly.** `parse_standup_reply` tries
numbered 1/2(/3) sections, then labeled `done:`/`plan:`/`blocked:` blocks
(including multi-line blocks and same-line content after the label), then flat
fallback with the whole reply as progress. Whatever structure it finds, the
**raw reply is always stored** (`DailyStandup.raw_response`), so nothing a
learner said is ever lost to a parse edge case.

## Added

- Models: `DailyStandupPrompt` and `StandupReply`
  (`app/models/daily_standup_prompt.py`, `app/models/standup_reply.py`),
  with `StandupPromptStatus` and `StandupReplyOutcome` in
  `app/models/enums.py`. `DailyStandup` grew `prompt_id` (unique FK →
  provenance), `raw_response`, `submitted_at`, `timezone`; legacy manual-entry
  rows keep a NULL `prompt_id` (safe).
- Domain DAL (`app/services/domain/standups.py`): `ensure_prompt`,
  `get_prompt_for`, `get_prompt`, `get_prompt_by_post_id`,
  `claim_due_prompts` (lease + SKIP LOCKED), `mark_prompt_sent/failed/no/data`,
  `release_prompt_claim`, `mark_prompt_missed`, `list_outstanding_prompts`,
  `mark_prompt_answered`, `create_standup_entry`, `record_reply`,
  `count_prompts`, `remove_prompts_between` (verification hygiene),
  `list_daily_standups`; plus `list_active_sprints` in
  `app/services/domain/sprints.py`. **Cohort-level read for summarisation:**
  `list_standups_for_channel` returns every entry written in a cohort's channel
  (a cohort's home is the sprint channel in this branch — see the
  `refactor_cohort_to_channel` migration) within a `[start_date, end_date]`
  range, joining `daily_standups → sprints`, ordered by day then learner, with
  an optional `learner_ids` narrowing — no caller writes SQL.
- Orchestration (`app/services/standups.py`): prompt rendering, the timezone
  math (`resolve_timezone`, `local_date_of`, `dispatch_at_for`, `day_end_utc`),
  `deliver_prompt`, `ensure_today_prompts` / `ensure_scope` (with
  `user_ids` restriction for verification so a probe can never register real
  people), `close_missed_prompts`, reply parsing/ack/mention filtering,
  `ingest_standup_reply`, confirmations.
- Worker (`app/workers/standup_dispatcher.py`), wired into `app/main.py`
  lifespan start/stop and the `/health` dispatcher component, gated by
  `settings.STANDUP_ENABLED` (settings in `app/core/config.py`).
- Ingest hook in `app/services/mattermost_ws.py`: DM replies are drained by the
  standup handler before the generic pipeline when the message is ours.
- Metric `sprintflow_standup_prompts_total` labeled `["outcome"]`
  (`app/core/metrics.py`).
- Migration `alembic/versions/0005_daily_standup_prompts.py` (revision
  `0005_standup_prompts`) adding the two tables and the `daily_standups`
  columns, with named PK/FK/unique constraints and indexes, matching repo
  migration style. Applied; `alembic check` reports no new drift (the pre-existing
  `policy_document_chunks` mismatch is another task's).

## Idempotency

- **Prompt per day:** unique `(sprint, learner, local_date)` + `ON CONFLICT DO
  NOTHING`; `ensure_scope` may run every poll pass.
- **Delivery:** SKIP-LOCKED lease; only the lease holder marks `sent`; a failed
  send stays pending and is retried—never double-DM'd by a concurrent worker.
- **Reply:** `uq_standup_replies_post_id` is the DB race guard — the exact same
  WebSocket event redelivered returns `ALREADY_RECORDED` and re-sends nothing;
  `record_reply` on the conflict re-reads the winner.
- **Entry:** `uq_daily_standups_sprint_learner_day` (pre-existing) means a
  racing first answer simply downgrades the loser to `duplicate`.

## Fallback paths

- **Learner removed / role inactive between ensure and deliver.** The deliver
  step re-checks the active `LEARNER` `ChannelRole` immediately before sending
  and marks the prompt `missed` instead of DM-ing someone who shouldn't be
  prompted (logged, counted as `skipped`).
- **Mattermost unreachable.** Send failures return `RETRY` with backoff;
  after `STANDUP_MAX_ATTEMPTS` the prompt is `failed` — a `missed` fact, never
  a fabricated entry — and the next day is unaffected. The worker never crashes
  the process; every exception is bounded per-item and per-pass.
- **Unknown/unsynced author.** A DM from someone ai-core never synced is not a
  standup (logged, returned `NOT_A_STANDUP`).
- **Silent day.** `close_missed_prompts` marks the prompt `missed` — the 
  *only* change is prompt state. No entry is fabricated, which is why a daily
  report built from `daily_standups` never invents participation.
- **Timezones never validated at signup.** Any stored zone is used; unknown
  values fall back to UTC, so dispatch/local-day logic cannot crash on a new
  learner.

## Verification

- Pure tests: `ai-core/tests/test_standups.py` — timezone math (incl. DST),
  parsing (numbered/labeled/flat), ack + mention filtering, rendering, backoff.
  28 tests.
- DB integration tests: `ai-core/tests/integration/test_standups_db.py`, run
  against a throwaway migrated Postgres with `SPRINTFLOW_INTEGRATION_DB=1`
  (SkipUnless), driven by the same seeded-fake-Mattermost pattern the
  onboarding tests use. 11 tests covering prompt idempotency, the lease
  lifecycle, delivered/retried dispatch, no-fabrication missed days, the
  full ingest classification (accepted/duplicate/late/redelivery/not-a-standup),
  and the cohort-level `list_standups_for_channel` read (date range, cross-
  cohort isolation, learner narrowing).
  (The pre-existing onboarding/back-office DB tests are stale — they exercise
  the removed `channels`/`channel_memberships` schema — so this suite is the
  current-schema reference; the whole non-integration suite, 431 tests, stays
  green.)
- Repeatable end-to-end verifier: `scripts/verify_standups.py` pipes
  `scripts/_standups_probe.py` into the running ai-core container against the
  live database with a faked Mattermost. It proves, with 40 PASS/FAIL lines and
  a non-zero exit:

  1. idempotent prompt creation;
  2. one and only one prompt DM to the right channel asking all three questions;
  3. accepted reply → exact entry + raw verbatim + prompt closes `answered`;
  4. duplicate preserved, confirmation sent, redelivery no-op;
  5. missed day closes with no entry; late reply still captured, stays closed;
  6. the real `StandupDispatcher.run_once` restricted to the probe's own learner
     registers, claims and delivers in one typed pass;
  7. the active-cohort filter and recipient timezone scheduling: a learner in a
     `completed` cohort or with an inactive channel membership is never prompted,
     while active learners in two timezones each get a prompt whose `dispatch_at`
     is their 09:00 local converted to UTC (06:00 UTC for Riyadh, 16:00 UTC for
     Los Angeles) — not the server's wall clock — and a not-yet-due west-coast
     prompt becomes claimable only once its local hour arrives, then is
     delivered end-to-end by the same dispatcher;
  8. probe rows always cleaned up.

Run it with the stack up:

```bash
python3 scripts/verify_standups.py
```