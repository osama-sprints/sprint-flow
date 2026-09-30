# Knowledge Capture from Resolved Escalations — Technical Report

## Candidate extraction

`knowledge_extraction.discover_candidates()` queries every `EscalationTicket` that is `resolved`, has an `answer`, and has a `raw_human_response` — then excludes anything that already has a row in `knowledge_candidates` (a plain `NOT IN` subquery on `escalation_id`). For each remaining ticket, `_extract_candidate()` sends the model exactly two pieces of information: the learner's original `question` and the reviewer's verbatim `raw_human_response`. Nothing else — no ticket reference, no reviewer identity — reaches the prompt, for the same reason `escalation_closure.py`'s synthesis call is similarly narrow: there's nothing to leak that was never provided.

The model responds in a fixed two-line shape (`AUDIENCE: ...` / `STATEMENT: ...`) or the single word `NONE`. Anything that doesn't parse cleanly into that shape — an unrecognized audience value, a missing statement line, free-form prose instead of the expected format — is treated identically to `NONE`: no candidate is created. This is a deliberate refusal-over-guessing choice, the same principle your Task 2 closure work already established for attribution.

## Anti-generalization safeguards

This is the property the brief calls the hardest part, and it's handled entirely in the prompt, not in code that post-processes the model's output. The system prompt states the failure mode explicitly — *"turning a one-off exception into a blanket rule"* — and gives the brief's own worked example (the regional fibre-cut extension) as a concrete right/wrong pair, so the model has a template for what "narrowing" actually looks like rather than an abstract instruction. The model has three legitimate outputs: a narrowed, conditioned statement; an unconditioned statement (if the resolution genuinely was general); or `NONE` when nothing generalizable exists at all. `test_extraction_narrows_a_one_off_exception` in the test suite exercises exactly this scenario.

One honest limitation: this is a single LLM call with a well-specified prompt, not a structurally-enforced guarantee — there's no code path that can *detect* a bad generalization after the fact and block it. If this proves insufficient in practice, a second verification pass (a separate call asking "does this statement claim more than the original resolution supports?") would be the natural next layer, but wasn't built here given the brief scopes this as prompt-guardrail work.

## Reviewer approval flow

`knowledge_review.list_pending_for_review()` joins every `pending` candidate back to its source `EscalationTicket`, returning the four things the brief requires a reviewer to see before deciding: the proposed `statement`, the proposed `audience`, the escalation's human-facing `ticket_ref`, and the original conversational context (`question` + `raw_human_response`, verbatim).

`approve_candidate()` and `reject_candidate()` are the only two ways a candidate's `status` ever changes. Both:
- Require `require_superadmin()` — the same authorisation primitive Task 3's back-office tooling already uses, so "authorised reviewer" means the same thing here as it does everywhere else admin-level actions are gated in this codebase.
- Refuse (return `ALREADY_DECIDED`, change nothing) if the candidate isn't currently `pending` — a decision happens exactly once, never silently repeated or overwritten.
- Stamp `reviewer_id` and `reviewed_at` at the moment of decision, giving every approved or rejected row a permanent record of who decided and when.

`approve_candidate()` additionally accepts an optional `audience_override`, so a reviewer who disagrees with the model's proposed audience can correct it at the point of approval rather than needing a separate edit step.

## Search isolation and audience enforcement

The zero-leakage invariant is enforced structurally, not by a filter that could be forgotten: **there is exactly one function in the entire codebase that ever writes a knowledge-candidate-derived row into `policy_document_chunks`** — the indexing step inside `approve_candidate()`. `reject_candidate()` and the initial `pending` state created by discovery never call it. A candidate cannot become searchable by any path other than an explicit, authorised approval. `test_reject_never_indexes` proves this directly by asserting the indexing mock is never invoked on the reject path.

Audience enforcement itself is inherited for free: an approved candidate is written into `policy_document_chunks` with its `audience` column set exactly like any other chunk, so `PolicyVectorStore.similarity_search()`'s existing `audience.in_(allowed_audiences)` filter applies to candidate-derived knowledge exactly as it does to ingested documents — no separate filtering logic needed or written.

**Open item, deliberately not resolved in this implementation**: the actual audience string values used here (`"learner"` / `"internal_operator"`) were chosen to match what Task 3's real ingested data and retrieval filter already use — not the brief's own illustrative value (`"internal"`). This was a live discrepancy discovered in the existing codebase (`similarity_search`'s `allowed_audiences` default set doesn't even include `"internal_operator"` for an unauthenticated/`None` audience) and needs a team decision, not a unilateral one, before this is considered final.

## Provenance and idempotency

Two independent mechanisms, at two different layers, both required:

1. **Candidate-level idempotency**: `KnowledgeCandidate.escalation_id` has a database-level `UNIQUE` constraint. `discover_candidates()` uses `INSERT ... ON CONFLICT (escalation_id) DO NOTHING` — even a second discovery pass racing against the first, or a full process restart mid-pass, cannot produce two candidates for the same escalation. This is a constraint, not an application-level check that could have a bug.
2. **Index-level idempotency**: each candidate's vector chunk uses a deterministic id — a hash of `escalation_id` and the statement's own content hash, following the exact same scheme `PolicyChunker` already uses for ingested documents. Re-running indexing for any reason (a retry, a re-approval attempt) lands on the same row via `upsert_chunks()`'s existing `session.get()`-then-update-or-insert logic, never a duplicate.

Provenance is carried in `doc_metadata` on the resulting chunk: `escalation_id`, `candidate_id`, and `reviewer_id` are all present on every candidate-derived row, so a search result can always be traced back to the exact escalation and reviewer decision it came from.

## Verification

`tests/test_knowledge_capture.py` covers, fully mocked (no live database or LLM call):
- Extraction returning `None` for ungeneralizable input, correctly narrowing the brief's own worked example, and refusing rather than guessing on malformed model output (wrong audience value, unparseable shape).
- Authorisation: an unauthorised requester is refused before any read or write happens.
- Both state-transition guards: approving/rejecting an already-decided candidate is refused, not silently repeated.
- **The single most load-bearing test**: `test_reject_never_indexes` — proves the vector store is never touched on the reject path.
- The approved path: status transitions, timestamps are stamped, and indexing is invoked exactly once, after the commit.
- The chunk handed to the vector store carries the correct audience and provenance metadata.
- The deterministic chunk-id function is stable across repeated calls for the same input and differs across escalations.

**Honest limitation, stated rather than glossed over**: the database-level guarantee behind idempotency — the `UNIQUE` constraint actually preventing a concurrent duplicate insert — is not exercised by this test file, since that requires a real Postgres connection under concurrent load. This mirrors an existing, accepted pattern in this repo (see `tests/integration/test_schema_db.py`'s concurrent-insert tests, skipped by default): the constraint's existence is proven by the migration itself, and its concurrent behavior is a live-database integration test, not a mocked unit test.