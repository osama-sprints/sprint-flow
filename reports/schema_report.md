# Sprint 1 / AI Eng 1 — Data Model and Migrations for the Corporate OS

Branch: `dev` (consolidated Sprint 1 foundation). Everything below is implemented
and verifiable in the repository; the commands quoted were run as written.

## Domain capabilities

### What was in the database before designing

The live compose database (`sprintflow` in `sprintflow-postgres`) was
inspected before the first migration was written, and again before this report:

```bash
docker exec sprintflow-postgres psql -U sprintflow -d sprintflow -Atc \
  "SELECT table_name FROM information_schema.tables WHERE table_schema='public' ORDER BY 1"
```

Before Sprint 1 the only tables were the four created by LangGraph's
`AsyncPostgresSaver.setup()` — `checkpoints`, `checkpoint_blobs`,
`checkpoint_writes`, `checkpoint_migrations` — and no `alembic_version`: the
template's Alembic setup had never been run. Development machines that applied
the pre-merge branch revisions additionally held `person`, `cohort`, `role`,
`ceremonytype`, `cohortmembership`, `sprint`, `ceremony`, `dailyprogress`,
`escalation`, `onboarding_state` and the template's `user` / `session` /
`thread`. Today the live database holds the four checkpointer tables (130
checkpoint rows at the time of writing), `alembic_version =
0001_sprintflow_domain`, and the eleven domain tables. Those two facts —
someone else already owns tables in this database, and nothing of ours had
ever been migrated — shaped the migration strategy below.

### The questions the model answers

The schema was designed backwards from these questions, not forwards from the
reference ERD. Each is answered by one typed call in `app/services/domain/`:

| Question | Table(s) | Data-access call |
| --- | --- | --- |
| Given a chat identity, who is this person? | `users` (`mattermost_user_id` unique) | `identity.get_user_by_mattermost_id`, `get_user_by_username`, `get_user_by_email` |
| Make sure this person exists and is current | `users` | `identity.upsert_mattermost_user` (INSERT … ON CONFLICT DO UPDATE) |
| Is this person a platform administrator? | `users.is_superadmin` | `identity.get_user_*` → `.is_superadmin`; used by `authorisation.require_superadmin` |
| Which cohort does "Backend-01" / "#7" mean? | `cohorts` | `cohorts.resolve_cohort` |
| What role does this person hold in this cohort? | `cohort_memberships` ⋈ `roles` | `cohorts.get_role_for_user_in_cohort` |
| Which cohorts is this person in, with which role? | `cohort_memberships` ⋈ `cohorts` ⋈ `roles` | `cohorts.list_memberships_for_user` |
| Who is in this cohort? | `cohort_memberships` ⋈ `users` ⋈ `roles` | `cohorts.list_cohort_members` |
| Give this person a role here (idempotently) | `cohort_memberships` | `cohorts.upsert_membership` → `(membership, created, previous_role_id)` |
| Is this cohort switched off? | `cohorts.is_active` | `cohorts.get_cohort`, `list_cohorts(active_only=True)`, `set_cohort_active` |
| Which sprint is this cohort in? | `sprints` | `sprints.get_active_sprint`, `get_sprint_by_name`, `list_sprints` |
| Would this sprint overlap another? | `sprints` | `sprints.find_overlapping_sprints` |
| What is scheduled for this cohort? / When is the next standup? | `ceremonies` ⋈ `ceremony_types` | `ceremonies.list_ceremonies`, `get_ceremony_type_by_key` |
| Does this time clash with another ceremony? | `ceremonies` | `ceremonies.find_overlapping_ceremonies` |
| What starts in the next hour, across cohorts? (reminders) | `ceremonies` | `ceremonies.list_upcoming_ceremonies` |
| Who changed this ceremony, from what, to what, why? | `ceremony_amendments` | `ceremonies.update_ceremony` (writes it), `list_amendments` |
| Did this learner submit today's standup? What blocks them? | `daily_standups` | `standups.upsert_daily_standup`, `list_daily_standups` |
| Which escalations are open / waiting on a human in this cohort? | `escalation_tickets` | `escalations.list_escalation_tickets(cohort_id, status=…)` |
| Which ticket does this DM reply belong to? | `escalation_tickets.human_dm_thread_id` | `escalations.get_escalation_ticket_by_human_thread` |
| Where must the answer be posted? | `escalation_tickets.learner_channel_id` / `learner_thread_id` | `escalations.get_escalation_ticket` |
| How long has a ticket been waiting? | `escalation_tickets.status_changed_at` | `escalations.set_escalation_status` stamps it on every transition |
| What onboarding message is owed to whom, and when? | `onboarding_steps` | `onboarding.enqueue_step`, `claim_due_steps`, `mark_step_sent`, `mark_step_failed` |

Every question is exercised with real rows by `ai-core/scripts/verify_schema.py`
(see *Verification*).

## Schema design and tradeoffs

The reference ERD (`tasks/general/01-database-schema.md`) is a valid answer; it
is not the one shipped. Every departure is listed with its benefit **and its
cost**. Names follow the contract vocabulary agreed with the four dependent
tasks (`ai-core/docs/sprint1_contract.md`): `User`/`users`, `Role`/`roles`,
`Cohort`/`cohorts`, `CohortMembership`/`cohort_memberships`,
`Sprint`/`sprints`, `CeremonyType`/`ceremony_types`, `Ceremony`/`ceremonies`,
`CeremonyAmendment`/`ceremony_amendments`, `DailyStandup`/`daily_standups`,
`EscalationTicket`/`escalation_tickets`, `OnboardingStep`/`onboarding_steps`.

| # | Departure from the ERD | Why (benefit) | Cost accepted |
| --- | --- | --- | --- |
| 1 | **Integer primary keys** instead of `uuid` on every table | Foreign keys stay 4 bytes and index-friendly; ids are readable in logs, chat replies (`ceremony 42`) and `psql`; the tool signatures in the contract take `ceremony_id: int`. Nothing here is ever merged across databases, which is the case uuids are for. | Ids are guessable and reveal ordering. Acceptable: no id is ever accepted from a person without an authorisation check on the row's cohort. |
| 2 | **`users` is the single identity concept** — `mattermost_user_id` (unique), `username`, `email`, `display_name`, `timezone`, `last_synced_at`, `is_superadmin`; the template's `user` / `session` / `thread` (JWT chat) tables are dropped | Review 01 rejected a parallel `Person`. Every inbound event carries a Mattermost id, so the row is created/refreshed by one upsert on every event and downstream code can rely on it existing. | The `users` row is a *cache* of the Mattermost profile: `username` and `email` can go stale until the next event from that person (`last_synced_at` says how stale). Lookups by handle therefore prefer the most recently synced row. |
| 3 | **`is_superadmin` synced from `ADMIN_EMAILS`** into stored data rather than read from the env at decision time | Authorisation reads one source — the database — for both platform and cohort authority; the flag is auditable and the identity sync is the only writer. | An allowlist change takes effect for a person on their next event, not instantly; and the flag can only be as fresh as the sync. Documented and accepted: `ADMIN_EMAILS` changes are rare and the sync runs on every message. |
| 4 | **`users.timezone`** (IANA zone from the Mattermost profile) | "Tomorrow at 2 pm" must be interpreted in the speaker's zone; storing it beside the identity means the scheduling task never makes a Mattermost call to find it. | One more profile field to keep in sync; `NULL` when the profile declares none (scheduling then asks or uses `SCHEDULING_DEFAULT_TIMEZONE`). |
| 5 | **Explicit plural `snake_case` table names** (`__tablename__` on every model) | SQLModel's implicit names (`ceremonytype`, `cohortmembership`) were called out by both reviews; explicit names cannot drift when a class is renamed. | Every model carries a `__tablename__` line and a pyright ignore for SQLModel's typing of it. |
| 6 | **Roles and ceremony types are seeded lookup tables** with a lowercase machine `key`, a display `label` (and a `description` / `default_duration_minutes`), mirrored by `StrEnum`s in `app/models/enums.py` with typed aliases (`"retro"`, `"scrum master"`, `"q&a"`) | Rows can be added without a migration (the ERD's own rule); foreign keys give referential integrity; the enum gives the Python boundary type safety and one place for labels and aliases; lowercase keys end the `Tech_Lead` vs `tech_lead` casing drift the reviews reported. | Two sources that must agree — enforced by `tests/test_schema.py` (migration seed snapshot == enums) and by the startup seed which refreshes labels. Adding a role still needs a code change if a tool must reason about it (`COHORT_ADMIN_ROLES`). |
| 7 | **`cohort_memberships` holds ONE role per (user, cohort)** — `UNIQUE (user_id, cohort_id)`; plus `status`, `joined_at`, `assigned_by_id` | Review 02: "decide whether one user can have multiple roles in one cohort — consumers call `.first()`". With one row, "what is your role here?" has exactly one answer, `assign_role` becomes an update that can report `ROLE_CHANGED` with the previous role, and authorisation is a single indexed lookup. Different roles in *different* cohorts remain separate rows — the non-negotiable invariant. | A person cannot be both Tech Lead and Scrum Master of the same cohort; they must pick one. `status` keeps history instead of deleting rows. |
| 8 | **`cohorts.is_active` boolean + `deactivated_at`** instead of a `status` string; **`mattermost_team_id` and `mattermost_channel_id`** added; `starts_on` / `ends_on` calendar dates; `created_by_id` | The only lifecycle question later tasks ask is "is it switched off?" (Sprint 4 archiving, reminders, standups); a boolean with an index is the cheapest filter and cannot be misspelled. The two Mattermost ids let reminders and announcements find the team/channel without a lookup by name. | A richer lifecycle (`draft`, `archived`, …) would need a new column later. `mattermost_team_id` is nullable because a cohort can exist before its team does. |
| 9 | **`sprints` use calendar `date`s** (`start_date`, `end_date`, `CHECK end_date >= start_date`), `status`, `UNIQUE (cohort_id, name)`, `opened_by_id` | A sprint runs "Monday to the Friday after next" for everyone regardless of zone; dates say exactly that and overlap checks are pure date arithmetic. Ceremonies inside carry exact instants. | No sub-day precision; a sprint cannot start "at 14:00". Not a real requirement. |
| 10 | **`ceremonies`** keep a **separate table** (not merged with standups into a generic events table) and add `duration_minutes` (`CHECK > 0`), `status`, `time_expression`, `time_zone`, `channel_id`, `notes`; `organizer_id` is a typed FK to `users`; `sprint_id` nullable | Ceremonies (a scheduled slot for a cohort) and standup entries (a person's daily text, one per day) share no columns except a cohort; a generic table would carry ~10 nullable columns and a `kind` column that every query filters on, and would let a "standup entry" have a duration. `duration_minutes` is what makes conflict detection meaningful (start + duration). `time_expression` and `time_zone` record *how* an instant was arrived at so an amendment can be understood later and never re-parsed. `status` replaces a boolean so `completed` is expressible. | Two tables to filter by `cohort_id` when building a dashboard instead of one; a future "generic reminder" would need its own table. Merging was the closest call; the summary and reminder tasks were consulted and both want typed columns. |
| 11 | **`ceremony_amendments` audit table** (not in the ERD) — one row per changed field: `field`, `old_value`, `new_value`, `amended_by_id`, `reason` | A mis-heard reschedule is the most likely scheduling failure; "who moved the retro and from when?" must be answerable without log archaeology. A separate table keeps `ceremonies` narrow and lets the trail be arbitrarily long; `update_ceremony` writes both in one transaction. | Writes are one row per changed field, and the trail is text (`old_value` is rendered, not typed). Acceptable for an audit trail read by humans. |
| 12 | **`daily_standups` with explicit `what_i_did` / `what_i_will_do` / `blockers`**, `UNIQUE (sprint_id, learner_id, log_date)`, `learner_id` → `users` (not a membership id) | Review 01 asked to confirm; the summary task highlights blockers without parsing free text, so the three fields are load-bearing. Keying on `learner_id` matches how every downstream task reasons ("the learner"), and a person's entries survive a role change. | The entry is not tied to the membership that existed on that day; the sprint's cohort plus the user is enough for reporting. A generic `notes` column was rejected because "blockers" would then be a convention, not a column. |
| 13 | **`escalation_tickets`**: integer `id` + unique `ticket_ref` (`ESC-000042`), `ticket_type` (`tech`/`ops`), `status` (`open`/`waiting_human`/`resolved`), `status_changed_at`, `question`, `answer`, `resolved_at`, **both** `learner_channel_id`/`learner_thread_id` and `human_dm_channel_id`/`human_dm_thread_id`, `sprint_id` | The ERD's `ticket_ref` primary key is kept as the human-facing reference (allocated from the row id, so unique under concurrency) while foreign keys stay integers. Both *channel + thread* pairs are stored because posting a reply in Mattermost needs the channel as well as the root post; the DM thread id is the correlation key for the human's reply. `status_changed_at` is stamped on every transition so Sprint 4's chaser can find tickets stuck in `waiting_human`. `answer` and `resolved_at` close the loop in the same row. | The ref is allocated in a second `UPDATE` inside the same transaction (a `PENDING-…` placeholder satisfies `NOT NULL UNIQUE` until then). Denormalising channel ids duplicates what Mattermost knows, deliberately: the proxy flow must work without a Mattermost round trip. |
| 14 | **`onboarding_steps` — a durable outbox** (not in the ERD): one row per owed delivery with `due_at`, `status`, retry bookkeeping and a lease (`claimed_at`, `claimed_by`); `UNIQUE (user_id, cohort_id, step_kind) NULLS NOT DISTINCT` | The onboarding task needs "exactly once, even after a restart, even if the event is replayed". A queue table gives that structurally: `INSERT … ON CONFLICT DO NOTHING` makes a replayed `new_user` event a no-op, and `SELECT … FOR UPDATE SKIP LOCKED` lets two dispatchers partition the work. `NULLS NOT DISTINCT` (PostgreSQL 15+) is what makes the welcome (`cohort_id IS NULL`) unique per person — under default semantics two `NULL`s never conflict. | Requires PostgreSQL ≥ 15 (the stack runs 16). Delivery state lives in the database rather than in memory, which is the point. |
| 15 | **`timestamptz` everywhere** (`created_at`, `updated_at`, `scheduled_at`, `due_at`, `status_changed_at`, …) and the DAL refuses naive datetimes (`require_aware`) | Review 01 found `timestamp without time zone`; later tasks schedule across zones. A naive value would be interpreted in the *server's* zone, silently. | Every caller must construct aware datetimes; the guard turns a silent bug into a `ValueError`. |
| 16 | **Lifecycle values are strings, not PostgreSQL enum types** | A new value needs no `ALTER TYPE`; the `StrEnum`s at the Python boundary give type safety where it matters. | The database does not reject an unknown status; the DAL only ever writes enum members. |

Rejected alternatives, for the record: polymorphic references (`subject_type`,
`subject_id`) on tickets — cheaper to add kinds, but no foreign keys and every
join needs a `CASE`; a global role on `users` — makes cohort-scoped authority
impossible (the one forbidden design); uuid keys (see #1); merging standups
into ceremonies (see #10).

## Invariants

| Invariant | Enforced by | Demonstrated by |
| --- | --- | --- |
| A person can hold different roles in different cohorts; a single global role is impossible | `cohort_memberships` is the only place a `role_id` exists (`users` has none); one row per (user, cohort) | probe "one person holds different roles in two cohorts (learner in A, tech_lead in B)"; host verifier inserts the same with `psql` and counts `DISTINCT role_id = 2`; `tests/test_schema.py::test_one_role_per_person_per_cohort_is_a_database_constraint` |
| Exactly one role per person per cohort | `uq_cohort_memberships_user_cohort`; `upsert_membership` updates instead of inserting | probe "repeating a role assignment creates no duplicate"; host verifier "a second role … rejected by the database"; integration test with 10 concurrent first assignments → one row |
| A chat identity resolves to one stable record | unique index on `mattermost_user_id`; upsert on conflict | probe "identity upsert is idempotent"; 10 concurrent upserts → one row (integration test) |
| The pre-existing conversation tables are never touched | `include_object` + `EXTERNALLY_OWNED_TABLES`; migration references only its own tables; explicit `downgrade()` list | host verifier: fake `checkpoints` row survives upgrade, second upgrade, downgrade and re-upgrade; live `alembic check` reports no operations |
| Stored instants are unambiguous | `DateTime(timezone=True)` on every instant column; `require_aware` in the DAL | probe "every timestamp column is timestamptz" (per table) and "a naive … is rejected" (ceremonies, onboarding, amendments); unit test over the metadata |
| Cohort names are unique regardless of case | `ux_cohorts_name_lower` | probe "cohort names are unique case-insensitively"; `resolve_cohort("BACKEND-01")` |
| Touching ceremonies do not conflict; overlapping ones do | `find_overlapping_ceremonies` uses strict `<` / `>` on `[start, start+duration)` | probe and integration test: 10:00–10:30 vs 10:30–11:00 → no conflict; vs 10:29 → conflict; cancelled → never |
| Ticket references are unique under concurrency | allocated from the row id inside the insert transaction; unique index | integration test: 10 concurrent tickets → 10 distinct `ESC-\d{6}` |
| Onboarding deliveries are exactly-once | `NULLS NOT DISTINCT` unique key + `ON CONFLICT DO NOTHING`; `FOR UPDATE SKIP LOCKED` + lease | probe (enqueue twice → one row; second worker gets nothing under a live lease); integration test (10 concurrent enqueues → 1 row; two workers claim disjoint sets while A's transaction is open) |
| Deactivating a cohort halts its automation | `cohorts.is_active` + `halt_steps_for_cohort` | probe "cohort kill switch …" and "deactivating a cohort halts its pending onboarding steps" |
| Repeating any setup step changes nothing | idempotent migration (Alembic version table), `ON CONFLICT` seeds | host verifier: second `upgrade head` runs no migration; counts unchanged across three seed runs |

## Migration strategy

### One consolidated revision, plus a forward revision

`ai-core/alembic/versions/0001_sprintflow_domain_schema.py` creates all eleven
tables with named primary keys (`pk_*`), foreign keys (`fk_<table>_<column>_<target>`),
unique (`uq_*`) and check (`ck_*`) constraints, seeds `roles` and
`ceremony_types`, and drops the template's unused `user` / `session` / `thread`
and the branch-era `onboarding_state` **only if present**. `downgrade()` drops
the eleven domain tables in reverse dependency order and nothing else.

`0002_sprints_name_case_insensitive.py` (revision `0002_sprints_name_ci`, the current
head) adds the functional unique index `ux_sprints_cohort_name_lower` on
`(cohort_id, lower(name))` so the database enforces the case-insensitive sprint-name
rule the back office already applies. It was added as a forward revision, not by
editing `0001`, because `0001` had already been applied to the running stack; its
`upgrade()` uses `if_not_exists` and its `downgrade()` drops only that index.

Why one revision rather than a forward chain on top of the six pre-merge
revisions (`b25d38b0cd7c` … `eba532c6476d`): Review 02 showed the chain could
not be downgraded (unnamed FK on `escalation`) and could not upgrade a
populated database (`ceremony.organizer NOT NULL` without a backfill). Those
revisions had been applied only on development machines, so rewriting history
before merge — which the review explicitly allowed — was cheaper and safer than
five migrations of renames and backfills that would exist only to repair a
branch nobody deployed. The cost is a one-time reset for those machines:

```bash
make db-reset     # prompts for the word 'reset'; drops the domain tables, every
                  # legacy branch table and alembic_version (checkpointer tables
                  # untouched), then runs `make migrate`
```

### Migrations run at container start

`ai-core/scripts/docker-entrypoint.sh` executes `alembic upgrade head` before
uvicorn unless `AI_CORE_MIGRATE_ON_START=false` (Review 02 finding 3: a clean
`docker compose up` had produced only the checkpoint tables). uvicorn runs a
single worker, so there is one migrator per container, and Alembic's version
table serialises concurrent containers. `/health` returns 503 while the
`cohorts` table is missing, so the container cannot report healthy without the
schema.

### Runbook

| Goal | Command (repository root, stack up) | Expected outcome |
| --- | --- | --- |
| Blank database → head, one command | `make up` (entrypoint applies it) or `make migrate` | `alembic upgrade head` logs `Running upgrade -> 0001_sprintflow_domain` then `0001_sprintflow_domain -> 0002_sprints_name_ci`; `make migrate-history` shows `0002_sprints_name_ci (head)` |
| Repeat | `make migrate` again | exit 0, no `Running upgrade` line |
| Reverse one step | `make migrate-downgrade` (`alembic downgrade -1`) | drops the sprint-name index (0002); a second step removes every domain table (0001); checkpointer tables untouched |
| Reverse fully | `docker compose exec -T ai-core /app/.venv/bin/alembic downgrade base` | same as above; `alembic_version` left empty |
| Seed / re-seed reference data | `make seed` (or automatically at every ai-core start) | prints `Reference data seeded: 4 roles, 5 ceremony types.`; row counts unchanged on repeat |
| Reset a dev machine that applied the old branch chain | `make db-reset` | confirmation prompt, then a fresh `0001` |
| Prove all of it on a fresh database without touching the live one | `python3 scripts/verify_schema.py` | 176 `PASS` lines (plus the smoke test), `SCHEMA VERIFICATION OK`, exit 0 |
| Fast local gates | `make verify-fast` (`lint` + `typecheck` + `test` in the container) | ruff clean, pyright 0 errors, pytest green |

Make targets added or changed in the root `Makefile`: `migrate`,
`migrate-downgrade`, `migrate-history`, `seed`, `db-reset`, `test`, `lint`,
`format` (replacing the stale `docker exec -it … uv run` version), `typecheck`,
`verify-fast`, and the `verify` chain in the order agreed for Sprint 1
(smoke → schema → authorisation → orchestration → scheduling → onboarding
journey → routing → threading → isolation → onboarding → admin agent → memory).
`psql` now targets the `postgres` service that actually exists in
`docker-compose.yml`.

### Review findings and where each is resolved

| Finding (review) | How resolved | Where |
| --- | --- | --- |
| Parallel `Person` concept (01) | one `User` model / `users` table; template `user`/`session`/`thread` removed | `app/models/user.py`, `app/models/__init__.py`, migration `LEGACY_TEMPLATE_TABLES` |
| One entity name for scheduled ceremonies with organizer, agenda, sprint, notes (01) | `Ceremony`/`ceremonies` with `organizer_id` FK, `agenda`, `notes`, `sprint_id` | `app/models/ceremony.py` |
| Instants stored without time zone (01) | `TZ_DATETIME = DateTime(timezone=True)` on every instant; probe asserts per table | `app/models/domain_base.py`, migration `TZ` |
| Escalation storage incomplete (01) / overlapping names (02) | one field per concept: `question`, `assigned_human_id`, `status`, `learner_channel_id`+`learner_thread_id`, `human_dm_channel_id`+`human_dm_thread_id` | `app/models/escalation_ticket.py` |
| Explicit standup fields (01) | `what_i_did`, `what_i_will_do`, `blockers` | `app/models/daily_standup.py` |
| Seeded keys agreed with dependent teams (01) / seed omits `admin`, adds `manager`/`coordinator` (02) | `RoleKey` = learner, tech_lead, ops_support, scrum_master (superadmin is a `users` flag, not a role); `CeremonyTypeKey` includes `open_qa` | `app/models/enums.py`, migration seed snapshot, `tests/test_schema.py` |
| Complete typed DAL with create/read for every entity (01) | seven aggregate modules, create/get/list for every table | `app/services/domain/*` (table in *Data access layer*) |
| Interface aligned with authorisation and scheduling (01) | names and signatures fixed in `ai-core/docs/sprint1_contract.md` §4; consumers: `app/services/authorisation.py`, `app/services/identity.py` | contract, `app/services/domain/` |
| Sync database calls in async flows (01, 02) | async psycopg engine, every DAL function `async` | `app/services/database.py` |
| `scripts/verify_schema.py` in `make verify` with the assistant smoke test (01) | host verifier + in-container probe; `verify` chain | `scripts/verify_schema.py`, `ai-core/scripts/verify_schema.py`, `Makefile` |
| Complete runbook in the report (01) | *Runbook* above | this file |
| Every departure justified with cost and benefit (01) | *Schema design and trade-offs* | this file |
| Ruff/format failures (01) | `ruff check` + `ruff format --check` clean on `app/ alembic/ scripts/ tests/`; `make lint` | — |
| Full downgrade broken on an unnamed FK (02) | every constraint named; `downgrade()` drops tables by explicit list; verifier runs `downgrade base` on a populated database | migration, host verifier |
| Populated upgrade broken by `NOT NULL` without backfill (02) | single revision creates the schema complete; verifier populates, downgrades, re-upgrades, re-populates and runs `alembic check` | migration, host verifier |
| Compose does not apply migrations (02) | entrypoint runs `alembic upgrade head`; `/health` 503 without the schema | `ai-core/scripts/docker-entrypoint.sh`, `app/main.py` |
| Seed command needs manual `PYTHONPATH` (02) | `seed_reference_data.py` inserts its own parent on `sys.path`; verifier runs the documented command as-is | `ai-core/scripts/seed_reference_data.py`, `make seed` |
| `app.models.database` did not export every model (02) | `app/models/__init__.py` exports all eleven + `DOMAIN_TABLES`; `env.py` imports the package; unit test pins `metadata == DOMAIN_TABLES` | `app/models/__init__.py`, `alembic/env.py`, `tests/test_schema.py` |
| Mixed-case lifecycle values across tasks (02) | lowercase `StrEnum`s shared by models, migration, services, tools | `app/models/enums.py` |
| Implicit table names `ceremonytype`, `cohortmembership`, `dailyprogress`; `type_id`, `organizer`, `duration_mins` (01, 02) | explicit plural names; `ceremony_type_id`, `organizer_id`, `duration_minutes` | models, migration |
| Multiple roles per cohort allowed but consumers assume one (02) | `UNIQUE (user_id, cohort_id)` | `app/models/cohort_membership.py` |
| Missing `mattermost_team_id` on cohorts (01, 02) | added, with `mattermost_channel_id` | `app/models/cohort.py` |
| `services/domain.py` as a merge hotspot (01, 02) | split by aggregate | `app/services/domain/` |
| Migrations named `add_domain_models`, `add_status_defaults` (01) | one revision `0001_sprintflow_domain` / `create the SprintFlow domain schema` | `alembic/versions/0001_sprintflow_domain_schema.py` |
| Verifier should test populated upgrades, full downgrade, constraints and behaviour, not only names and counts (02) | see *Verification* | both verifier scripts |

## Protecting existing tables

The database is shared with the LangGraph checkpointer (and could be with
mem0). Four layers keep those tables out of reach:

1. **`include_object` in `alembic/env.py`** returns `False` for any table in
   `EXTERNALLY_OWNED_TABLES` (`checkpoints`, `checkpoint_blobs`,
   `checkpoint_writes`, `checkpoint_migrations`, `longterm_memory`,
   `mem0migrations`) and for any column/index belonging to them, so
   `autogenerate` and `alembic check` never propose dropping or altering them.
   The list lives in `app/services/database.py` so it is importable and covered
   by `tests/test_schema.py`.
2. **The migration never references them.** `upgrade()` creates its own
   tables and drops only the template's `user` / `session` / `thread` and the
   branch-era `onboarding_state`, each guarded by `if legacy in existing`.
   Those four were never populated in any deployment (no migration had ever
   run) and no code references them.
3. **`downgrade()` is an explicit list**, not `metadata.drop_all()`.
4. **The verifier fakes the situation.** `scripts/verify_schema.py` creates a
   `checkpoints` table with one row *before* the first `upgrade head` and
   asserts the row is still there after the upgrade, the no-op upgrade, the
   populated `downgrade base` and the re-upgrade; against the live database it
   asserts all four checkpointer tables exist and `alembic check` reports "No
   new upgrade operations detected". After the smoke test it asserts
   `checkpoints` is populated, i.e. the assistant's conversation store is
   intact and in use.

## Reference data

Reference data is data, not code, and it is seeded in three places that agree
by construction:

- **In the migration** (`_seed_reference_data`, `INSERT … ON CONFLICT (key) DO
  NOTHING`) so a blank database is usable the moment `upgrade head` finishes —
  4 roles, 5 ceremony types, including `open_qa`.
- **At every ai-core start** (`lifespan` → `seed_reference_data()`, try/except,
  logs and continues) and via **`make seed`** — `INSERT … ON CONFLICT (key) DO
  UPDATE` refreshing labels, descriptions and default durations. Keys are never
  removed here; a removed row is restored.
- **In code**: `RoleKey`, `CeremonyTypeKey`, their labels, descriptions,
  default durations and typed aliases in `app/models/enums.py`.
  `tests/test_schema.py::test_migration_seed_snapshot_matches_the_enums` fails
  if the migration's snapshot and the enums drift.

Keys: roles `learner`, `tech_lead`, `ops_support`, `scrum_master` (only
`tech_lead` and `scrum_master` may administer a cohort — `COHORT_ADMIN_ROLES`;
platform administration is `users.is_superadmin`, not a role); ceremony types
`daily_standup` (15 min), `sprint_planning` (90), `sprint_review` (60),
`retrospective` (60), `open_qa` (60).

Repeatability is demonstrated, not claimed: the host verifier records counts
after the migration seed, after `make seed`'s command once and again twice, and
asserts `4/5` throughout; the probe calls `seed_reference_data()` twice and
compares row lists.

## Data access layer

### Engine and sessions

`app/services/database.py` builds one async engine per process on psycopg 3
(`postgresql+psycopg`, `pool_pre_ping`, sized by `POSTGRES_POOL_SIZE` /
`POSTGRES_MAX_OVERFLOW`) — the LangGraph checkpointer keeps its own
`psycopg_pool` and the two never share a connection. `session_scope(session)`
is the one transaction boundary: with no session it opens one, commits on
success and rolls back on any `BaseException`; with a session it yields it
untouched so several calls compose into one transaction (e.g. conflict check
+ insert, or ceremony update + amendment rows).

Sessions use `expire_on_commit=False` and models declare **no relationships**:
every function returns fully loaded rows (or plain values / `NamedTuple`s such
as `CohortMember`, `MembershipChange`), so a tool can read `cohort.name` after
the session closed and nothing lazy-loads outside a transaction — the
`DetachedInstanceError` class of bug cannot occur at the tool boundary. The
cost is that a caller wanting a join asks for it explicitly
(`list_cohort_members` returns `(membership, user, role)` triples).

### Every function, by module

| Module | Functions |
| --- | --- |
| `identity` | `get_user`, `get_user_by_mattermost_id`, `get_user_by_username` (handle with/without `@`, case-insensitive, most recently synced first), `get_user_by_email`, `upsert_mattermost_user`, `list_users` |
| `cohorts` | `create_cohort`, `get_cohort`, `get_cohort_by_name`, `resolve_cohort`, `list_cohorts`, `set_cohort_active`, `get_role_by_key`, `get_role`, `list_roles`, `get_membership`, `get_role_for_user_in_cohort`, `list_memberships_for_user`, `list_cohort_members`, `upsert_membership`, `set_membership_status` |
| `sprints` | `create_sprint`, `get_sprint`, `get_sprint_by_name`, `list_sprints`, `get_active_sprint`, `find_overlapping_sprints`, `set_sprint_status` |
| `ceremonies` | `get_ceremony_type_by_key`, `get_ceremony_type`, `list_ceremony_types`, `create_ceremony`, `get_ceremony`, `list_ceremonies`, `list_upcoming_ceremonies`, `find_overlapping_ceremonies`, `update_ceremony` (+ amendment rows, `AMENDABLE_FIELDS`), `list_amendments` |
| `standups` | `upsert_daily_standup`, `get_daily_standup`, `list_daily_standups` |
| `escalations` | `format_ticket_ref`, `create_escalation_ticket`, `get_escalation_ticket`, `get_escalation_ticket_by_human_thread`, `list_escalation_tickets`, `set_escalation_status` |
| `onboarding` | `enqueue_step`, `get_step`, `get_step_for`, `list_steps_for_user`, `claim_due_steps`, `mark_step_sent`, `mark_step_failed`, `release_claim`, `halt_steps_for_cohort`, `count_steps` |
| `reference_data` | `seed_reference_data` |

Consumers today: `app/services/authorisation.py` (`cohorts`, `identity`),
`app/services/identity.py` (`cohorts`, `identity`), `app/main.py`
(`reference_data`), `ai-core/scripts/seed_reference_data.py`, and the four
sibling tasks' services. None of them contains SQL.

### Defects found by trying to break it

The layer was hammered against a throwaway database (10 concurrent tasks per
scenario; the scenarios now live in `ai-core/tests/integration/test_schema_db.py`
and in the probe). What held: concurrent `upsert_mattermost_user` for one id
(one row, no error), `enqueue_step` race (one row, one `created=True`),
`claim_due_steps` from two workers with worker A's transaction still open
(disjoint sets, second claim under a live lease gets nothing, expired lease
re-claimable), `update_ceremony` with an unknown field (`ValueError`),
concurrent `create_escalation_ticket` (10 unique `ESC-\d{6}`),
`find_overlapping_ceremonies` boundaries (touching intervals do not overlap),
`find_overlapping_sprints` boundaries, `session_scope` rollback on error.

What broke, and the surgical fixes:

| Defect | Fix | File |
| --- | --- | --- |
| Concurrent first `upsert_membership` for the same (user, cohort): 9 of 10 callers got `IntegrityError` (a double-sent "assign role" would answer `SYSTEM_ERROR` instead of `ROLE_ALREADY_ASSIGNED`) | insert inside a savepoint (`begin_nested`); on `IntegrityError` re-read and continue as an update, so all callers converge on the one row | `app/services/domain/cohorts.py` |
| `enqueue_step` accepted a naive `due_at` (PostgreSQL would interpret it in the session zone) | `require_aware` guard on `due_at`, `next_attempt_at`, `now`; same helper now used by `create_ceremony`, `update_ceremony`, `find_overlapping_ceremonies` | `app/models/domain_base.py`, `app/services/domain/onboarding.py`, `ceremonies.py` |
| `get_user_by_username` ordered `last_synced_at DESC` without `NULLS LAST`, so a never-synced duplicate handle beat a freshly synced one | `.desc().nulls_last()` + `id DESC` tiebreak | `app/services/domain/identity.py` |
| The DAL used SQLModel's `AsyncSession.execute`, which is decorated `@deprecated` and warns on every call (identity upsert, outbox insert, startup seed) | `exec()` (accepts `Insert` since SQLModel 0.0.25) | `identity.py`, `onboarding.py`, `reference_data.py` |
| The in-container probe crashed in its own cleanup (`AsyncSession.exec(text, params)` — positional params) and left probe rows behind; it also failed under `python -` because `__file__` is undefined when piped over stdin | cleanup on a sync engine connection inside `finally`, keyed by the ids it created; `__file__` guarded | `ai-core/scripts/verify_schema.py` |
| Alembic's exclusion list lived only inside `env.py`, which cannot be imported (it runs migrations on import) | `EXTERNALLY_OWNED_TABLES` / `is_externally_owned` in `app/services/database.py`, used by `env.py` and unit-tested | `app/services/database.py`, `alembic/env.py` |

Not changed, deliberately: `create_cohort`, `create_sprint` and
`upsert_daily_standup` still surface `IntegrityError` if two callers race to
create the same name/day. The unique constraints guarantee no duplicate state
either way; the tools check by name first, and a racing second caller gets
`SYSTEM_ERROR` rather than a silent merge — acceptable for an admin action that
is retried by a human.

## Verification

### What `python3 scripts/verify_schema.py` checks (host, stdlib only)

Run from the repository root with the stack up and bootstrapped. It reads
`os.environ` (the Makefile sources `.env`; run by hand it loads `.env` for any
unset key). One `PASS`/`FAIL` line per assertion, exit 1 on any failure,
throwaway database dropped in a `finally`:

1. `docker compose ps` shows `postgres` and `ai-core` running (otherwise it
   stops before creating anything).
2. `createdb sprintflow_verify_<stamp>` inside the compose Postgres; a fake
   `checkpoints` table with one row is created with `psql`.
3. `docker compose exec -T -e POSTGRES_DB=<throwaway> -e AI_CORE_MIGRATE_ON_START=false ai-core /app/.venv/bin/alembic upgrade head`
   → exit 0, 11 domain tables, `alembic_version = 0002_sprints_name_ci`,
   checkpoints row intact. Run again → exit 0 and no `Running upgrade` line.
4. `…/python /app/scripts/seed_reference_data.py` twice (no `PYTHONPATH`) →
   exit 0 both times; role/ceremony-type counts `4/5` unchanged across the
   migration seed and both runs; keys are the agreed lowercase set, including
   `open_qa`.
5. The in-container probe `ai-core/scripts/verify_schema.py` piped over
   stdin with `-e SCHEMA_PROBE_EXPECT_CHECKPOINT_TABLES=checkpoints`; every
   probe line is echoed as `probe: … PASS/FAIL` and folded into the total
   (131 assertions: every table, columns equal to the model, every timestamp
   column `timestamptz`, unique/FK/PK/check constraints present and named,
   `NULLS NOT DISTINCT`, case-insensitive cohort name index, legacy tables
   absent; then, with real rows: seed twice, identity upsert and three-way
   resolution, two roles in two cohorts, repeat assignment, role change,
   inactive membership, kill switch, sprint and ceremony overlap boundaries,
   naive instants rejected, amendment trail, cancel, standup upsert, ticket
   reference and correlation, outbox idempotency and lease, halt — and
   cleanup with "no probe users left behind").
6. Populated cycle with `psql`: user + 2 cohorts + 2 memberships with
   different roles + sprint + ceremony; `DISTINCT role_id = 2`; a second role
   in the same cohort is rejected by `uq_cohort_memberships_user_cohort`;
   `alembic downgrade base` → exit 0, 0 domain tables, `checkpoints` table and
   row intact, no revision stamped; `alembic upgrade head` → exit 0, tables and
   seeds back; the population is re-applied at head; `alembic check` → "No
   new upgrade operations detected".
7. Live database, read-only: `alembic check` → no operations; `alembic
   current` contains `0002_sprints_name_ci (head)`; all four checkpointer
   tables and all 11 domain tables exist; `/health` (from inside the
   container) is 200 with `components.domain_schema == "healthy"`.
8. `scripts/smoke_test.sh` via subprocess → exit 0 (`SKIP_SMOKE=1` prints a
   `SKIP` line instead); then the live `checkpoints` table has > 0 rows.
9. `dropdb --if-exists --force` in `finally`.

Expected output: `176/176 checks passed` (plus 2 for the smoke step) and
`SCHEMA VERIFICATION OK`.

### What was actually run, and what was not

Run on a throwaway PostgreSQL 16 (`s1e1` on `sprintflow-devdb`) from this
branch:

| Check | Command | Result |
| --- | --- | --- |
| Migration up / check / down / up | `.venv/bin/alembic upgrade head`, `alembic check`, `downgrade base`, `upgrade head` | exit 0; "No new upgrade operations detected." |
| In-container probe as a file and piped over stdin | `.venv/bin/python scripts/verify_schema.py`; `… python - < scripts/verify_schema.py` (with `SCHEMA_PROBE_EXPECT_CHECKPOINT_TABLES=`) | 131/131, `SCHEMA VERIFICATION OK`, 0 rows left behind |
| Host verifier end to end | `python3 scripts/verify_schema.py` with `docker compose` shimmed onto the throwaway server and the local venv, four fake checkpointer tables standing in for the live database, `SKIP_SMOKE=1` | 176/176, throwaway database created and dropped |
| Unit tests | `APP_ENV=test .venv/bin/python -m pytest -q tests/test_schema.py` (34 tests); the whole Sprint 1 suite is 388 unit + 43 database-backed tests | all passed |
| DB-backed tests | `SPRINTFLOW_INTEGRATION_DB=1 … pytest -q tests/integration` | 7 passed; every scenario cleans up |
| Break script (10-way concurrency per scenario, since folded into the tests above) | ad hoc, deleted | 37/37 after the fixes listed above (32/37 before) |
| Gates | `ruff check`, `ruff format --check` (app/ alembic/ scripts/ tests/), `pyright` | clean; 0 errors |

Run at integration on the real Compose stack (2026-09-03): `python3 scripts/verify_schema.py`
reported **178/178 checks passed, SCHEMA VERIFICATION OK** — the throwaway database
`sprintflow_verify_20260903085452` was created inside the compose Postgres with a fake
pre-existing `checkpoints` row, migrated up twice, seeded twice with unchanged counts,
probed (131 in-container assertions), populated, downgraded to base (only domain tables
gone, the `checkpoints` row intact), migrated again, `alembic check` was clean on both the
throwaway and the live database, the live database was at head with the four real
checkpointer tables, `/health` answered 200 with `domain_schema: healthy`, the assistant
answered `scripts/smoke_test.sh` after the migration, the live `checkpoints` table was
populated by that conversation, and the throwaway database was dropped. On a fresh stack
the same schema was created by the container's own start (`alembic upgrade head` in the
entrypoint) before the service accepted traffic.
