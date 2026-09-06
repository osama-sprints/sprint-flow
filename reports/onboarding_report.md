# Sprint 1 / AI Eng 5 — Proactive Role-Aware Onboarding

When a person registers in the Mattermost workspace, the assistant welcomes them by DM,
orients them for the role they hold (or tells them the orientation follows once a lead
sets it), and checks in again a configurable number of hours later. Every message is a
row in a durable outbox (`onboarding_steps`) that a background dispatcher delivers; the
event handler itself sends nothing. This report explains each design decision, states
which guarantee each mechanism gives, separates what was observed from what was reasoned
about, and gives the commands to reproduce every claim.

Files:

| Concern | File |
| --- | --- |
| Arrival event handling (foundation, unchanged) | `ai-core/app/services/mattermost_ws.py` — `_handle_new_user` |
| Journey policy, content, delivery | `ai-core/app/services/onboarding.py` |
| Dispatch loop | `ai-core/app/workers/onboarding_dispatcher.py` |
| Outbox persistence (foundation) | `ai-core/app/models/onboarding_step.py`, `ai-core/app/services/domain/onboarding.py` |
| Content, one file per (step kind × role) | `ai-core/app/core/prompts/onboarding/*.md` (15 files) |
| Unit tests (pure logic) | `ai-core/tests/test_onboarding.py` |
| Database-backed tests (fake Mattermost) | `ai-core/tests/integration/test_onboarding_db.py` |
| In-container probe | `scripts/_onboarding_probe.py` |
| Host verifier (counts real DMs, includes the replay case) | `scripts/verify_onboarding_journey.py` |

## Arrival detection

**Signal.** ai-core keeps a WebSocket session to Mattermost as the bot. When an account is
created, Mattermost broadcasts a `new_user` event with an empty broadcast scope, so every
authenticated connection — including the bot's — receives it. That is the signal used.
`added_to_team` is deliberately *not* used: Mattermost sends it only to the joining user's
own connections, so a handler built on it works when the developer joins themselves and
never fires for anyone else (this is the trap `tasks/general/02-codebase-conventions.md`
§5 documents, and the "arrival signal that only reaches the person who joined" mistake in
the task brief).

**What the handler does — and does not do.** `_handle_new_user` (foundation, unchanged)
adds the account to the default team, then calls `identity.sync_mattermost_user(user_id)`
— an `INSERT … ON CONFLICT (mattermost_user_id) DO UPDATE` on `users`, so the person has a
stable internal id and an up-to-date profile before any onboarding decision — and then
`onboarding.start_journey(user)`. `start_journey` writes two outbox rows and sets an
`asyncio.Event`. It never opens a DM. A slow or down Mattermost therefore cannot stall
the WebSocket loop, and a failure inside the handler is caught and logged
(`onboarding_arrival_failed`) rather than taking the listener down.

**Replayed events.** The WebSocket reconnects after drops and Mattermost may resend
events; the bot also sees its own account's events, which the handler filters out by id.
A replayed `new_user` runs exactly the same `start_journey` and finds both rows already
there (see *Exactly once*); it logs `onboarding_arrival_replayed` and returns `False`.

**Bot's own DMs.** The outgoing welcome is a REST post by the bot account. Inbound posts
are filtered twice before any processing: `props.from_bot == "true"` and
`is_own_post(user_id)` (foundation). So the assistant never answers its own onboarding
DMs; the verifier watches the DM for 20 s after the last delivery to assert that
silence.

## Role tailoring

**Role resolution at delivery time, not at arrival.** A person normally arrives before a
lead has assigned them a role, so any role captured at arrival would be wrong. Instead,
`deliver_step` calls `resolve_onboarding_context(user_id, step.cohort_id)` when the row
is actually claimed:

- For a cohort-scoped step (an `orientation`), the role is the person's ACTIVE
  membership in that cohort, provided the cohort is active.
- For a workspace-level step (`welcome`, `follow_up`), the role is the most recently
  joined ACTIVE membership in an ACTIVE cohort (`pick_primary_membership`, pure, unit
  tested). Inactive cohorts never contribute a role.
- The cohort's leads (`tech_lead`, `scrum_master` members, excluding the person) are
  resolved so the message can name real people.

The role that was actually used is written to `onboarding_steps.role_key_at_delivery`,
so an operator can see what each person received.

**Materially different content.** There are fifteen templates, one per (step kind ×
role) including a `no_role` variant for each kind, rendered with `str.format`
(`{first_name}`, `{cohort_name}`, `{role_label}`, `{bot_handle}`, `{lead_handles}`) in
the assistant's established voice (`system.md`: warm, concise, senior colleague,
Markdown). They differ in what they tell the person to *do*, which would be wrong for
another role:

| Role | The welcome / orientation explains |
| --- | --- |
| Learner | ask the assistant first (DM or mention); escalation is invisible — the answer comes back in their own thread; the daily what/next/blocked standup; `what's scheduled this week?` for the cohort calendar |
| Tech lead | technical escalations arrive as DMs with a ticket ref; a one-line DM reply closes the ticket and is rewritten for the learner; `open sprint …`, `assign @… as … in …`, `schedule … for …` with time confirmation; morning blocker summary |
| Ops support | policy/operational escalations arrive as DMs; a short reply resolves them; back-office views (`who is in …?`, `list cohorts`, calendar); sprints and ceremonies belong to the leads |
| Scrum master | scheduling ceremonies by chat with the assistant restating the exact instant and saving only after confirmation, overlap refusal; opening sprints; the calendar; blocker summary |
| No role yet | warm welcome, how to ask questions, and "I'll send your orientation as soon as your cohort lead sets your role" |

`tests/test_onboarding.py` enforces this structurally: every one of the 15 variants
renders non-empty with no unrendered placeholder; for each kind, no two role variants
share more than 40 % of their lines (measured: the largest overlap is the shared
sign-off line); each variant contains its role-specific phrases (e.g. a tech lead's text
must contain `open sprint`, `assign`, `escalation`, `reply`) and a learner's text must
*not* contain `open sprint` or `assign @`.

**Arrived before the role exists.** The welcome goes out with the `no_role` content and
`role_key_at_delivery = NULL`. When the back-office `assign_role` tool later calls
`onboarding.on_role_assigned(user_id, cohort_id)`, an `orientation` row for that cohort is
enqueued (due now) and delivered with the role's orientation content. If the welcome is
still pending when the role arrives, nothing extra is enqueued: the welcome itself
renders the role content and, on success, records that cohort's `orientation` row as
sent with the same post id, so the orientation is never sent twice. If the person was
never welcomed by the bot (an account that predates it) the orientation is sent anyway
— they hold a role now and the orientation is what explains it.

## Journey design

Three step kinds, each an outbox row with a unique key `(user_id, cohort_id, step_kind)`
(`NULLS NOT DISTINCT`, so the NULL cohort of workspace-level steps is one value):

| Step | Cohort | Due | Enqueued by |
| --- | --- | --- | --- |
| `welcome` | NULL | arrival | `start_journey` |
| `orientation` | the cohort | role assignment (or recorded as carried by the welcome) | `on_role_assigned` / `deliver_step` |
| `follow_up` | NULL | arrival + `ONBOARDING_FOLLOW_UP_DELAY_HOURS` (default 72 h) | `start_journey` |

The number of journey steps was deliberately kept small; the effort went into the
properties (exactly once, durability, deactivation) rather than more messages. The
follow-up is also role-aware: it is rendered with whatever role the person holds when it
becomes due, so a person welcomed without a role but assigned one on day 2 receives a
role-specific check-in on day 3.

Row lifecycle: `pending` → `sent` (with `sent_at`, `mattermost_post_id`,
`role_key_at_delivery`) or, after `ONBOARDING_MAX_ATTEMPTS` failures, `failed` (with
`last_error`). `halted` exists for the archiving task (see *Deactivation*). Every
instant column is `timestamptz` (`due_at`, `sent_at`, `next_attempt_at`, `claimed_at`,
audit columns); the migration probe asserts `onboarding_steps.due_at` is timezone-aware.

## Scheduling approach

**Where due work is found.** In the database, by polling: `claim_due_steps` selects
`status = 'pending' AND due_at <= now AND (next_attempt_at IS NULL OR next_attempt_at <= now)
AND (claimed_at IS NULL OR claimed_at < now - lease)` with `FOR UPDATE SKIP LOCKED`, stamps
`claimed_at`/`claimed_by` in the same transaction, and returns the batch. Nothing about
"when" lives in process memory — no timers, no in-memory queue.

**The dispatcher.** `OnboardingDispatcher` is one asyncio task per ai-core process,
started from `lifespan` (foundation wiring) and reported in `/health` under
`onboarding_dispatcher` (`running`, `worker_id`, `runs`, `last_run_at`, `last_summary`,
`schema_missing`). The loop is `run_once()` then wait for either `wake()` (an
`asyncio.Event` set by `start_journey`/`on_role_assigned` through a registered callback,
so a welcome goes out within milliseconds of arrival) or
`ONBOARDING_POLL_INTERVAL_SECONDS` (default 30 s, the path a three-day follow-up takes).
A pass that fills its batch (50 rows) loops immediately. Any exception in a pass is
logged with `logger.exception` and the loop continues; if the outbox table does not
exist yet (a container started before its migration ran) the pass logs
`onboarding_schema_missing`, flags it in `/health`, and keeps polling.

**Delivery of one row** (`deliver_step`): resolve the role context → check the kill
switch (`is_halted`, pure) → render → `create_direct_channel` + `create_post` over REST →
`mark_step_sent`; on any error `mark_step_failed` with `next_attempt_at = now +
min(ONBOARDING_RETRY_BACKOFF_SECONDS × 2^attempt, 3600)`. Every data-access call opens
and closes its own `AsyncSession`; **no session is open during the network call**
(review finding 7). Prometheus `sprintflow_onboarding_steps_total{step_kind, outcome}`
counts `sent | retry | failed | halted | skipped`.

**Why in-process rather than a separate worker.** The repository runs one uvicorn worker
per container and already hosts the WebSocket listener in-process; a second service
would add a deployment unit for a loop that does one indexed query every 30 s. The
design does not *depend* on being single: see *Exactly once* for what happens with two.

**Configuration** (all in `ai-core/app/core/config.py`, mirrored in `.env.example`):

| Variable | Default | Meaning |
| --- | --- | --- |
| `ONBOARDING_ENABLED` | `true` | Master switch; `start_journey`/`on_role_assigned` become no-ops and the dispatcher does not start |
| `ONBOARDING_FOLLOW_UP_DELAY_HOURS` | `72` | Welcome → follow-up delay |
| `ONBOARDING_POLL_INTERVAL_SECONDS` | `30` | Poll cadence (also woken on arrival) |
| `ONBOARDING_MAX_ATTEMPTS` | `5` | Attempts before a step is marked `failed` |
| `ONBOARDING_RETRY_BACKOFF_SECONDS` | `60` | Backoff base: 60, 120, 240, 480, 960 … capped at 3600 |
| `ONBOARDING_CLAIM_LEASE_SECONDS` | `600` | How long a claim excludes other workers before it is treated as abandoned. Sized above the worst-case delivery (two REST calls, each retried three times with backoff over `MATTERMOST_HTTP_TIMEOUT`, ≈ 200 s at the 30 s default) so a slow but alive worker is never overtaken |

## Exactly once

"Greeted exactly once" is enforced by three layers, each structural, none a check-then-act:

1. **One row per (person, cohort, kind) — the unique key.** `enqueue_step` is a single
   `INSERT … ON CONFLICT (user_id, cohort_id, step_kind) DO NOTHING`. Two concurrent
   `new_user` deliveries, a reconnect replay, or two processes racing cannot create a
   second welcome row; the database refuses it. `start_journey` returns `True` only for
   the call whose insert actually happened.
2. **One worker per row — the claim lease, and settlement only by its holder.** `FOR UPDATE SKIP LOCKED` means two
   dispatchers running the same query at the same instant receive disjoint rows; the
   lease (`claimed_at`, `claimed_by`) written in that transaction keeps the row out of
   later queries until it is settled or the lease expires. `mark_step_sent` and
   `mark_step_failed` take the claimant's id and refuse to settle a row whose claim has
   since passed to another worker (the late worker logs `onboarding_claim_lost_after_send`
   instead of overwriting the outcome).
3. **Mark after send.** The row becomes `sent` only after Mattermost returned a post id
   (review finding 1). A failed send leaves it `pending` with `attempt_count`,
   `next_attempt_at` and `last_error`, so the *next* attempt is a real retry, not a
   no-op. A restart between enqueue and send changes nothing: the row is still pending
   and is claimed by the next pass.

**More than one dispatcher.** Two ai-core containers (or `--workers 2`) each run a
dispatcher with a distinct `worker_id` (`hostname:pid`). Layer 2 partitions the rows
between them. This was *tested*, not only reasoned about: two dispatchers with batch size
3 run `run_once` concurrently over 20 due steps until both find nothing; the assertion is
20 posts, every row `sent` once with a distinct post id, and both workers having claimed
rows (`tests/integration/test_onboarding_db.py::test_two_dispatchers_deliver_twenty_steps_exactly_once`,
and probe check "concurrency: 20 due steps -> exactly 20 posts"; last observed split 11/9).

**The honest boundary.** Mark-after-send makes delivery *at-least-once* in exactly one
window: if the process dies (or the database becomes unreachable) after Mattermost
created the post but before `mark_step_sent` commits, the lease expires after
`ONBOARDING_CLAIM_LEASE_SECONDS` and another pass sends the same step again. That window
is a few milliseconds per delivery; removing it would require Mattermost to accept an
idempotency key on post creation, which it does not. Replayed events, restarts at any
other point, and concurrent dispatchers do not produce a duplicate: the unique key stops a
second row and the claim lease stops a second delivery of the same row.

One narrow case shares that same at-least-once shape rather than being excluded by it. A
role assigned while a person's welcome is already claimed queues that cohort's orientation
as its own row (deferring instead would lose it, since the in-flight welcome resolved the
role before the assignment landed). If the welcome then does carry the orientation, that row
may already be with another worker, so `_record_carried_orientation` settles it only when no
live claim exists (`mark_step_sent(require_unclaimed=True, lease_seconds=…)`) and otherwise
logs `onboarding_orientation_left_to_its_claimant`, leaving the row to the worker delivering
it. In that window the person can receive the orientation twice — once folded into the
welcome, once on its own. Closing it would need a lock spanning both rows, which is not worth
it for a duplicated greeting; losing the orientation entirely would be worse.

## Durability

Every pending step is a committed row before anything else happens. The follow-up due in
72 hours is `onboarding_steps` row with `status='pending'` and a `due_at`; the dispatcher
re-discovers it by query after any number of restarts of ai-core, of PostgreSQL, or of
the whole compose stack, because the poll condition is a predicate on stored columns
rather than a timer. Migrations run at container start (`AI_CORE_MIGRATE_ON_START`,
foundation), and `/health` returns 503 while the domain schema is missing, so a container
cannot look healthy with no outbox (review finding 11).

This is demonstrated, not assumed, in two ways: `test_follow_up_due_in_the_past_is_sent`
enqueues a follow-up already due (as it would be after a long outage) and a fresh
dispatcher instance delivers it; and the host verifier moves a real user's follow-up to
`now()`, runs `docker compose restart ai-core`, and asserts the DM then holds exactly two
bot posts (welcome + follow-up).

**Operating it.**

- Pending work: `SELECT id, user_id, cohort_id, step_kind, due_at, attempt_count,
  next_attempt_at, claimed_by FROM onboarding_steps WHERE status='pending' ORDER BY due_at;`
- Failed steps (gave up after `ONBOARDING_MAX_ATTEMPTS`): `SELECT id, user_id, step_kind,
  attempt_count, last_error FROM onboarding_steps WHERE status='failed';`
- Re-queue after fixing the cause: `UPDATE onboarding_steps SET status='pending',
  next_attempt_at=NULL WHERE id=…;` — the next pass picks it up (attempt counting starts
  from the stored count; set `attempt_count=0` too for a full fresh budget).
- Dispatcher state: `GET /health` → `onboarding_dispatcher`; logs `onboarding_dispatch_pass`,
  `onboarding_step_sent`, `onboarding_step_delivery_failed`, `onboarding_step_halted_inactive_cohort`,
  `onboarding_schema_missing`; metric `sprintflow_onboarding_steps_total`.
- If the dispatcher is down (ai-core stopped): nothing is lost and nothing is sent; rows
  accumulate as `pending` and are delivered, oldest `due_at` first, within one poll
  interval of the next start. A row whose worker died mid-delivery stays leased for
  `ONBOARDING_CLAIM_LEASE_SECONDS`, then becomes claimable again.

## Deactivation

`cohorts.is_active` is the kill switch. It is evaluated **at delivery time** by
`is_halted(step, context)` (pure, unit tested), so archiving a cohort later needs no
onboarding code change:

- A cohort-scoped step whose cohort is inactive → halted (`cohort_inactive`).
- A workspace-level step for a person who has one or more memberships but *every* one
  of their cohorts is inactive → halted (`all_cohorts_inactive`). A person with no
  memberships at all is *not* halted — that is the arrived-before-role case.
- A cohort-scoped step whose cohort is active but where the person no longer holds an
  active membership → deferred (`membership_missing`); the orientation is never sent
  with the wrong content.

A halted step is not counted as an attempt and is not marked anything: `release_claim`
puts it back to `pending`, unclaimed, and the event is logged as
`onboarding_step_halted_inactive_cohort`. Reactivating the cohort therefore resumes the
journey with no operator action (tested: after `set_cohort_active(id, True)` the next pass
delivers both steps). The archiving task (Sprint 4) can additionally call
`domain.onboarding.halt_steps_for_cohort(cohort_id)` to mark rows `halted` permanently
and stop the per-poll re-check; the cost of not doing so is one claim/release per halted
row per poll interval.

Both the database-backed test (`test_inactive_cohort_halts_steps_and_reactivation_resumes`)
and the live verifier (create cohort → add the real user as learner → deactivate →
enqueue an orientation → wait 2 × poll + 10 s → DM count unchanged, row still `pending`
and unclaimed) observe the silence rather than reason about it.

## Verification

Three layers; the first two were run on a throwaway PostgreSQL 16 (`s1e5`, migrated with
`alembic upgrade head`) during development, the third is the live-stack verifier.

**1. Unit tests (no DB, no network).** From `ai-core/`:

```bash
APP_ENV=test .venv/bin/python -m pytest -q tests/test_onboarding.py
# 52 passed
```

Covers: all 15 templates render; ≤ 40 % line overlap between role variants of the same
kind; role-specific phrases present and forbidden ones absent; `is_halted` for every
case; backoff schedule `[60, 120, 240, 480, 960, 1920, 3600, 3600]` and the configured
base; `pick_primary_membership` ordering (most recent active cohort wins, inactive
ignored); `DispatchSummary` counting; missing-table detection; wake-listener
registration.

**2. Database-backed tests with a fake Mattermost client** (skipped unless
`SPRINTFLOW_INTEGRATION_DB=1`). From `ai-core/` with `POSTGRES_*` pointing at a migrated
throwaway database:

```bash
SPRINTFLOW_INTEGRATION_DB=1 APP_ENV=test POSTGRES_HOST=127.0.0.1 POSTGRES_PORT=55432 \
POSTGRES_DB=s1e5 POSTGRES_USER=sprintflow POSTGRES_PASSWORD=devpass \
.venv/bin/python -m pytest -q tests/integration/test_onboarding_db.py
# 13 passed
```

Scenarios (each asserts row state *and* the fake's post count): arrival twice → one
welcome row, one post, second pass sends nothing; delivery failure → not marked sent,
`attempt_count = 1`, `next_attempt_at = now + 60 s`, nothing claimed inside the window,
sent on the next pass after the fake recovers (`attempt_count = 2`); a refused post (client
returned `None`) counts as a failure; gives up as `failed` after `ONBOARDING_MAX_ATTEMPTS`;
inactive cohort → both steps halted, pending, unclaimed, and reactivation resumes; two
dispatchers over 20 due steps → 20 posts, each once; role after a role-less welcome → one
orientation post naming the cohort and its lead, and repeating the hook sends nothing;
role before the welcome → the welcome carries tech-lead content and the orientation row is
recorded sent with the same post id; a follow-up due in the past → sent; only inactive
memberships halt a workspace step; `ONBOARDING_ENABLED=false` enqueues nothing; only the worker holding a step's claim may settle it
(`test_settlement_requires_the_claim_holder`); a role assigned while the welcome is already claimed
still gets its own orientation row (`test_role_assigned_while_welcome_is_claimed_still_gets_an_orientation`).

**3. Live stack.** The same scenarios run in-container against the real database
(`scripts/_onboarding_probe.py`, piped over stdin, `verify-onb-` rows, cleaned up; its rows
are due 30 days out so the live dispatcher never touches them), then the host script
drives the real event path and counts real DMs:

```bash
# stack up and bootstrapped (make up && make bootstrap); from the repository root
set -a; . ./.env; set +a
python3 scripts/verify_onboarding_journey.py
```

Stages, each printing the actual bot-post count in the new user's DM:

1. In-container probe: 31/31 checks (as observed on the throwaway database).
2. Register a brand-new account over REST (as a real signup) → wait ≤ 90 s → **exactly 1**
   bot post in the DM (the existing `verify_onboarding.py` team auto-join still runs first;
   it is unchanged).
3. **Replay**: call `start_journey` for that user inside the container — the exact code
   the `new_user` event runs (Mattermost has no API to re-emit the event itself) — assert
   `created=False`, wait 30 s, assert the DM still holds **exactly 1** bot post.
4. Move `follow_up.due_at` to `now()`, `docker compose restart ai-core`, wait ≤ 2 × poll +
   60 s → **exactly 2** bot posts; the row is `sent` with the post id.
5. Create cohort `verify-onb-<stamp>`, add the user as learner, deactivate it, enqueue an
   orientation → wait 2 × poll + 10 s → still **exactly 2** posts; the row is `pending`,
   unclaimed.
6. Watch 20 s more → still 2 (the bot never answered its own DMs). Clean up.

What was observed: layers 1–2 and the probe were executed during development and their
numbers above are measured. Layer 3 — the real `new_user` WebSocket event, real REST
delivery, a real `docker compose restart ai-core` and the kill switch — was executed at
integration on the Compose stack on 2026-09-03 (`python3 scripts/verify_onboarding_journey.py`,
exit 0). Observed bot-post counts in the new person's DM: **1** after arrival (the welcome,
within the 90 s window), **1** thirty seconds after `start_journey` was replayed for the same
person (`created=False`, no second greeting), **2** after the follow-up was made due and
`ai-core` was restarted (the follow-up row `sent` with the post id), still **2** seventy
seconds after an orientation was enqueued for a cohort that had been deactivated (row left
`pending`, unclaimed), and still **2** after a further 20 s watch (the bot never answered its
own DMs). The in-container probe reported 31/31 on the live database, scoped to its own
users. The at-least-once window described under *Exactly once* remains reasoned about, not
reproduced.

**Quality gates** (from `ai-core/`): `ruff check app/ alembic/ scripts/ tests/` and
`ruff format --check …` clean; `pyright` 0 errors; `pytest -q tests/` 52 passed, 11
skipped without the integration flag.
