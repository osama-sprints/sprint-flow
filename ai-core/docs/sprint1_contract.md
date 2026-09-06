# SprintFlow Sprint 1 — shared engineering contract

The names, shapes and rules the five Sprint 1 tasks share. This is the document the task
reports refer to; where the shipped code and this text differed, the text was updated to
match the code at integration (2026-09-03).

Everything below is implemented on `dev`. Read `ai-core/AGENTS.md` and the conventions in
this document before extending it.

## 0. Repository facts you must respect

- Python 3.13, FastAPI + LangGraph 1.x, SQLModel over SQLAlchemy 2.0, psycopg 3.
- `ai-core/app/core/config.py` is hand-rolled `os.getenv` in `Settings.__init__`. Append
  new settings at the END under a comment naming your task. Update `.env.example` (and
  `.env` if present) for every new variable.
- structlog only: event names `lowercase_with_underscores`, variables as kwargs, never
  f-strings in log calls. `logger.exception` where a traceback matters.
- All imports at the top of the file. Google-style docstrings with Args/Returns/Raises.
  Guard clauses, early returns, happy path last. Type hints on every signature.
- Ruff config: line length 119, rules E/F/B/ERA/D (google docstrings). `ruff check app/`
  and `ruff format --check app/` must pass. Pyright (standard) must pass on `app/`.
- Tools are bound to the model at startup: a new tool needs an ai-core restart.
- The tool executor calls `tool.ainvoke(args)` with no config. Per-request context travels
  in the `current_requester` ContextVar (`app/core/requester.py`). NEVER accept requester
  identity as a tool argument.
- `ask_human` / `interrupt()` is THE confirmation mechanism (LangGraph interrupt +
  `Command(resume=...)`). Do not build another.
- Tools must never raise (except `GraphInterrupt`, which must propagate). Use
  `@guarded_tool` from `app/core/langgraph/tools/results.py`.
- No test framework was in the repo originally; the established verification pattern is
  host-side, stdlib-only `scripts/verify_*.py` that print one `PASS`/`FAIL` line per
  assertion and exit non-zero on failure, exercising in-container code by piping a helper
  over stdin: `docker compose exec -T ai-core /app/.venv/bin/python - < scripts/_x_probe.py`.
  Unit tests (pytest) live in `ai-core/tests/` and may only test pure logic (no DB, no
  network) — run with `ai-core/.venv/bin/python -m pytest` on the host or
  `docker compose exec -T ai-core /app/.venv/bin/python -m pytest` in the container.
- Assert silence where silence is correct (refusal creates nothing, replay does not
  re-greet, learner route cannot reach admin tools).

## 1. Vocabulary (final — do not introduce synonyms)

| Concept | Model | Table | Notes |
| --- | --- | --- | --- |
| Person | `User` | `users` | one identity concept; `mattermost_user_id` is the join |
| Role lookup | `Role` | `roles` | `key` lowercase machine key, `label` display |
| Cohort | `Cohort` | `cohorts` | `is_active` is the kill switch |
| Membership | `CohortMembership` | `cohort_memberships` | ONE role per (user, cohort) |
| Sprint | `Sprint` | `sprints` | `start_date`/`end_date` calendar dates |
| Ceremony type | `CeremonyType` | `ceremony_types` | seeded lookup |
| Ceremony | `Ceremony` | `ceremonies` | `scheduled_at` tz-aware instant |
| Amendment | `CeremonyAmendment` | `ceremony_amendments` | audit trail |
| Standup | `DailyStandup` | `daily_standups` | explicit what/next/blockers |
| Escalation | `EscalationTicket` | `escalation_tickets` | `ticket_ref` = `ESC-000001` |
| Onboarding | `OnboardingStep` | `onboarding_steps` | outbox of scheduled deliveries |

Machine keys (`app/models/enums.py`):

- `RoleKey`: `learner`, `tech_lead`, `ops_support`, `scrum_master`.
  `COHORT_ADMIN_ROLES = {tech_lead, scrum_master}` may administer a cohort.
- `CeremonyTypeKey`: `daily_standup`, `sprint_planning`, `sprint_review`, `retrospective`,
  `open_qa`. `CEREMONY_TYPE_ALIASES` maps human words ("standup", "retro", "q&a", "demo"…).
- `MembershipStatus`: `active`, `inactive`.
- `SprintStatus`: `planned`, `active`, `completed`.
- `CeremonyStatus`: `scheduled`, `cancelled`, `completed`.
- `EscalationType`: `tech`, `ops`. `EscalationStatus`: `open`, `waiting_human`, `resolved`.
- `OnboardingStepKind`: `welcome`, `orientation`, `follow_up`.
  `OnboardingStepStatus`: `pending`, `sent`, `failed`, `halted`.

Identity fields: `mattermost_user_id` (Mattermost id), `user_id` (internal integer id),
`cohort_id`, `sprint_id`, `role_id`, `ceremony_type_id`, `organizer_id`, `learner_id`,
`assigned_human_id`, `learner_thread_id`, `human_dm_thread_id`, `scheduled_at`,
`duration_minutes`. Internal ids are integers everywhere. Tools accept human references
(`cohort` = name or numeric id, `person` = `@username` or email) and resolve them explicitly.

Superadmin: `users.is_superadmin`, synced from `ADMIN_EMAILS` on every identity sync.
Platform-level actions (create cohort, Mattermost workspace tools) require superadmin.
Cohort-level actions require superadmin OR an active membership in that cohort with a role
in `COHORT_ADMIN_ROLES`. Reads of a cohort's calendar/members require superadmin OR any
active membership in that cohort.

## 2. Requester context — `app/core/requester.py`

```python
@dataclass(frozen=True)
class RequesterContext:
    mattermost_user_id: str
    username: str = ""
    email: str | None = None
    channel_id: str = ""
    channel_type: str = ""          # O public, P private, D direct, G group
    user_id: int | None = None      # users.id after sync; None if the sync failed
    is_superadmin: bool = False
    timezone: str | None = None     # IANA zone from the Mattermost profile
    cohort_roles: Mapping[int, str] # cohort_id -> role key, ACTIVE memberships only (routing hint)
    @property is_admin -> is_superadmin   # legacy name used by mattermost_admin.py
    def has_cohort_authority(cohort_id) -> bool
    def has_any_cohort_authority() -> bool
current_requester: ContextVar[RequesterContext | None]
```

`app/services/conversation.py` builds it via `identity.resolve_requester(message)` BEFORE the
graph runs. `cohort_roles` is a hint for routing only; authorisation re-reads the database.

## 3. Authorisation — `app/services/authorisation.py` (single implementation)

```python
REFUSAL_MESSAGE = "Refused: You do not have permission to execute this action."
def require_requester() -> RequesterContext   # raises AuthorisationRefused when no requester is bound
class AuthorisationRefused(Exception)   # .reason for logs; str(exc) == REFUSAL_MESSAGE
class ValidationFailed(Exception)       # str(exc) is the user-facing validation sentence
class AuthorisationDecision(NamedTuple): allowed: bool; reason: str; role_key: str | None
async def require_requester_user(requester) -> User            # refuses when not synced
async def require_superadmin(requester) -> User
async def require_cohort_authority(requester, cohort_id) -> AuthorisationDecision
async def require_cohort_membership(requester, cohort_id) -> AuthorisationDecision
async def decide_cohort_authority(requester, cohort_id, allowed_roles=COHORT_ADMIN_ROLES)
```

All decisions read `users` and `cohort_memberships` from the database at decision time.
Every privileged service function calls one of these BEFORE any mutation. Refusal and
validation are different exceptions and different result codes.

## 4. Data-access layer — `app/services/domain/` (async, typed)

All functions are `async def`, open their own `AsyncSession` via
`app.services.database.session_scope()` unless a `session` kwarg is supplied (for
transactional composition), and return SQLModel rows (columns fully loaded,
`expire_on_commit=False`, no relationships) or plain values. Never write SQL in tools.

- `identity.py`: `get_user(user_id)`, `get_user_by_mattermost_id(mm_id)`,
  `get_user_by_username(username)`, `get_user_by_email(email)`,
  `upsert_mattermost_user(mattermost_user_id, username, email, display_name, timezone,
  is_superadmin) -> User` (INSERT … ON CONFLICT (mattermost_user_id) DO UPDATE).
- `cohorts.py`: `create_cohort(name, mattermost_team_id=None, ...) -> Cohort`,
  `get_cohort(cohort_id)`, `get_cohort_by_name(name)` (case-insensitive),
  `resolve_cohort(reference: str)` (numeric id or name), `list_cohorts(active_only=False)`,
  `set_cohort_active(cohort_id, is_active)`, `get_role_by_key(key)`, `list_roles()`,
  `get_membership(user_id, cohort_id)`, `list_memberships_for_user(user_id, active_only=True)`,
  `list_cohort_members(cohort_id)` -> list[(CohortMembership, User, Role)],
  `upsert_membership(user_id, cohort_id, role_id, assigned_by_id) -> MembershipChange(membership, created, previous_role_id, reactivated)`,
  `get_role_for_user_in_cohort(user_id, cohort_id) -> Role | None`.
- `sprints.py`: `create_sprint(cohort_id, name, start_date, end_date, opened_by_id, status)`,
  `get_sprint(sprint_id)`, `get_sprint_by_name(cohort_id, name)`, `list_sprints(cohort_id)`,
  `set_sprint_status(sprint_id, status)`, `find_overlapping_sprints(cohort_id, start, end, exclude_id)`.
- `ceremonies.py`: `get_ceremony_type_by_key(key)`, `list_ceremony_types()`,
  `create_ceremony(...) -> Ceremony`, `get_ceremony(ceremony_id)`,
  `list_ceremonies(cohort_id, include_past, include_cancelled, now)`,
  `find_overlapping_ceremonies(cohort_id, start, duration_minutes, exclude_id)`,
  `update_ceremony(ceremony_id, amended_by_id, changes: dict, reason) -> Ceremony` (writes one
  `CeremonyAmendment` per changed field in the same transaction),
  `list_amendments(ceremony_id)`.
- `standups.py`: `upsert_daily_standup(...)` (one entry per learner per day), `get_daily_standup`, `list_daily_standups(sprint_id, learner_id=None, log_date=None)`.
- `escalations.py`: `create_escalation_ticket(...)` (allocates `ticket_ref`),
  `get_escalation_ticket(ticket_ref)`, `list_escalation_tickets(cohort_id, status=None)`,
  `set_escalation_status(ticket_ref, status, answer=None, human_dm_channel_id=None, human_dm_thread_id=None)`.
- `onboarding.py`: `enqueue_step(user_id, cohort_id, step_kind, due_at) -> (OnboardingStep, created)`
  (ON CONFLICT DO NOTHING on the unique key), `list_steps_for_user`,
  `claim_due_steps(worker_id, lease_seconds, limit, now, user_ids=None) -> list[OnboardingStep]`
  (SELECT … FOR UPDATE SKIP LOCKED + set claimed_at/claimed_by in one transaction),
  `mark_step_sent(step_id, mattermost_post_id, role_key, claimed_by=None)`,
  `mark_step_failed(step_id, error, next_attempt_at, max_attempts, claimed_by=None)` (both settle only
  for the worker still holding the claim), `release_claim`, `halt_steps_for_cohort(cohort_id)`.
- `reference_data.py`: `seed_reference_data()` idempotent; also called at startup.

## 5. Tool contract — `app/core/langgraph/tools/`

Every tool returns a string `"[CODE] human sentence"`. Codes:
`COHORT_CREATED`, `COHORT_ALREADY_EXISTS`, `ROLE_ASSIGNED`, `ROLE_ALREADY_ASSIGNED`,
`ROLE_CHANGED`, `SPRINT_OPENED`, `SPRINT_ALREADY_OPEN`, `CEREMONY_SCHEDULED`,
`CEREMONY_AMENDED`, `CEREMONY_CANCELLED`, `CEREMONY_CONFLICT`, `TIME_CLARIFICATION_REQUIRED`,
`CONFIRMATION_DECLINED`, `AUTHORISATION_REFUSED`, `VALIDATION_ERROR`, `SYSTEM_ERROR`, `OK`.
Helpers in `tools/results.py`: `tool_result(code, message)`, `@guarded_tool` (maps
`AuthorisationRefused` -> AUTHORISATION_REFUSED with REFUSAL_MESSAGE, `ValidationFailed` ->
VALIDATION_ERROR, any other Exception -> SYSTEM_ERROR with a readable sentence and
`logger.exception`; re-raises `GraphInterrupt`).

Tool names and signatures (exact):

Back office — `tools/back_office.py`, service `app/services/back_office.py`:
- `create_cohort(name: str, mattermost_team: str | None = None)` — superadmin. Idempotent by
  case-insensitive name.
- `assign_role(person: str, role: str, cohort: str)` — superadmin or cohort authority.
  `person` is `@username` or email (resolved via Mattermost REST then synced into `users`).
  `role` accepts key, label or alias. One role per (user, cohort): same role -> ROLE_ALREADY_ASSIGNED,
  different role -> ROLE_CHANGED (previous role named); an inactive membership re-assigned its role ->
  ROLE_ASSIGNED ("re-added"). Calls
  `onboarding.on_role_assigned(user_id, cohort_id)` after commit (no-op if onboarding disabled).
- `open_sprint(cohort: str, sprint_name: str, start_date: str | None = None, end_date: str | None = None)`
  — cohort authority. ISO dates; defaults today and today + `SPRINT_DEFAULT_LENGTH_DAYS`.
  Idempotent by (cohort, name). Overlap with another non-completed sprint -> VALIDATION_ERROR.
- `list_cohorts()` — superadmin sees all; others see their own memberships.
- `list_cohort_members(cohort: str)` — cohort membership required.

Scheduling — `tools/ceremonies.py`, services
`app/services/time_interpretation.py` and `app/services/ceremony_scheduling.py`:
- `schedule_ceremony(cohort: str, ceremony_type: str, time_expression: str, agenda: str | None = None, duration_minutes: int | None = None, sprint_name: str | None = None)`
  — cohort authority; interprets time in the requester's zone; ambiguity ->
  TIME_CLARIFICATION_REQUIRED with a question; conflict -> CEREMONY_CONFLICT; otherwise
  `interrupt()` with the absolute instant (user zone + UTC) and persist ONLY on an affirmative
  reply; declined -> CONFIRMATION_DECLINED and zero rows.
- `amend_ceremony(ceremony_id: int, new_time_expression: str | None = None, new_agenda: str | None = None, cancel: bool = False, reason: str | None = None)`
  — cohort authority of the ceremony's cohort; time change confirmed via `interrupt()`;
  past ceremonies: time cannot change and cannot be cancelled, agenda/notes may.
- `list_ceremonies(cohort: str, include_past: bool = False, include_cancelled: bool = False)`
  — any member of the cohort.

Existing (unchanged names): `ask_human`, `duckduckgo_search`, `mattermost_find_or_create_team`,
`mattermost_add_user_to_team`, `mattermost_send_welcome_dm`.

Registry `tools/__init__.py`: exports `tools` (all) and `TOOL_GROUPS: dict[str, list[BaseTool]]`
keyed `learner_support` (ask_human, web search, list_ceremonies, list_cohorts, list_cohort_members —
read-only), `back_office` (ask_human + the five back-office tools + the three ceremony tools) and
`general` (the pre-Sprint-1 set: web search, ask_human, the three Mattermost workspace tools).

## 6. Supervisor — `app/core/langgraph/`

- Routes (`app/schemas/graph.py` `CapabilityRoute`): `learner_support`, `back_office`, `general`.
  GraphState gains defaulted fields only: `route: str | None`, `route_plan: list[str]`,
  `route_confidence: float | None`, `matched_rule: str | None`, `is_multi_intent: bool`.
  Store plain strings, not enums, so old checkpoints and future renames always load.
- `routing_rules.py`: regex table; `detect_intents(text, requester) -> list[RoutingResult]`
  (rule name, route, confidence). Mutation rules require `requester.has_any_cohort_authority()`
  or superadmin; otherwise the result is `learner_support` with `matched_rule="<rule>_denied_role"`
  (e.g. `back_office_cohort_denied_role`). Directory and calendar reads route to `learner_support`
  for everyone; the read tools refuse non-members in code.
  Cover Sprint 1 vocabulary: create/open cohort, assign/promote role, open/start sprint,
  schedule/reschedule/cancel/amend standup/planning/review/retro/q&a, agenda, what's scheduled/
  when is the next…, my role/cohort, assignment/deadline/policy/blocked.
- `supervisor.py`: `supervisor_node(state, config)` — no model call; reads `current_requester`;
  emits log `routing_decision_made` (session_id, route, route_plan, matched_rule, route_confidence,
  is_multi_intent, requester, latency_ms, input_length, model_call) and Prometheus `sprintflow_routing_decisions_total`,
  `sprintflow_routing_latency_seconds`, `sprintflow_routing_model_calls_total` (always 0 today —
  reported as the escalation rate).
- `specialists.py`: `SPECIALISTS: dict[route, Specialist(route, node_name, tools_node_name, tool_group, prompt_context)]`.
  Graph nodes: `supervisor` -> conditional -> `learner_support` | `back_office` | `chat`.
  `chat` and `tool_call` KEEP their names (pre-Sprint-1 checkpoints paused at `tool_call` still
  resume). Each specialist has its own executor node (`learner_support_tools`, `back_office_tools`,
  `tool_call`) that only executes tools in its own group; an unknown/out-of-group tool call gets a
  ToolMessage error, never execution. Multi-intent: `route_plan` executed in order; the last
  specialist is told prior steps already ran and must compose ONE reply.
- `LLMService.call(messages, tools=...)` binds the given subset per call.
- `graph.get_response` resumes with `Command(resume=...)` ONLY when the saved tasks carry a real
  interrupt; a failed pending node starts a fresh turn. A confirming tool interrupts with a structured
  payload `{"question": str, "scheduled_at": iso | None}`; the person sees only the question and the
  conversation layer echoes the payload back as `{"reply": text, "interrupt": payload}` so the tool
  can verify the replayed proposal matches what was confirmed. Plain-string interrupts (`ask_human`)
  receive the plain reply. Tool calls in one turn execute sequentially so a confirmation resumes the
  call it belongs to.

## 7. Onboarding — `app/services/onboarding.py`, `app/workers/onboarding_dispatcher.py`

- Arrival (`new_user`) -> `identity.upsert_mattermost_user` + `onboarding.start_journey(user)`:
  enqueue `welcome` (due now) and `follow_up` (due now + `ONBOARDING_FOLLOW_UP_DELAY_HOURS`)
  with `cohort_id=NULL`; both ON CONFLICT DO NOTHING; wake the dispatcher. Nothing is sent in the
  event handler.
- `on_role_assigned(user_id, cohort_id)`: enqueue `orientation` (due now) for (user, cohort) if the
  welcome for that user was already sent without a role; if the welcome is still pending, the
  welcome itself carries the orientation and records `orientation` as sent for that cohort.
- Dispatcher: single asyncio task per process; loop every `ONBOARDING_POLL_INTERVAL_SECONDS` or on
  wake-up; `claim_due_steps` (SKIP LOCKED + lease) -> resolve role/cohort at delivery time -> skip
  (leave pending, log `onboarding_step_halted_inactive_cohort`) when the step's cohort, or every
  cohort the user belongs to, is inactive -> send DM with the REST client (session closed, no DB
  session held across network I/O) -> `mark_step_sent` on success; on failure
  `mark_step_failed` with exponential backoff; after `ONBOARDING_MAX_ATTEMPTS` -> `failed`.
- Content in `app/core/prompts/onboarding/*.md`, materially different per role
  (learner / tech_lead / ops_support / scrum_master / no role yet), in the assistant's voice.
- Bot never reacts to its own DMs (existing `is_own_post` + `from_bot`).

## 8. Migrations, seeding, startup

- Revision `0001_sprintflow_domain` creates every table above, and revision `0002_sprints_name_ci`
  (the current head) adds the functional unique index `ux_sprints_cohort_name_lower`; `0001`
  seeds `roles` and `ceremony_types` idempotently, and drops the unused template tables
  (`user`, `session`, `thread`) if present. `include_object` in `alembic/env.py` excludes the
  LangGraph checkpointer tables and mem0 tables. Downgrade drops only domain tables.
- `scripts/docker-entrypoint.sh` runs `alembic upgrade head` before uvicorn when
  `AI_CORE_MIGRATE_ON_START=true` (default). `lifespan` seeds reference data (try/except, logs
  and continues) and starts the onboarding dispatcher. `/health` reports `domain_schema` and
  returns 503 when `cohorts` is missing.
- Make targets: `migrate`, `migrate-downgrade`, `seed`, `test`, `lint`, `verify` (chain).

## 9. Reports — `reports/*.md`

Sections exactly as required by the task JSON, only claiming behaviour the code demonstrates,
with reproducible commands and measured numbers where the task asks for them.
