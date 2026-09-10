# SprintFlow — Grounded Policy Answers and Honest Refusal — Report

New capability: `policy_support`, a fourth specialist route alongside
`learner_support`, `back_office`, and `general`. Code: `app/schemas/graph.py`
(new `CapabilityRoute.POLICY_SUPPORT`), `app/core/langgraph/routing_rules.py`
(new rule, and removal of the overlapping policy keywords from
`learner_support`), `app/services/policy_retrieval.py`,
`app/core/langgraph/nodes.py` (`policy_retrieval_node`),
`app/core/langgraph/specialists.py` (new `Specialist` entry +
`_POLICY_SUPPORT_CONTEXT`). Verification:
`tests/test_policy_retrieval_node.py`.

## retrieval_integration

Retrieval itself (ingestion, chunking, embeddings, vector store) is owned by
the document-ingestion pipeline (`app/services/document_ingestion/vector_store.py`,
`similarity_search()`), agreed as a fixed interface before this task's code
was written specifically so the two pieces could be built in parallel without
guessing at each other's shape:

```python
async def similarity_search(
    query: str,
    audience: Optional[str] = None,   # "learner" | "internal_operator" | None
    top_k: int = 5,
    similarity_threshold: float = 0.0,
) -> List[Dict[str, Any]]
```

returning, per match: `content`, `similarity_score`, `document_id`,
`source_file_path`, `section_title`, `page_number`, `audience`.

`app/services/policy_retrieval.py` is a thin layer on top of this — it does
not re-implement retrieval, it turns a raw score list into one of four
outcomes (`grounded` / `weak_match` / `no_match` / `error`) that the rest of
the system branches on. `policy_retrieval_node` (in `core/langgraph/nodes.py`)
is a **new node type**: unlike the other specialists, it runs *before* any
model call, as a deterministic gate.

## grounding

Grounding is enforced at two layers, not one:

1. **Structural**: the model is only ever shown the exact chunks
   `similarity_search` returned above the threshold — it receives no separate
   "go look things up" tool, so it physically cannot introduce a document the
   retrieval layer didn't surface. This is the same capability-separation
   pattern already used for `learner_support`/`back_office` (a specialist's
   node binds only what it's allowed to use) applied to *information* rather
   than *tools*.
2. **Prompt-level**: `_POLICY_SUPPORT_CONTEXT` in `specialists.py` instructs
   the model to answer "strictly using ONLY the provided document snippets,"
   and to state plainly when a part of the question isn't covered rather than
   filling the gap from general knowledge.

Layer 1 is the real guarantee (no LLM call, no chance to hallucinate a
citation, on the refusal path — see `refusal_behaviour`); layer 2 is the
defense for the part of the flow where the model does run, to stop it padding
a grounded answer with unsupported extras.

## citation

Every retrieved chunk is rendered as
`[Source: <document_id>, §<section_title>, p.<page_number>] <content>`
before it reaches the model, and the prompt requires the same format to
follow every claim in the reply. All three fields are drawn directly from the
retrieval payload — never invented — so a reviewer can open `document_id` at
`page_number` and check `section_title` against what the model actually
said. `source_file_path` is carried in the retrieved dict for logging/audit
but is not currently surfaced in the chat reply itself, since a raw
filesystem path is not what a learner needs inline; it is available to add if
a full click-through citation is required later.

## refusal_behaviour

Refusal is a **graph-level branch, not a model decision**. When
`get_grounded_answer_or_refusal` returns anything other than `grounded`
(`weak_match`, `no_match`, or `error`), `policy_retrieval_node` routes
straight to `END` without ever invoking the model — the refusal message is
Norhan's `open_escalation(...).message`, relayed exactly, not
model-generated. This means an unsupported query costs zero model calls and
can never be phrased as a plausible-sounding guess, because no generation
happens on that path at all.

## audience_restriction

`audience` is derived once, in code, from `current_requester` (the same
`ContextVar` — never the message text or any model output — that every other
authorisation check in this codebase already trusts):

- `requester.is_superadmin` or `requester.has_any_cohort_authority()` (an
  existing method on `RequesterContext`, already used by `back_office`
  routing) → `audience=None`, unrestricted.
- Otherwise, including when no requester is bound at all → `audience="learner"`,
  the most restrictive option. Failing closed (defaulting to the restrictive
  case) was a deliberate choice: an unresolved identity should never
  accidentally grant operator-document access.

The filter is applied *before* the vector search runs
(`similarity_search(..., audience=audience)`), per the ingestion team's
confirmation that the audience filter is a `WHERE` clause evaluated before
distances are computed — an internal-only chunk is excluded from the
candidate set itself, not merely hidden after the fact.

## weak_match_handling

`get_grounded_answer_or_refusal` compares the best returned
`similarity_score` against a threshold (default `0.75`); anything below it is
`weak_match` rather than `grounded`, and takes the exact same refusal +
escalation path as `no_match` (see `refusal_behaviour`). Weak-match and
no-match are kept as distinct string outcomes internally (for future log
analysis — e.g. tuning the threshold) even though they currently produce
identical downstream behaviour, since collapsing them into one case now would
make it harder to tell "nothing existed" from "something existed but wasn't
confident enough" later.

## escalation_link

Every refusal path calls `open_escalation(question=<extracted text>,
ticket_type=EscalationType.OPS, requester=<current requester>)` —
`EscalationType.OPS` is passed unconditionally and directly in code, per the
escalation team's explicit requirement that the type must never be inferred
from the question text; the `policy_support` node structurally knows every
refusal it triggers is a policy/operational matter, so the type is a
constant, not a classification. `open_escalation` resolves cohort/learner/
sprint/channel context automatically from `requester`; this code does not
guess at or duplicate those fields. `result.message` — already a complete,
learner-facing sentence from the escalation team's side — is relayed as-is
as the assistant's reply, wrapped in an `AIMessage` so it is attributed to
the bot rather than the requester.

## verification

`tests/test_policy_retrieval_node.py` covers every property named in the
task brief, using `current_requester` set to real `RequesterContext`
instances (not a stubbed state field) so the tests exercise the actual
authorisation pattern rather than a shortcut:

- **Text extraction** (4 tests): `BaseMessage`, `dict`, plain `str`, and empty
  input all resolve to a plain string, never a leaked message object —
  regression coverage for the original bug where the raw message object was
  passed straight to retrieval and escalation.
- **Audience / role restriction** (4 tests): a learner is restricted to
  `audience="learner"`; a tech lead, a superadmin, and (failing closed) an
  unbound requester are checked explicitly, covering both directions of the
  boundary this task's success standard names as the target property.
- **Grounded answers with citations** (1 test): asserts the built context
  string contains the real document id, section title, page number, *and*
  the actual retrieved content — this is the regression test for the earlier
  bug where citations were rendered with the content field missing.
- **Refusal + escalation, parametrised over `no_match` / `weak_match` /
  `error`** (1 test, 3 cases): confirms all three unsupported-query states
  escalate identically, with `ticket_type=EscalationType.OPS` and the
  original question text (not a message object), and that the relayed reply
  is the escalation service's own message.
- **Safe degradation** (1 test): an empty message list still escalates
  cleanly rather than raising.

All tests run against the node function directly with mocked retrieval/
escalation calls — no live stack or vector store required, matching the
verification approach taken for the Sprint 1 supervisor (fast, deterministic,
no external dependency for the properties being checked). End-to-end
behaviour (a real learner message reaching a real grounded or refused reply
in Mattermost) should still be spot-checked manually against the live stack
before submission.
