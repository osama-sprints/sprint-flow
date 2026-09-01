# SprintFlow Data Model and Migrations Report

## 1. Overview

This report documents the SprintFlow relational schema, domain-model alignment, migrations, reference data, data-access layer, architectural decisions, and verification.

The implementation aligns the domain model with the agreed SprintFlow requirements while preserving the existing agent persistence and conversation infrastructure. It also documents the decisions made around identity, cohort-scoped relationships, ceremony scheduling, escalation tracking, progress tracking, timestamp handling, database access, and schema validation.

## 2. Domain Schema

The schema introduces the following domain entities:

- `User` — represents the unified SprintFlow user identity and optionally maps a user to a Mattermost account through a unique Mattermost user ID and handle.
- `Cohort` — represents a SprintFlow cohort and its lifecycle.
- `Role` — stores reusable role definitions.
- `CohortMembership` — associates a user with a cohort and assigns a cohort-scoped role.
- `Sprint` — represents a time-boxed sprint belonging to a cohort.
- `CeremonyType` — stores reusable ceremony types.
- `Ceremony` — represents a scheduled ceremony within a cohort, including its type, status, scheduled time, duration, organizer, agenda, channel, and raw input.
- `DailyProgress` — records daily progress for a cohort member within a sprint through explicit `what_i_did`, `what_i_will_do`, and optional `blockers` fields.
- `Escalation` — tracks learner questions and their human escalation lifecycle, including the assigned human and conversation thread identifiers.

Common `created_at` and `updated_at` fields are provided through `DomainBase` for domain entities. Domain timestamps are stored as timezone-aware values.

## 3. Cohort-Scoped Roles

Roles are assigned through `CohortMembership` rather than directly to `User`.

This allows the same user to hold different roles in different cohorts while keeping role assignments scoped to a specific cohort.

A unique constraint on `(user_id, cohort_id, role_id)` prevents duplicate assignment of the same role to a user within the same cohort while allowing the user to hold multiple roles in the same cohort and participate in multiple cohorts.

## 4. Domain Consistency

Foreign keys enforce that referenced records exist, but some relationships also require semantic cohort consistency.

For example, a `DailyProgress` record references both a sprint and a cohort membership. Basic foreign keys alone would allow those records to belong to different cohorts.

Rather than introducing composite foreign keys and additional schema complexity, these domain invariants are enforced in the typed data-access layer:

- `create_daily_progress()` verifies that the sprint and cohort membership belong to the same cohort.
- `create_escalation()` verifies that the membership belongs to the supplied cohort.
- When an escalation has a sprint, `create_escalation()` also verifies that the sprint belongs to the same cohort.

This keeps the relational schema straightforward while ensuring invalid domain combinations are rejected through the supported application access layer.

## 5. Status Fields

Lifecycle statuses remain string fields with application-defined values rather than PostgreSQL enum types.

Examples include:

- Cohort: `active`
- Sprint: `planned`, `active`, `completed`
- Daily progress: `pending`, `completed`, `skipped`
- Escalation: `pending`, `resolved`, `ignored`

This avoids coupling the schema to PostgreSQL-specific enum types and keeps future status changes simpler.

## 6. Migrations and Table Protection

The schema is managed through reversible Alembic migrations.

The domain schema was initially introduced through:

1. `ae9ba88585bc_add_domain_models.py` — creates the domain tables and relationships.
2. `a86e08b1fcb3_add_status_defaults.py` — applies the required non-null constraints for fields with model defaults.

The final domain-model alignment was then applied through:

3. `2399bc07d56b_align_domain_models.py` — replaces the `Person` identity model with the unified `User` model, updates `CohortMembership` to use `user_id`, and applies the remaining domain-model changes.
4. `eba532c6476d_make_domain_timestamps_timezone_aware.py` — converts the domain timestamp columns to timezone-aware timestamps.

The existing LangGraph checkpointer and conversation persistence tables were preserved and were not removed or recreated as part of the domain-model alignment.

All migrations are reversible through Alembic, allowing the database to be returned to a previous migration revision when required.

## 7. Reference Data

Reference data for roles and ceremony types is provided through an idempotent seed script.

The agreed role keys are:

- `learner`
- `mentor`
- `manager`
- `coordinator`

The agreed ceremony-type keys are:

- `planning`
- `review`
- `retrospective`
- `demo`

The seed script checks for existing records before creating them, so it can be safely re-run without creating duplicate reference records.

These keys should be treated as the shared identifiers used by dependent services when referring to roles and ceremony types.

## 8. Typed Data-Access Layer

Domain database operations are exposed through typed service functions in `app/services/`.

The data-access layer provides typed create/read operations for the core domain entities, including:

- cohort creation and lookup
- role creation and lookup
- cohort membership creation and lookup
- sprint creation and lookup
- ceremony creation and lookup
- ceremony-type lookup and creation
- daily progress creation and lookup
- escalation creation and lookup
- Mattermost user identity resolution
- cohort-scoped role resolution
- sprint status lookup

This keeps downstream components from needing to issue raw database queries directly and provides a consistent application-level interface for domain data access.

## 9. Synchronous Database Access Trade-off

The repository's `AGENTS.md` recommends asynchronous database operations. However, the existing `DatabaseService` and domain service patterns use SQLModel's synchronous `Session`, and the current database configuration does not include `asyncpg`.

The implementation therefore retains synchronous database access for the domain data-access layer rather than introducing a broader database-layer migration. This minimizes changes to the existing agent persistence infrastructure and keeps the new services consistent with the current application architecture.

This is an intentional team-level architectural decision for the current implementation. A future refactor could migrate the domain data-access layer to `AsyncSession` and `asyncpg` if the application architecture adopts asynchronous database access consistently.

## 10. Verification

The schema and database changes were verified through the following checks:

- Alembic migrations were applied successfully through the current `head` revision.
- `alembic check` reported no pending upgrade operations.
- The schema verification script `ai-core/scripts/verify_schema.py` confirmed that all required domain and persistence tables exist, the obsolete `person` table is absent, and the required identity, progress, escalation, and ceremony columns are present.
- The schema verification script was executed against the containerized application database to verify the same database environment used by `ai-core`.
- Reference-data seeding completed successfully and is idempotent.
- Pyright completed with `0 errors`.
- Cross-cohort validation is implemented in the daily-progress and escalation service functions.
- Mattermost-to-`ai-core` smoke testing confirmed that the running agent can receive and respond to messages through the configured Mattermost integration.

The verification workflow is integrated into the project's `make verify` target so schema validation can run alongside the existing application verification suites.

## 11. Setup, Seed, Verification, and Rollback

### Setup

Start the SprintFlow services with Docker Compose:

```bash
make up
```

The application database and supporting services are initialized through the project configuration.

### Migrations

Apply all pending migrations:

```bash
MSYS_NO_PATHCONV=1 docker compose exec ai-core /app/.venv/bin/alembic upgrade head
```

Check whether the database schema matches the current models:

```bash
MSYS_NO_PATHCONV=1 docker compose exec ai-core /app/.venv/bin/alembic check
```

### Reference Data

Run the reference-data seed script after the database has been migrated. The seed operation is idempotent and can safely be repeated.

### Schema Verification

Run the schema verification script against the containerized application environment:

```bash
MSYS_NO_PATHCONV=1 docker compose exec -T ai-core /app/.venv/bin/python - < ai-core/scripts/verify_schema.py
```

The script verifies required tables and columns, confirms that the obsolete `person` table is absent, and checks database connectivity.

The schema verification is also included in the project's `make verify` workflow.

### Rollback

Alembic migrations can be rolled back to a previous revision when required. For example:

```bash
MSYS_NO_PATHCONV=1 docker compose exec ai-core /app/.venv/bin/alembic downgrade <revision>
```

After a rollback, the database can be brought back to the latest schema with:

```bash
MSYS_NO_PATHCONV=1 docker compose exec ai-core /app/.venv/bin/alembic upgrade head
```

Rollback is supported by the migration definitions; destructive database-volume resets are not part of the normal migration workflow.

## 12. Trade-offs

The implementation favors a simple relational schema, explicit cohort-scoped relationships, typed service-level domain validation, and minimal changes to the existing agent persistence infrastructure.

The main design decisions were:

- **Unified identity:** `User` is used as the single application identity instead of maintaining a parallel `Person` model. Mattermost-specific identifiers remain optional fields on `User`.
- **Cohort-scoped roles:** roles are assigned through `CohortMembership`, allowing users to hold different roles across cohorts and multiple roles within the same cohort.
- **Domain validation:** semantic relationships such as cohort consistency are validated in the typed data-access layer rather than introducing more complex composite foreign keys.
- **Ceremony representation:** ceremonies use a reusable `CeremonyType` together with scheduling, organizer, agenda, channel, and status fields, providing enough structure for downstream scheduling workflows.
- **Explicit progress and escalation data:** daily progress uses structured fields for completed work, planned work, and blockers, while escalations retain the learner's question, human assignment, lifecycle state, and conversation thread identifiers.
- **Timezone-aware timestamps:** domain timestamps are stored as timezone-aware values to avoid ambiguity when scheduling and comparing events across environments.
- **Status fields:** lifecycle statuses remain application-defined strings rather than PostgreSQL enums, keeping future status changes simpler.
- **Synchronous database access:** the existing synchronous SQLModel session pattern is retained rather than introducing a broader asynchronous database migration.
- **Persistence protection:** existing LangGraph checkpoint and conversation persistence tables were preserved throughout the domain-model changes.

These choices prioritize maintainability, compatibility with the existing application architecture, and clear ownership boundaries while leaving room for future architectural changes if the platform's requirements evolve.
