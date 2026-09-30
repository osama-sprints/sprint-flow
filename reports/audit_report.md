# SprintFlow — Software Quality & Production Readiness Audit

**Audited revision:** branch `feature/standup-collection`, HEAD `9038f10`
**Audit date:** 2026-09-21
**Environment:** local Docker stack (`sprintflow-ai-core`, `sprintflow-mattermost` 11.7.10, `sprintflow-postgres` pg16 + pgvector), `APP_ENV=development`
**Method:** static review + live runtime probes against the running stack + existing pytest suites. No production application code was modified.

---

## 1. Executive summary

SprintFlow's **core conversational loop is healthy and well engineered**: the webhook → agent → bot-reply path, DM handling over WebSocket, message routing, thread continuity, standup collection (37/37), ceremony/calendar management (18/18), thread isolation, admin-document ingestion, webhook authentication, and rate limiting all verified PASS live.

However, the product ships with a **mid-refactor defect cluster** left over from the `cohorts → channels` migration. The headline problems:

1. **The announcements feature cannot work at all** — no DB table, a `cohort_id`/`channel_id` signature mismatch that throws `TypeError` at the tool boundary, and a delivery stub that never posts anything.
2. **A hardcoded superadmin backdoor** in `RequesterContext.is_admin`.
3. **The verification harness is largely stale** — the standard probes (`verify_escalation`, `verify_scheduling`, `verify_authorisation`, `verify_onboarding_journey`) crash against current code.
4. **20 unit tests and the integration suite fail**, mostly because tests were not carried through the refactor.

A full remediation plan is in §7.

---

## 2. Verification evidence table

| Check | Tool | Result |
|---|---|---|
| Mattermost REST + bot posting | `scripts/test_mattermost.py` | **PASS** (auth 200, channel 200, post 201) |
| Public-channel routing (5 cases) | `scripts/verify_routing.py` | **PASS** (5/5) |
| Thread isolation | `scripts/verify_isolation.py` | **PASS** |
| Proactive standups | `scripts/verify_standups.py` | **PASS** (37/37) |
| Ceremony/calendar (create/reschedule/cancel/outage/disabled) | `scripts/verify_calendar_integration.py` | **PASS** (18/18) |
| Admin agent (team ops, non-admin refusal) | `scripts/verify_admin_agent.py` | **PASS** (4/4) |
| Live DM end-to-end (user → bot → reply) | custom probe | **PASS** (reply + auto-onboarding welcome) |
| Ingestion pipeline (28 chunks, pgvector, embedding) | `ai-core/scripts/verify_ingestion.py` | Ingestion **PASS**; final idempotency assert fails on cold start — **harness bug only** (product idempotent: 28→28) |
| Webhook auth / rate limit / bot-loop guard | direct HTTP probes | **PASS** (401 bad token, 400 malformed, 200 valid, 429 at limit) |
| Authorisation boundary (learner vs superadmin) | custom probe | **PASS** (learner refused, nothing written; superadmin allowed) |
| Migrations | `alembic current/heads` | **PASS** (single head `d32cf4519a86`, DB at head) |
| Escalation verification probe | `scripts/verify_escalation.py` | **FAIL** — `relation "cohorts" does not exist` |
| Scheduling verification probe | `scripts/verify_scheduling.py` | **FAIL** — `channels.create_channel` missing |
| Authorisation verification probe | `scripts/verify_authorisation.py` | **FAIL** — cohort-era probe import (`app.services.domain.cohorts`) |
| Onboarding verification probe | `scripts/verify_onboarding_journey.py` | probe **FAIL** (stale), live welcome DM **PASS** |
| Unit test suite | `uv run pytest tests/` | **511 passed / 20 failed / 54 skipped** |
| Integration suite (throwaway DB `sprintflow_audit`, migrated to head) | `uv run pytest tests/integration/` | **~18 passed / ~26 failed + ~10 errors** (order-dependent) |

---

## 3. Critical findings (bugs)

### BUG-001 — Announcements feature is completely non-functional
Severity: **High**. The feature is wired into the agent graph and tool list, but every layer is broken:

- **No database table.** The `Announcement` model exists (`ai-core/app/models/announcement.py`) but no Alembic migration creates it; the DB head is `d32cf4519a86` and the live `sprintflow` DB has no `announcements` table. Any persistence raises `ProgrammingError: relation "announcements" does not exist` (reproduced live via `confirm_announcement_tool`).
- **Uncallable tool signature.** `prepare_announcement_preview_tool(cohort_id=…, …)` (`ai-core/app/core/langgraph/tools/back_office.py:34`) forwards `cohort_id` into service functions `resolve_announcement_channel` / `resolve_recipients_by_role` / `create_announcement_preview` that take `channel_id` (`ai-core/app/services/announcements.py`). Reproduced live: `TypeError: list_channel_roles() got multiple values for argument 'channel_id'`.
- **Delivery is a stub.** `send_to_mattermost(...)` (`ai-core/app/services/announcements.py:22`) returns `"mm_post_<timestamp>"` without calling Mattermost, so even a confirmed announcement never posts.
- **Audit attribution fakeable.** `created_by_user_id: int = 1` default in the tool (`back_office.py:40`) is model-suppliable and defaults to the first user row rather than the actual requester.

### BUG-002 — Hardcoded superadmin backdoor
Severity: **High**. `ai-core/app/core/requester.py:64`:

```
if self.mattermost_user_id == "106328600742274448745":
    return True
```

`is_admin` is the legacy name the workspace-administration tools rely on. Any Mattermost account with that user-id is treated as a superadmin with no email-verified/ADMIN_EMAILS trust chain, no matter what role it holds. The allowlist path elsewhere (`app/services/identity.py:71`) requires email on `ADMIN_EMAILS` **and** `email_verified` or `system_admin`, so this literal is a straight bypass of a deliberately hardened check. It should be removed and, if needed, replaced with an explicit allowlist entry plus the same trust check.

### BUG-003 — Verification harness is stale (regression coverage is broken)
Severity: **Medium**. AGENTS.md mandates probe-based verification, but most probes did not survive the `cohorts → channels` refactor:

- `scripts/_escalation_probe.py` SQL targets the dropped `cohorts` table → `ProgrammingError: relation "cohorts" does not exist`.
- `scripts/_authorisation_probe.py` imports `from app.services.domain import cohorts as cohort_repo` → `ImportError` (forbidden import per AGENTS.md), then expects `back_office.create_cohort`.
- `scripts/_scheduling_probe.py` calls `channels.create_channel` / `upsert_membership` which do not exist in `app/services/domain/channels.py`.
- `scripts/_onboarding_probe.py` and `scripts/_cohort_lookup_probe.py` are cohort-era as well.
- Host verifiers that subprocess probes from `/scripts/…` paths (`verify_scheduling`, `verify_escalation`, `verify_standups`, `verify_calendar_integration`) raise `FileNotFoundError` because the root `scripts/` dir is not mounted into the container; verifiers that call `http://localhost:8065` fail with `ConnectionRefused` when they run inside the container rather than on the host.
- Consequence: **esc escalation, scheduling/authorisation regression coverage is currently red**, even though the underlying services were shown by my probes and the passing verifiers to work.

### BUG-004 — Schema inconsistencies from the refactor
Severity: **Medium**.
- The bare `channels` table exists (`id integer`, `name`, `mattermost_channel_id`, `team_id`) but has **no ORM model and is never referenced**; `sprints.channel_id` stores Mattermost string ids (`character varying`) and is not an FK. Join/cleanup SQL that mixes them fails: reproduced in the integration suite via `operator does not exist: character varying = integer`.
- Integration cleanup SQL references a `channel_memberships` table that does not exist (the real table is `channel_roles`), causing cleanup failures that leave test rows behind.
- `test_schema.py` fails because the `announcements` model declares an FK to the dropped `cohorts` table and uses a naive `status_changed_at` datetime.

### BUG-005 — Dead, broken cohort-era helpers in the authorisation module
Severity: **Medium (hygiene)**. `ai-core/app/services/authorisation.py:23-51` ships `require_cohort_authority` and `require_active_cohort` that call `sprint_repo.get_sprint_by_id` (does not exist; the repo only has `get_sprint`) and reference `AuthorisationDecision`/`User` before their definitions in the same file. They are not called by the running application (only by stale tests) but are importable and guaranteed to crash. They should be deleted along with the failing tests.

### BUG-006 — Missing `trio` dependency
Severity: **Low**. Two test failures are pure infrastructure: `test_policy_retrieval_service[trio]` and `test_policy_retrieval_node_security_and_escalation[trio]` raise `ModuleNotFoundError: no module named 'trio'` (`anyio` backend). `trio` is not declared in `ai-core/pyproject.toml` / `uv.lock`. Add it (or drop the trio backend parametrization).

### BUG-007 — Stale unit tests (20 failures)
Severity: **Medium**. Categories:

| Group | Failure mode |
|---|---|
| `test_announcement_channel_resolution.py` (4) | Mock nonexistent `require_cohort_authority` / `require_active_cohort` on `app.services.announcements` |
| `test_recipient_resolution.py` (6) | Mock nonexistent `sprint_repo` / `identity_repo` attributes on the announcements module; `cohort_id` semantics |
| `test_announcement_preview.py` (1) | `cohort_id` kwarg vs current `channel_id` signature |
| `test_schema.py` (2) | FK → dropped `cohorts` table; naive datetime |
| `test_complete_policy_tasks.py` (3) | Routing target drift (`policy_support` vs `policy_support_llm_node`) + due-message control-flow + trio |
| `test_routing_rules.py` (1) | Route plan differs from current graph |
| `test_supervisor.py` (2) | Multi-intent decision shape moved on |
| `test_scenario_integration.py` (1) | Escalation status expectation drifted |

### BUG-008 — `verify_ingestion.py` idempotency check assumes a pre-seeded table
Severity: **Low (harness)**. `ai-core/scripts/verify_ingestion.py:77-82` asserts `chunk_count()` is unchanged across two `ingest_all()` calls. On a clean DB the first call legitimately goes 0 → 28 and the verifier exits 1. The product path is genuinely idempotent (verified 28 → 28); the verifier should seed first, or assert on a *second* consecutive run.

---

## 4. Notable risks & observations

- **RISK-010 — Dual-transport duplicate replies.** A public-channel message that begins with a webhook trigger word and also mentions the bot is claimed by both transports (`_should_handle` rule 2, `mattermost_ws.py:439`, fires before rule 3). Deduplication relies on the in-process `claim_mattermost_event` cache, and the stack logs `redis_not_available` at boot — i.e. **no shared cache**. Single instance is safe; scaling the ai-core service horizontally without a shared claim cache will double-answer some messages.
- **RISK-011 — Attachment trigger over-permissive and unmonitored.** `is_ingestion_request()` returns `bool(file_ids)` — *any* attachment triggers the ingestion path; the `_ADMIN_PROMPTS` keyword regex (`mattermost_ingestion.py:17`) is defined but never used. `_is_authorized_admin` also grants ingestion to **any `channel_admin`** in any channel; combined with a single global `learner` audience, a channel admin from one unrelated channel can inject arbitrary PDF/DOCX into the global QA corpus every learner can query. Recommend: require the keyword prompt plus system_admin-only (or per-channel admin allowlist + probe checks).
- **RISK-012 — Empty webhook token opens the endpoint.** `mattermost.py:144-148`: when `MATTERMOST_OUTGOING_WEBHOOK_TOKEN` is unset, the endpoint logs a warning and **accepts all requests**. Currently configured, but a redeploy that forgets the env var silently disables auth.
- **RISK-013 — Dev environment drives a production LLM gateway.** Embedding calls proxy through the production `management.sprints.ai` LiteLLM endpoint and consume spend/budget on a shared key (~$2.04 seen mid-session). Dev/stale probes in loops can bill against a shared production budget. Consider a per-environment key/budget cap.
- **OBS — Announcement SQL injection surface is clean** (parameterized queries), webhook HMAC compare is constant-time, Pydantic validation is used, and the LLM never directly controls SQL. Authorisation decisions are made from stored rows via `current_requester` (ContextVar), which is the right design.

---

## 5. What works well (verified live)

- **Transport layer**: webhook ACK-then-answer pattern with correct bot-loop guard on both transports, constant-time token comparison, 400/401 paths, rate limiting (`429` observed at limit).
- **Conversation agent**: real DM end-to-end reply; auto-onboarding welcome to a brand-new user; public-channel routing 5/5 including thread continuity and "mention mid-sentence";
- **Standups**: 37/37 — idempotent prompt creation, local-timezone dispatch instants, dedupe, missed/late handling, dispatcher delivers row state machine.
- **Calendar**: 18/18 — provider create/reschedule/cancel, outage resilience with bounded retries (3), disabled-mode fallback.
- **Admin agent**: 4/4 team operations + non-admin refusal.
- **Admission control**: superadmin-only `assign_role`/`open_sprint` verified against the live DB (learner refused, zero rows written).
- **Ingestion**: pgvector active, chunking + embedding end-to-end, idempotent re-ingestion.
- **DB hygiene**: single Alembic head, migrations apply cleanly on a throwaway DB.

---

## 6. Determinism / consistency (test count drift)

The integration run produced different totals across two runs (26 failed + 10 errors vs 19 failed + 10 errors), indicating **test-order dependence** (shared DB state, premature cleanup failures). Any fix must make the integration suite hermetic (per-test cleanups that succeed, unique prefixes, and no dependence on leftover rows).

---

## 7. Recommended action plan (source fixes, prioritized)

| # | Fix | Files |
|---|---|---|
| 1 | Remove the hardcoded admin id; route `is_admin` through the stored `is_superadmin` / allowlist+trust check | `ai-core/app/core/requester.py:64` |
| 2 | Land an `announcements` migration (FK → `channels` with `ON DELETE …`, tz-aware `status_changed_at`) or delete the feature | `ai-core/app/models/announcement.py`, `ai-core/alembic/versions/` |
| 3 | Re-key announcement tools to `channel_id`, guard with `@guarded_tool` + `require_channel_authority`, drive `created_by_user_id` from `current_requester` | `ai-core/app/core/langgraph/tools/back_office.py`, `ai-core/app/services/announcements.py` |
| 4 | Implement `send_to_mattermost` (call `mattermost_client.create_post`) or remove the stub | `ai-core/app/services/announcements.py:22` |
| 5 | Delete `require_cohort_authority` / `require_active_cohort` (and their stale tests) | `ai-core/app/services/authorisation.py:23-51` |
| 6 | Rewrite cohort-era probes to the channels schema; make host verifiers mount-independent | `scripts/_escalation_probe.py`, `scripts/_authorisation_probe.py`, `scripts/_scheduling_probe.py`, `scripts/_onboarding_probe.py`, `scripts/verify_scheduling.py`, `scripts/verify_escalation.py` |
| 7 | Fix `test_announcement_*`, `test_recipient_resolution`, `test_schema` to current APIs; align routing/supervisor/intent expectations | `ai-core/tests/` |
| 8 | Add `trio` (or drop trio backend parametrization) | `ai-core/pyproject.toml` |
| 9 | Make integration tests hermetic; fix cleanup SQL (`channel_roles`, `sprints.channel_id` join by Mattermost id) | `ai-core/tests/integration/` |
| 10 | Seed-before-assert in `verify_ingestion.py`; document that cold-start grows the chunk count | `ai-core/scripts/verify_ingestion.py:77` |
| 11 | Harden attachment ingestion: keyword prompt OR system-admin-only; remove unused `_ADMIN_PROMPTS` ambiguity | `ai-core/app/services/mattermost_ingestion.py` |
| 12 | Fail closed at the webhook when the token is unset | `ai-core/app/api/v1/mattermost.py:144-148` |
| 13 | Add shared claim-cache backing (Redis/Valkey) before horizontal scaling | `ai-core/app/services/conversation.py`, `ai-core/app/core/cache.py` |

---

## 8. Secrets & sensitivity note

`.env` contains live Mattermost bot tokens, an outgoing-webhook shared secret, an admin password, an LLM proxy key and a JWT secret. All are **redacted throughout this report** and must not appear in any issue/commit. `AGENTS.md` (repository root) is accurate about the container Python workflow (`uv run python`, `PYTHONPATH=.`) but is out of date about the probes' health (see BUG-003).

---

*Report produced by live audit. Every claim above was either reproduced on the running stack or is a direct static observation with a file:line reference.*