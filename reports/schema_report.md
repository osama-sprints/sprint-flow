# SprintFlow Data Model and Migrations Report

## 1. Overview

This report documents the relational schema, migrations, reference data, data-access layer, architectural decisions, and verification performed for the SprintFlow domain model.

## 2. Domain Schema

The schema introduces the following domain entities:

- `Person` — maps Mattermost identities to SprintFlow users through a unique Mattermost user ID and handle.
- `Cohort` — represents a SprintFlow cohort and its lifecycle.
- `Role` — stores reusable role definitions.
- `CohortMembership` — associates a person with a cohort and assigns a cohort-scoped role.
- `Sprint` — represents a time-boxed sprint belonging to a cohort.
- `CeremonyType` — stores reusable ceremony types.
- `Ceremony` — represents scheduled ceremonies within a cohort.
- `DailyProgress` — records daily progress for a cohort member within a sprint.
- `Escalation` — tracks open/resolved/ignored escalations and optionally correlates them with conversations and sprints.

Common `created_at` and `updated_at` fields are provided through `DomainBase`.

## 3. Cohort-Scoped Roles

Roles are intentionally assigned through `CohortMembership` rather than directly to `Person`.

This allows the same person to hold different roles in different cohorts. A unique constraint on `(person_id, cohort_id)` prevents duplicate membership in the same cohort while still allowing membership in multiple cohorts.

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

Two reversible Alembic migrations were added:

1. `ae9ba88585bc_add_domain_models.py` creates the domain tables and relationships.
2. `a86e08b1fcb3_add_status_defaults.py` applies the required non-null constraints for fields with model defaults.

The existing agent conversation/checkpoint tables were not modified.

Migration rollback was verified by downgrading from `a86e08b1fcb3` to `ae9ba88585bc`, followed by upgrading back to the head revision.

## 7. Reference Data

Reference data for roles and ceremony types is provided through an idempotent seed script.

The seed script was executed successfully more than once. Re-running it completed without errors or duplicate-record failures.

## 8. Typed Data-Access Layer

Domain operations are exposed through typed service functions in `app/services/`.

The services provide operations for:

- cohort creation and lookup
- cohort membership creation and lookup
- sprint creation and lookup
- ceremony creation
- daily progress creation
- escalation creation
- Mattermost identity resolution
- cohort-scoped role resolution

This keeps downstream components from needing to issue raw database queries directly.

## 9. Synchronous Database Access Trade-off

The repository's `AGENTS.md` recommends asynchronous database operations. However, the existing `DatabaseService` and domain service patterns use SQLModel's synchronous `Session`, and the current domain database path does not include `asyncpg`.

The implementation therefore retains the existing synchronous session pattern for the new domain services rather than introducing a broader database-layer migration. This minimizes changes to the existing agent persistence infrastructure and keeps the new services consistent with the current implementation.

A future refactor could migrate the domain data-access layer to `AsyncSession` and `asyncpg` if the application architecture adopts that approach consistently.

## 10. Verification

The following verification was performed:

- Alembic downgrade from the latest domain migration completed successfully.
- Alembic upgrade to `head` completed successfully.
- `alembic current` confirmed `a86e08b1fcb3 (head)`.
- Reference-data seeding completed successfully on repeated execution.
- Pyright completed with `0 errors`.
- Cross-cohort validation was implemented in the daily-progress and escalation service functions.

## 11. Trade-offs

The implementation favors a simple relational schema, explicit cohort-scoped relationships, typed service-level domain validation, and minimal changes to the existing agent persistence layer.

Composite foreign keys and PostgreSQL enums were deliberately avoided because their additional complexity was not necessary for the current requirements.
