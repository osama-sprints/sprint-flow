# Ceremony Scheduling Through Conversation — Sprint 1 / task s1e4

This report describes what the code in this branch does and how it was verified. Every
claim below is backed by a unit test, a database-backed test, or the verifier, which was
run in full — including the live Mattermost conversation layer — on the Compose stack on
2026-09-03 (see the verification summary at the end).

Files:

| Layer | Path |
| --- | --- |
| Time interpretation (pure) | `ai-core/app/services/time_interpretation.py` |
| Scheduling service (authorise, interpret, conflicts, persist) | `ai-core/app/services/ceremony_scheduling.py` |
| Agent tools (thin, confirmation via `interrupt()`) | `ai-core/app/core/langgraph/tools/ceremonies.py` |
| Unit tests (no DB) | `ai-core/tests/test_time_interpretation.py`, `ai-core/tests/test_ceremony_scheduling.py` |
| Database-backed tests | `ai-core/tests/integration/test_ceremony_scheduling_db.py` (skipped unless `SPRINTFLOW_INTEGRATION_DB=1`) |
| Verifier | `scripts/verify_scheduling.py` (host, stdlib) piping `scripts/_scheduling_probe.py` into the container |

Reproduce every gate from `ai-core/` (a Postgres with the Sprint 1 schema at `POSTGRES_*`):

```bash
.venv/bin/ruff check app/ alembic/ scripts/ tests/
.venv/bin/ruff format --check app/ alembic/ scripts/ tests/
APP_ENV=test uv run --frozen --no-sync pyright                       # 0 errors
APP_ENV=test .venv/bin/python -m pytest -q tests/                    # 129 passed (unit only)
APP_ENV=test SPRINTFLOW_INTEGRATION_DB=1 .venv/bin/python -m pytest -q tests/   # 138 passed (unit + 9 DB scenarios)
APP_ENV=test .venv/bin/python ../scripts/_scheduling_probe.py checks # 33/33 checks passed
```

With the stack up and bootstrapped, from the repository root: `python3 scripts/verify_scheduling.py`
(add `--probe-only` to skip the Mattermost conversation).

## Time interpretation

**The policy, stated once.**

- *What is stored:* one timezone-aware instant, `ceremonies.scheduled_at` (`timestamptz`), always
  written in UTC. The zone the words were read in (`time_zone`) and the words themselves
  (`time_expression`) are stored beside it for the audit trail only; nothing downstream reads them.
- *What is displayed:* every time the assistant shows is rendered twice — in the person's zone
  ("Friday 4 September 2026, 14:00 (Europe/Berlin)") and in UTC ("2026-09-04 12:00 UTC"). The
  confirmation question, the calendar and the amendment summaries all use both renderings.
- *What happens to ambiguity:* it becomes a question. The interpreter returns a status other than
  `ok` with a `question`, the tool answers `[TIME_CLARIFICATION_REQUIRED] <question>`, and no row is
  written. There is no default anywhere on the path: no default meridiem, no default day, no
  default zone (the configurable default zone is a *configured* fallback, empty by default).

**Zone precedence** (`app.services.ceremony_scheduling.effective_zone` + the interpreter):

1. a zone written in the expression — an IANA name (`Europe/Berlin`, case-insensitive), `UTC`/`GMT`,
   an offset (`+02:00`, `UTC+2`, `GMT+05:30`) or a known abbreviation (CET, CEST, EET, EEST, WET,
   WEST, BST, EST, EDT, CST, CDT, MST, MDT, PST, PDT — fixed offsets; CET is always +01:00);
2. the person's Mattermost profile zone (`RequesterContext.timezone`, synced into `users.timezone`);
3. `SCHEDULING_DEFAULT_TIMEZONE` (empty in `.env.example`);
4. otherwise the status is `no_timezone` and the person is asked for their zone.

An unknown zone-like token after a time ("2pm IST", "2pm Mars/Base") is refused with a question
rather than silently falling back to the profile zone.

**What counts as explicit** (`interpret_time`, pure, `now` injected, no I/O):

| Input shape | Outcome |
| --- | --- |
| hour with am/pm (`2pm`, `2 p.m.`, `2:30pm`) | clock time |
| two-digit 24-hour `HH:MM` (`14:00`, `09:30`, `12:00`) | clock time |
| `noon`, `midday`, `midnight` | clock time |
| `at 13` … `at 23` | clock time |
| `2 in the afternoon/morning/evening` | clock time (this is the phrasing the clarifying question invites) |
| `at 2`, `at 12`, `2:30`, `2 o'clock` (≤ 12, no meridiem) | **ambiguous**: "Did you mean 2 in the afternoon or 2 in the morning?" |
| no clock time (`tomorrow`, `tomorrow morning`, `in 2 hours`) | **ambiguous**: asks for a clock time |
| no day (`3pm UTC`, `2pm`) | **ambiguous**: asks which day |
| `next week at 2pm`, `sept at 2pm`, `the 10th at 2pm`, `4/9 at 2pm` | **ambiguous**: does not name a full day / day-month order unclear |
| two times (`2pm and 3pm`) | **ambiguous** |
| `Feb 30 at 10am`, `tmrw at 2pm`, `13pm` | **unparseable** with a question |
| before `now + SCHEDULING_MIN_LEAD_MINUTES` (`yesterday at 3pm`, `today at 9am` at 10:00) | **past** with a question |
| beyond `SCHEDULING_MAX_HORIZON_DAYS` (`in 3 years at 10am`) | **too_far** with a question |
| DST gap (`2026-03-29 02:30` Europe/Berlin) | **ambiguous**: "does not exist … clocks go forward" |
| DST fold (`2026-10-25 02:30` Europe/Berlin) | **ambiguous**: "happens twice … clocks go back" |

How it works: the clock time and the zone are tokenised first with anchored regular expressions
(`\b(\d{1,2})(?::(\d{2}))?\s*(am|pm|a\.m\.|p\.m\.)`, `\b([01]\d|2[0-3]):[0-5]\d\b`,
`\bat\s+(1[3-9]|2[0-3])\b`, `noon|midday|midnight`) and removed from the text; only the remaining
date phrase goes to `dateparser` (`TIMEZONE`, `RETURN_AS_TIMEZONE_AWARE`, `PREFER_DATES_FROM=future`,
`RELATIVE_BASE=now in the zone`). The tool never scans the string for bare digits 13–23, so a year
can no longer be read as an hour: `January 1, 2100 at 12:00 UTC` resolves to exactly 12:00 UTC
(`test_year_digits_are_never_read_as_a_24_hour_time`, the review-02 bug). The wall-clock time is
then attached with `zoneinfo`, gaps and folds are detected before conversion, and the result is
converted to UTC. A single-digit `H:MM` without am/pm is deliberately treated as ambiguous rather
than as 24-hour time, because "tomorrow at 2:30" is not clearer than "tomorrow at 2".

The same words give the same instant whatever the server's `TZ` is
(`test_same_words_same_instant_regardless_of_process_zone`; review-01 finding).

Awkward cases exercised and recorded in `tests/test_time_interpretation.py` (129 unit tests in
total across the two files): the DST gap and fold in Europe/Berlin and America/New_York, a time
with no date, a date with no time, impossible dates, the 12am/12pm/noon/midnight corners, an
explicit zone overriding the profile zone, an unknown zone, past and horizon limits, and the
`parse_yes_no` confirmation vocabulary.

## Confirmation flow

`schedule_ceremony` is a thin wrapper: `prepare_schedule` (reads only) → `interrupt(question)` →
`parse_yes_no(reply)` → `commit_schedule` (the single write). The question is built by
`ScheduleProposal.confirmation_question()` and states the type, cohort, the instant in the person's
zone **and** in UTC, the duration, the sprint and the agenda, then asks for yes/no:

> Please confirm: schedule Sprint Planning for cohort 'Backend-01' on Friday 4 September 2026,
> 14:00 (Europe/Berlin) — that is 2026-09-04 12:00 UTC — lasting 90 minutes with agenda: Plan the
> sprint. Reply 'yes' to book it or 'no' to leave it unscheduled.

`interrupt()` is LangGraph's own pause: `graph.get_response` returns the question verbatim as the
assistant's message (no model in between), and the person's next message resumes the graph with
`Command(resume=<text>)`. Because LangGraph replays the whole tool call on resume, everything
before the `interrupt()` is read-only and the one write happens after it; `commit_schedule`
re-checks authority and overlap at write time so a change that happened while the person was
deciding still cannot slip through. Replies are read by `parse_yes_no`: yes/y/yeah/yep/confirm/
confirmed/correct/ok/okay/go ahead/do it/book it/sure/please do/proceed → commit; no/nope/cancel/
wrong/don't/stop/change/not yet → `[CONFIRMATION_DECLINED] Nothing was scheduled.`; anything else
→ declined with "Your reply was not a clear yes". A negative word wins over an affirmative one
("yes but change the time" is not consent).

Time amendments and cancellations go through the same `interrupt()`; agenda-only amendments apply
immediately. `GraphInterrupt` is never caught (the `guarded_tool` re-raises `GraphBubbleUp`).

**The confirmation protocol (structured interrupt).** LangGraph replays the whole tool call when the
person answers, and a relative expression re-interpreted on replay can drift ("tomorrow at 2pm" asked
at 23:59 and answered at 00:01 would name a different day). The tool therefore interrupts with a
payload, not a string: `{"question": "...", "scheduled_at": "<ISO instant or null>"}`. The person sees
only the question (`graph.pending_interrupt` renders `question`). When they answer, the conversation
layer resumes with `{"reply": "<their text>", "interrupt": <that payload>}` (`graph.resume_value`);
plain-string interrupts such as `ask_human` still receive the plain text. On replay `_confirm`
compares the instant it is about to store with the echoed one: equal → parse the reply; different →
ask again with the new instant (the stale answer is discarded, nothing is written). Any other tool
that wants a verified confirmation follows the same shape; tool calls within one model turn run one
at a time, so an answer always reaches the question it was given for.

**Verified in isolation** (database-backed test `test_confirmation_gates_persistence_and_stores_the_exact_instant`
and probe checks 7–16): the tool run inside a `StateGraph` with a `MemorySaver` pauses with the
question; zero rows exist while paused; `resume="no"` leaves zero rows; `resume="maybe later"`
leaves zero rows; `resume="yes"` produces exactly one row whose `scheduled_at` equals the instant
named in the question. **Verified in the product** at integration on 2026-09-03 (see the
verification summary at the end). `scripts/verify_scheduling.py` performs the real conversation:
the admin (given a `scrum_master` membership in a cohort created by the probe; the admin is a
superadmin anyway, so the membership documents the cohort-role path rather than being required)
DMs `schedule sprint planning for tomorrow at 2 pm for cohort Verify-Sched-<stamp>`, the script
asserts the reply contains an absolute time with `UTC` and `(Europe/Berlin)` and that no row exists
yet, replies `yes`, asserts through the probe that exactly one ceremony exists with `scheduled_at`
equal to the confirmed instant, then sends `schedule the retro for tomorrow at 2` and asserts a
clarifying question and no new row. The transcript will show: request → verbatim confirmation
question → `yes` → the model's summary of `[CEREMONY_SCHEDULED] …` → the ambiguous request → the
verbatim clarification.

## Storage representation

One row in `ceremonies` per ceremony, written only by `commit_schedule` through
`app.services.domain.ceremonies.create_ceremony`:

| Column | Written as | Meaning |
| --- | --- | --- |
| `scheduled_at` | `timestamptz`, UTC, tz-aware (`create_ceremony` rejects naive values) | the one instant; identical for every reader |
| `duration_minutes` | int (> 0, check constraint) | end = start + duration |
| `ceremony_type_id` | FK to the seeded `ceremony_types` row (alias-resolved; unknown → `VALIDATION_ERROR` listing the five) | no on-demand types |
| `cohort_id`, `sprint_id` | FKs; sprint resolved by name within the cohort | scope |
| `organizer_id` | FK to the **stored requester** (`users.id` from `require_cohort_authority`) | never a tool argument |
| `agenda`, `notes` | text | shown in reminders / added afterwards |
| `status` | `scheduled` / `cancelled` / `completed` | cancellation is a status change, in the trail |
| `channel_id` | the cohort's `mattermost_channel_id` when known | where reminders post |
| `time_expression`, `time_zone` | the words and the zone they were read in | audit only |

Round trip proved: the DB test asserts `stored.scheduled_at == expected` where `expected` is
tomorrow 14:00 Europe/Berlin converted to UTC independently of the interpreter, and that
`utcoffset() == 0` on read-back; the probe asserts the same against the confirmed instant.

Every amendment is traceable: `update_ceremony` writes one `ceremony_amendments` row per changed
field (`field`, `old_value`, `new_value`, `amended_by_id`, `reason`) in the same transaction as
the change. Moving a ceremony records `scheduled_at` (ISO old/new), `time_expression` and
`time_zone`; cancelling records `status: scheduled → cancelled`. Unchanged fields write nothing, so
repeating an amendment does not pad the trail.

## Authorisation reuse

There is one authorisation implementation, `app/services/authorisation.py`, and this task calls it;
it adds no rule of its own:

- `prepare_schedule` / `commit_schedule` / `prepare_amendment` / `commit_amendment` →
  `require_cohort_authority(requester, cohort_id)` (superadmin, or an active `tech_lead` /
  `scrum_master` membership in **that** cohort, read from the database at decision time);
- `list_calendar` → `require_cohort_membership(requester, cohort_id)` (any active role, or superadmin);
- `require_active_cohort` for the kill switch (a `ValidationFailed`, not a refusal).

Identity comes only from the `current_requester` ContextVar the conversation layer binds; no tool
takes a person, a user id or an organiser as an argument (review-02 finding 1 and 3). The
organiser stored on the row is the user returned by the authorisation decision. Refusals surface
as `[AUTHORISATION_REFUSED] Refused: You do not have permission to execute this action.` and
validation problems as `[VALIDATION_ERROR] …` — distinct codes, distinct exceptions.

Proved on the database: a learner, a non-member, an unsynced Mattermost id and a scrum master of
*another* cohort are all refused and the ceremony count stays at zero; the same through the tool
with the ContextVar bound; a learner cannot amend; a non-member cannot read.

## Conflict policy

**Policy: refuse by default; `warn` is available.** `SCHEDULING_CONFLICT_POLICY=refuse` (default)
or `warn`; any other value logs `scheduling_conflict_policy_unknown` and behaves as `refuse`.

Overlap is computed from start and duration (`find_overlapping_ceremonies`: two ranges overlap
when each starts before the other ends; cancelled and completed ceremonies never conflict; the
ceremony being amended is excluded). Under `refuse` the tool answers
`[CEREMONY_CONFLICT] That time clashes with #12 Sprint Planning on Friday 4 September 2026, 14:00
(Europe/Berlin), 2026-09-04 12:00 UTC, 90 min. The cohort cannot attend two ceremonies at once;
please choose another time or amend the existing ceremony.` and writes nothing. Under `warn` the
warning sentence is prepended to the confirmation question and to the final result, and the person
decides. In both modes an exact repeat (same type, same start) is refused as "already exists", so
sending the same request twice never creates a second identical ceremony. The check runs again
inside `commit_schedule`, so a clash that appeared while the person was deciding is still caught.

Why refuse is the right default for this product: a cohort's ceremonies are attended by the same
people, so two ceremonies at once means one of them has no audience; and the reminder task that
follows would page everybody for both. Refusing with the clash named turns a silent double-booking
into a one-line correction ("2pm clashes with the standup — 2:30 then"). `warn` exists for teams
that deliberately run parallel tracks (for example a Q&A that overlaps an optional demo); it keeps
the person informed without blocking them.

Proved on the database (`test_conflict_policy_refuses_by_default_and_warns_when_configured`, probe
checks 17–19): a 90-minute planning at 14:00 blocks a standup at 15:00 but not at 15:30; `warn`
proceeds with the warning; a proposal prepared under `warn` cannot be committed under `refuse`.

## Read access

`list_ceremonies(cohort, include_past=False, include_cancelled=False)` requires membership, not a
role: any active member of the cohort (learner included) or a superadmin can read; a non-member
is refused, so one cohort's calendar never leaks into another. The filters are explicit
(review-02): past ceremonies and cancelled ceremonies are hidden unless asked for. The rendering
lists, per ceremony, its id, type label, the time in the reader's zone **and** UTC, duration,
organiser handle, agenda and status:

```
Upcoming ceremonies for cohort 'Backend-01' (times shown in Africa/Cairo and UTC):
- #7 Open Q&A — Friday 4 September 2026, 15:00 (Africa/Cairo) / 2026-09-04 12:00 UTC — 60 min — organiser @lead — agenda: Ask anything — status: scheduled
```

A reader without a profile zone (and no configured default) sees UTC only. The integrator wires
`list_ceremonies` into the learner-support tool group as well as the back-office group so the
learner route can answer "when is the next retro".

## Downstream contract

The reminder (Sprint 2) and reporting (Sprint 4) tasks consume the rows without interpreting
anything:

- `ceremonies.scheduled_at` — `timestamptz`; compare directly with `now()`; render with the
  reader's zone at display time (`time_interpretation.format_local` / `format_utc` are reusable).
- `ceremonies.duration_minutes` — end instant = `scheduled_at + duration_minutes`.
- `ceremonies.ceremony_type_id` → `ceremony_types.key` / `label` (`daily_standup`, `sprint_planning`,
  `sprint_review`, `retrospective`, `open_qa`).
- `ceremonies.status` — send reminders only for `scheduled`.
- `ceremonies.agenda` — the text to include in the reminder; `notes` is post-hoc.
- `ceremonies.channel_id` — where to post (the cohort's channel when known), `cohort_id` for the
  member list, `organizer_id` for "who to ask".
- `app.services.domain.ceremonies.list_upcoming_ceremonies(within=timedelta(hours=24))` — every
  `scheduled` ceremony across cohorts starting in the window, soonest first: the reminder query.
- `ceremony_amendments` — for reports on what moved and why.
- `cohorts.is_active` — the kill switch every scheduled job filters on (scheduling refuses work on
  an inactive cohort with a validation error).

`time_expression` and `time_zone` are *not* part of this contract; they exist so an amendment can be
understood afterwards.

## Design decisions

- **Interpretation, persistence and conversation are three modules.** `time_interpretation` is
  pure (fixed `now`, no settings, no I/O) so every temporal rule has a table-driven test;
  `ceremony_scheduling` owns authorisation, conflicts and the two-phase prepare/commit; the tools
  only map outcomes to result codes and run the interrupt. Review 02 asked for exactly this split.
- **Tokenise the time yourself, let dateparser do only the date.** dateparser silently ignores
  "at 15", misreads "the 10th" as 3 October and "sept" as next year, and accepts stray words; the
  probes that showed this are why the interpreter guards those shapes explicitly instead of trusting
  a full-string parse. Supporting a wide range of phrasings was deliberately *not* a goal: the
  confirmation step is the safety net, so the interpreter's job is to be strict and to ask well.
- **Two-digit `HH:MM` is 24-hour; `H:MM` without am/pm is ambiguous.** Stricter than the brief's
  regex on purpose (see Time interpretation).
- **"next Monday" means the coming Monday.** dateparser's `PREFER_DATES_FROM=future` semantics;
  the confirmation shows the date, which is where a different reading gets caught.
- **Cancellation is confirmed too**, not only time changes: it is the amendment most likely to hit
  the wrong ceremony, and the question names the ceremony, its cohort and its time.
- **Past ceremonies are history.** Their time cannot change and they cannot be cancelled
  (`ValidationFailed` with the policy sentence); agenda and notes can still be updated so outcomes
  can be recorded. A cancelled ceremony cannot be edited; schedule a new one.
- **Commit re-validates.** `commit_schedule` re-runs authority and overlap; the proposal is data,
  not a capability.
- **Repeat safety.** Same request twice → "already exists" (either policy); same amendment twice →
  no new trail rows; cancel twice → "already cancelled".
- **Trade-off accepted:** the model must pass the person's words verbatim as `time_expression`
  (the tool docstrings say so, and the supervisor's back-office prompt tells it to confirm before
  hard-to-undo changes). If a model were to add an "am/pm" itself, the confirmation question would
  still show the resolved instant before anything is stored.

Verification summary — measured during development: 129 unit tests and 9 database scenarios pass
(`138 passed`), the probe reports `33/33 checks passed`, ruff check/format and pyright report no
errors. Measured at integration on the real Compose stack (2026-09-03, `python3 scripts/verify_scheduling.py`,
`SCHEDULING OK`, 11/11 live assertions): the admin's profile zone was set to `Europe/Berlin`; the DM
"schedule sprint planning for tomorrow at 2 pm for cohort Verify-Sched-…" produced the confirmation
question with the instant in both zones and **no row**; the reply `yes` produced exactly one row whose
`scheduled_at` equals the confirmed instant (Friday 4 September 2026, 14:00 Europe/Berlin = 12:00 UTC,
`sprint_planning`, `scheduled`, organiser = the admin's `users` row) and the bot's summary repeated both
renderings; the follow-up DM "schedule the retro for tomorrow at 2 …" produced the clarifying question
"Did you mean 2 in the afternoon or 2 in the morning? Please say '2pm', '2am' or use 24-hour time.",
asked for no confirmation, and created no row.

The same run also amends through conversation (22/22 assertions, `SCHEDULING OK`). "move ceremony #12
to 4 pm the same day" is refused rather than resolved against the ceremony's own date — the bot answers
"I could not read 'the same day' as a day. Please give the day as well…" and nothing changes, which is
the no-silent-guess rule applying to amendments as well as new bookings. Repeating it as "move ceremony
#12 to 2026-09-04 16:00 Europe/Berlin" produces a confirmation naming **both** instants ("move it from
Friday 4 September 2026, 14:00 (Europe/Berlin) (2026-09-04 12:00 UTC) to … 16:00 (Europe/Berlin) — that
is 2026-09-04 14:00 UTC"), still writes nothing, and only on "yes" moves the row to 14:00 UTC and writes
a `ceremony_amendments` row for `scheduled_at` attributed to the admin. Reading the calendar back in the
same conversation then shows the amended time.
