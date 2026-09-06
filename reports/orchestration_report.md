# Orchestration Report — Sprint 1 / AI Eng 3: Multi-Agent Orchestration and Delegation

**Date:** 2026-09-03
**Branch:** `dev` (task s1e3)
**Scope:** `ai-core/app/core/langgraph/` (`supervisor.py`, `routing_rules.py`, `routing_examples.py`,
`specialists.py`, `graph.py`), `ai-core/app/schemas/graph.py`, `ai-core/app/core/prompts/system.md`,
`ai-core/tests/test_routing_rules.py`, `test_supervisor.py`, `test_specialists.py`,
`tests/integration/test_orchestration_db.py`, `scripts/verify_orchestration.py`, `scripts/_routing_probe.py`.

Every number in this report was measured by a command quoted next to it. Numbers that can only be
produced against the live Docker stack come from `python3 scripts/verify_orchestration.py` run at
integration on 2026-09-03 (the *Live results* table under *Cost control*).

## Routing design

### The decision and where it is made

A message reaches the agent through the existing conversation layer (`app/services/conversation.py`),
which already decides *whether* to answer (mention/thread/DM rules, loop guard). That decision is
untouched. The supervisor makes a different decision — *which specialised capability answers an
accepted message* — and it is the first node of the LangGraph graph:

```
before (foundation)        chat ──> tool_call ──> chat ──> END           (every tool bound, one prompt)

after                      supervisor ──(state.route)──> learner_support <──> learner_support_tools
                                                         back_office     <──> back_office_tools
                                                         chat            <──> tool_call
                           (any specialist ──> next specialist in route_plan ──> … ──> END)
```

`supervisor_node` (`app/core/langgraph/supervisor.py`) is synchronous and I/O-free. It reads the
requester from the `current_requester` ContextVar — the same object the tools trust, populated from
the Mattermost event's user id and stored data, never from message text — and the last human message,
and calls `detect_intents(text, requester)` from `routing_rules.py`. It writes five defaulted fields
into `GraphState`: `route`, `route_plan`, `route_confidence`, `matched_rule`, `is_multi_intent` (plain
strings, never enums, so a renamed route can never break loading an old checkpoint). A conditional edge
then sends the turn to the specialist named by `route`.

### How `detect_intents` decides (regex only)

1. The text is normalised (whitespace, curly apostrophes) and tested against every rule in
   `ROUTING_RULES`, in order of consequence: mutations first (`back_office_cohort`, `back_office_role`,
   `back_office_sprint`, `back_office_schedule`), then reads (`cohort_directory`, `learner_calendar` — open to
   any member and routed to learner support; the read tools refuse non-members in code),
   then `learner_support`, then `workspace_admin` (the pre-Sprint-1 Mattermost tools, on the general
   route).
2. **Role, not only words.** Mutation rules are gated on
   `requester.has_any_cohort_authority()` (superadmin, or an active `tech_lead`/`scrum_master`
   membership somewhere). Without authority the match is kept but redirected to `learner_support` with
   the rule name suffixed `_denied_role` — so `create a cohort` from a learner is
   `learner_support / back_office_cohort_denied_role`, while the same sentence from a scrum master is
   `back_office / back_office_cohort`. The learner-support specialist has no mutating tools at all, so
   the denial is structural; the tools re-check authority in code regardless of routing.
3. **Questions are never mutations.** A single-clause message that opens with an interrogative
   (`when/what/which/who/is/are/do/does/did/has/have/should…`, after skipping greetings and mentions)
   skips every mutation rule: `when should we schedule the retro?` is a calendar read. Polite orders
   (`can you open sprint 2?`) are not interrogative openers and still route to the back office. A second
   clause (`… and …`, `then`, `also`, `;`, a `?` followed by more text) re-enables mutations so
   `what's on this week and open sprint 2` is decomposed.
4. **The `schedule` noun.** The scheduling verb must not follow a determiner
   (`the/a/our/my/for/of…`), so `what's the schedule for the retro` describes and `schedule the retro`
   orders.
5. Every distinct route that matched is returned in rule order, de-duplicated by route. Two or more
   routes form a multi-step plan (capped by `ROUTING_MAX_ROUTES_PER_TURN`, default 2). Nothing matched
   means `general` with `matched_rule=general_fallback` — the behaviour that worked before.

### Vocabulary and evidence

The vocabulary is specified by `app/core/langgraph/routing_examples.py`: **85 labelled sentences**
(create/open/archive/deactivate cohort; assign/promote/demote/change role in several phrasings; open/
start/kick off/close/mark sprint; schedule/book/set up/put/reschedule/move/cancel/amend/postpone each
ceremony type including `q&a`, `office hours`, `demo`; agenda updates; calendar reads; learner questions
on deadline/policy/blockers/"who do I ask"; directory reads; workspace admin; greetings/thanks/random;
multi-intent; and 8 deliberately ambiguous cases). Each entry states the requester kind
(`learner`/`authority`/`admin`/`anonymous`), the expected route plan and the expected `matched_rule`.

- `tests/test_routing_rules.py` asserts all 85, that every rule and route has at least one labelled
  sentence, that the Sprint 1 vocabulary words appear in the corpus, and the latency budget.
- `scripts/_routing_probe.py probe` re-runs the same corpus on the deployed code and prints the latency
  distribution (see *Cost control*).

Measured on the throwaway environment (`cd ai-core && APP_ENV=test .venv/bin/python -m pytest -q tests/`):
**388 passed, 43 skipped in 6.0 s** (the skips are the database-backed `tests/integration/`, which need
`SPRINTFLOW_INTEGRATION_DB=1`); probe: **15/15 checks passed**, 85/85 sentences routed as labelled.

### Where this design struggles (known limits)

- English-only, regex-only. A paraphrase with none of the listed verbs falls to `general`, which is
  safe (the general specialist answers with the old tool set) but not specialised. New phrasings are
  added as rules plus labelled sentences, not by retraining anything.
- Plan order follows rule consequence (mutation before read), not word order. This is deliberate —
  reads then reflect the mutation — but `tell me what's on and then open sprint 2` runs the sprint first.
- A message needing three capabilities is capped at two (`ROUTING_MAX_ROUTES_PER_TURN=2`); the rest is
  dropped from the plan and the person has to ask again. Raising the setting needs no code change.
- The imperative/question heuristics are heuristics: `what about scheduling a retro for Friday?` is
  question-shaped and becomes a calendar read; `make sure the students know` matches the role rule for
  an authority (and the back-office specialist then says it cannot do that). Both are recorded as hard
  cases and would be the first candidates for a model fallback *only if* the measured fallback rate ever
  justified the cost (today it does not — see *Cost control*).
- Authority for routing comes from the `cohort_roles` snapshot taken at identity sync; authorisation
  itself re-reads the database in the tools, so a stale snapshot can at worst mis-route, never
  mis-authorise.

## Specialisations

Three specialists, each a pair of graph nodes bound to ONE tool group (`app/core/langgraph/specialists.py`):

| Route | Model node | Executor node | Tool group (`tools/__init__.py`) | May do |
| --- | --- | --- | --- | --- |
| `learner_support` | `learner_support` | `learner_support_tools` | `LEARNER_SUPPORT_TOOLS` — `ask_human`, web search, `list_ceremonies`, `list_cohorts`, `list_cohort_members` (all read-only; each refuses non-members in code) | answer, clarify, read the calendar and the roster |
| `back_office` | `back_office` | `back_office_tools` | `BACK_OFFICE_TOOLS` — `ask_human`, `create_cohort`, `assign_role`, `open_sprint`, `list_cohorts`, `list_cohort_members`, `schedule_ceremony`, `amend_ceremony`, `list_ceremonies` | cohort/role/sprint/ceremony administration |
| `general` | `chat` | `tool_call` | `GENERAL_TOOLS` — exactly the pre-Sprint-1 set: web search, `ask_human`, `mattermost_*` | what the bot did before the sprint |

The constraint is structural, in two layers, not a prompt instruction:

1. **Binding.** The specialist node calls `llm_service.call(messages, tools=TOOL_GROUPS[group])`, which
   binds exactly that subset for the call. The model literally cannot see another group's tools.
2. **Execution.** The executor node builds `{t.name: t for t in TOOL_GROUPS[group]}` and, for any
   name outside it (a hallucination, or a tool it saw in an earlier turn under another route), returns a
   `ToolMessage` `[VALIDATION_ERROR] The tool 'X' is not available here … it was not run.` and never
   executes it.

`graph.py` has no per-specialist code: `build_graph()` iterates `SPECIALISTS` and generates both nodes
from the table. The prompt context per specialist also lives in the table; the learner-support one
explicitly forbids claiming that an administrative action was done and tells the model to point the
person to their tech lead / scrum master.

**Containment demonstrated, not asserted** (`tests/test_specialists.py`, fake LLM + `MemorySaver`):

- `test_learner_route_never_executes_a_back_office_tool`: a learner sends `create a cohort`; the fake
  model emits a `create_cohort` tool call anyway. Path visited:
  `supervisor → learner_support → learner_support_tools → learner_support`; the recording fake
  `create_cohort` was executed **0** times; the `ToolMessage` starts with `[VALIDATION_ERROR]`; the
  model's first call saw only `['ask_human', 'list_ceremonies']`.
- `test_general_route_cannot_reach_back_office_tools_either`: the same on the fallback route.
- `test_back_office_route_executes_its_own_tool`: the positive control — the same tool call from an
  admin on the back-office route executes once.
- `scripts/_routing_probe.py` asserts on the deployed registry that `learner_support` contains none of
  `create_cohort, assign_role, open_sprint, schedule_ceremony, amend_ceremony, mattermost_*`, that
  `back_office` contains neither web search nor `mattermost_*`, and that the two groups differ.

### How to add a specialisation

1. Add a value to `CapabilityRoute` in `app/schemas/graph.py` (the state stores it as a string).
2. Add a tool group to `TOOL_GROUPS` in `app/core/langgraph/tools/__init__.py` (tools are thin wrappers
   over services; privileged ones use `@guarded_tool` and authorise in code).
3. Add a `Specialist(route, node_name, tools_node_name, tool_group, prompt_context)` entry to
   `SPECIALISTS` in `specialists.py`. `build_graph()` creates its two nodes and its conditional edge;
   nothing in `graph.py` changes.
4. Add `Rule`s to `ROUTING_RULES` in `routing_rules.py` (set `mutation=True` and
   `requires_cohort_authority=True` for orders that change things) and labelled sentences to
   `routing_examples.py` — the test `test_corpus_is_large_enough_and_covers_every_rule_and_route` fails
   until every rule has one.
5. Restart `ai-core` (tools bind at startup). Run `python3 scripts/verify_orchestration.py`.

## Multi-step handling

Decomposition is explicit and cheap: it *is* the routing result. `detect_intents` returns every route
a message calls for; the supervisor stores the first as `route` and the rest as `route_plan`.

Execution: a specialist that finishes without tool calls checks `route_plan`; if non-empty it pops the
first route into `route` and hands over with `Command(goto=<that specialist's node>)`. The next
specialist's prompt says that earlier parts of the same request were already handled, that their
results are the assistant messages after the person's last message, and that it must write **ONE final
reply that covers its part and summarises the earlier results** — because the conversation layer posts
only the last assistant message (`answer_and_reply` takes the last `assistant` entry), the person sees
one coherent answer and never the internal steps. The first specialist's prompt says the opposite: do
only your part, state its outcome, do not comment on the rest. (`describe_route(route, plan,
continuation=…)` in `specialists.py`; continuation is detected from the messages themselves, so no extra
state field is needed.)

Demonstrated in `test_multi_intent_runs_back_office_then_learner_support_with_one_final_reply`:
`open sprint 2 for Backend-01 and tell me when the retro is` from a scrum master visits
`supervisor → back_office → learner_support → learner_support_tools → learner_support`; the three model
calls saw `[ask_human, create_cohort]`, `[ask_human, list_ceremonies]`, `[ask_human, list_ceremonies]`
respectively; the first prompt contains "the following will handle the rest: learner_support", the
later ones "Earlier parts of this same request were already handled"; the final state has
`route=learner_support`, `route_plan=[]`, `is_multi_intent=True`, and the last message is the composed
reply. The same scenario round-trips through the real Postgres checkpointer in
`tests/integration/test_orchestration_db.py::test_routing_fields_round_trip_through_postgres`.

Live (integrator, `scripts/verify_orchestration.py` step 2): DM
`open sprint Verify-<stamp> for cohort Verify-Orch-<stamp> and tell me what's scheduled`, count bot
posts after the trigger, wait 20 s after the first, assert exactly one reply mentioning the sprint and
the calendar, then confirm the sprint row exists via the probe.

Two back-office actions in one sentence (`create cohort X and make @bob its scrum master`) are one
route: the back-office specialist calls both tools itself. Decomposition is only across capabilities.

## Cost control

**No model is consulted for routing.** `supervisor_node` has no code path that calls the LLM;
`sprintflow_routing_model_calls_total` exists precisely so that this can be checked on `/metrics`
rather than believed. The common path adds one synchronous regex pass plus one extra LangGraph
super-step per turn (the supervisor node and its checkpoint write). The latency figures below
measure the regex pass only; the super-step cost is one Postgres checkpoint round trip, which is
the same order as the existing per-node writes and is invisible next to the 4–14 s model calls
measured live.

Measured — method: run `detect_intents` over the 85-sentence labelled corpus repeatedly until ≥ 1000
timed calls, `time.perf_counter()` around each call, sorted percentiles
(`cd ai-core && .venv/bin/python ../scripts/_routing_probe.py probe`, Python 3.13, this workstation):

| sample | n | p50 | p95 | p99 | max | mean |
| --- | --- | --- | --- | --- | --- | --- |
| labelled corpus, probe run (2588f47) | 1020 | 0.078 ms | 0.155 ms | 0.181 ms | 0.189 ms | 0.083 ms |
| labelled corpus, ad-hoc run before the probe existed | 1700 | 0.027 ms | 0.053 ms | — | 0.095 ms | — |

`tests/test_routing_rules.py::test_routing_latency_p95_under_budget` asserts p95 < 2 ms over ≥ 1000
calls on every test run; the probe asserts the same on the deployed code and prints the distribution.

Escalation rate (fraction of routing decisions that needed a model): **0 / 1020** decisions in the probe
run and **0 / every** graph turn in the unit and integration tests, read from
`sprintflow_routing_model_calls_total` (asserted in `test_multi_intent_decision_shape_and_metrics` and
in the probe). Live sample: step 1 of the verifier reads `/metrics` inside the container before and
after N = 6 mixed-intent DMs and prints the per-route deltas and the model-call delta.

What the sprint does add per turn is not routing cost but specialist cost: a multi-intent message costs
one extra model call per additional route (at most one extra with the default cap), and a paused
confirmation costs the same as before. Prompt size grew from 2470 characters (foundation shape) to
3277–3942 characters depending on the specialist (measured with `load_system_prompt(...)`), which is
what surfaced the `Message` 3000-character cap described under *Design decisions*.

**Live results** — `python3 scripts/verify_orchestration.py` on the Compose stack at commit `2588f47`,
2026-09-03 (Gemini through the LiteLLM proxy, real Mattermost; the seven DMs were: next standup,
list cohorts, assign a scrum master, leave policy, thanks, what's on this week, blocked on docker):

| measurement | value |
| --- | --- |
| routing decisions delta per route over N=7 DMs (`learner_support` / `back_office` / `general`) | 5 / 1 / 1 |
| `sprintflow_routing_model_calls_total` delta (escalation rate) | 0 / 7 |
| in-container probe latency (n, p50, p95, p99, max) | 1020, 0.078 ms, 0.155 ms, 0.181 ms, 0.189 ms |
| end-to-end time to first reply per turn (min / median / max over 12 turns) | 4.0 s / 8.0 s / 14.1 s |
| multi-intent DM: number of bot replies (expect 1) | 1 (sprint opened, calendar mentioned, sprint row present) |
| in-flight: schedule → confirmation question → `yes` → ceremony row | 1 reply each turn; ceremony #9 created, instant timezone-aware |
| pre-existing suites folded in | `verify_routing.py`, `verify_threading.py`, `verify_isolation.py` all exit 0 |

End-to-end latency is dominated by the model (4–14 s per turn); the supervisor's share is under
0.2 ms per decision, i.e. below 0.01 % of a turn. The `back_office` row is the one mutating DM in the
batch (`assign … as scrum master`), which is also what proves a mutation routes away from learner
support when the requester does hold authority.

## Fallback behaviour

An unclear message is answered, never dropped or refused:

- `detect_intents` always returns at least one result; nothing matched means `general` with
  `matched_rule=general_fallback` and confidence 0.0.
- `specialist_for(route)` returns the general specialist for a missing or unknown route (an old
  checkpoint, a renamed route), so the conditional edge can never dangle.
- The general specialist is the pre-Sprint-1 behaviour: `chat`/`tool_call` with exactly the old tool set.

Demonstrated: `test_unclear_message_ends_in_chat_with_a_reply` (`asdfgh ???` visits
`supervisor → chat`, `route=general`, `matched_rule=general_fallback`, a reply is produced) and, in the
corpus, `hello there`, `thanks!`, `tell me a joke`, `what's the weather like in Cairo today?`. Live:
verifier step 3 sends `hello there` and asserts exactly one reply.

A learner asking for an admin action is not "unclear" and is not refused by the router either: it goes
to learner support, whose prompt says plainly who can do it and whose tool group cannot do it.

## In-flight conversations

`ask_human`/`interrupt()` remains the only confirmation mechanism. What the sprint had to guarantee is
that a conversation paused on a question resumes *where it paused*, not at the supervisor:

- `get_response` resumes with `Command(resume=…)` only when `pending_interrupt(state)` finds a task that
  actually carries an interrupt (a node that merely raised leaves `state.next` set but no interrupt, and
  starts a fresh turn instead — root cause 1 of `reports/ceremony_bot_failure_report.md`). On resume
  LangGraph re-enters the paused task; the supervisor is not on that path, so `route` and the counters
  are untouched.
- The pre-Sprint-1 node names `chat` and `tool_call` are kept for the general specialist, so a
  checkpoint written before this sprint and paused inside `tool_call` resumes into the new graph's
  `tool_call`.

Demonstrated:

- `test_legacy_interrupt_in_tool_call_resumes_without_rerunning_supervisor`: a graph with the OLD
  state schema and OLD topology (`chat → tool_call`) pauses on `ask_human`; the NEW graph is built on the
  same checkpointer and resumed with `Command(resume="yes")`. Path: `tool_call → chat`; the supervisor
  counter is unchanged; `route` stays unset; the `ToolMessage` carries `yes`.
- `test_specialist_interrupt_resumes_in_its_own_tools_node`: a back-office confirmation pauses in
  `back_office_tools`; resume visits `back_office_tools → back_office`, `route` still `back_office`, no
  new routing decision.
- `test_old_shaped_checkpoint_loads_and_routes`: a checkpoint holding only `messages` +
  `long_term_memory` loads into the new `GraphState`, routes, and keeps its history (4 messages, full
  history sent to the model).
- `test_stale_failed_node_is_not_an_interrupt`: a node that raised leaves `next=('chat',)` and no
  interrupt, so the next message starts a fresh turn.
- The same three scenarios pass against the real `AsyncPostgresSaver`
  (`tests/integration/test_orchestration_db.py`, 3 passed against the throwaway Postgres 16).

Live (verifier step 5): DM `schedule the standup for tomorrow at 9am UTC for cohort <X>` (routes to the
back office; the scheduling tool asks to confirm), reply `yes`, then assert through the probe that a
`daily_standup` ceremony now exists for the cohort with a timezone-aware instant.

## Observability

Every routing decision is reviewable after the fact without replaying the conversation:

- **Log** `routing_decision_made` (structlog, one per turn) with `session_id`, `route`, `route_plan`,
  `matched_rule`, `route_confidence`, `is_multi_intent`, `requester` (handle + roles, no secrets),
  `input_length`, `latency_ms`, `model_call=False`. Grepping `matched_rule=general_fallback` lists the
  messages the table did not understand; `_denied_role` lists learners asking for admin actions.
- **Log** `llm_response_generated` now carries `specialist`, `route`, `tool_count`, the `tool_calls`
  the model asked for, `continuation` and `remaining_plan`; `routing_plan_advanced` marks each hand-over;
  `tool_call_refused_out_of_group` records every refused tool name with its route.
- **Metrics** on `/metrics`: `sprintflow_routing_decisions_total{route,matched_rule,multi_intent}`,
  `sprintflow_routing_latency_seconds` (histogram, buckets from 0.1 ms), and
  `sprintflow_routing_model_calls_total{route}` — the escalation numerator, always 0 today.
- **State**: `route`, `route_plan`, `route_confidence`, `matched_rule`, `is_multi_intent` are persisted
  in the checkpoint, so `scripts/_dump_session.py`-style inspection of a thread shows how its last turn
  was routed.

The verifier reads `/metrics` from inside the container (`docker compose exec -T ai-core python -c
"urllib…"`) before and after the batch and prints the deltas, which is exactly how an operator would
sample routing quality in production.

## Design decisions

- **Rules, not a model, and not similarity.** The product's busiest surface is chat; a classifier call
  per message would multiply cost and latency for a decision that a few dozen regexes make in ~0.03 ms.
  Similarity search would need embeddings per message (a network call) for marginal gain over a
  vocabulary this small and this formal. The measured fallback rate decides whether that ever changes;
  today no message needs the expensive path.
- **Three specialists, not two or five.** Learner support and back office are the product's two
  segregated fronts. `general` is not a fourth capability: it is the untouched pre-sprint behaviour
  kept as the fallback and as the home of the existing workspace-admin tools, and it keeps the old
  node names so old checkpoints resume. Splitting scheduling from back office (as the review suggested
  naming) was not done: the same people (cohort authority) do both, and one more specialist would only
  add a hand-over on `open sprint 2 and schedule the planning`.
- **Decomposition is the routing result.** No planner, no model: a message with two capabilities is
  two rule matches. This is cheap, deterministic and testable, and the composition rule (last specialist
  writes the one reply, earlier replies stay in state) needs no extra state field.
- **Authority in routing is a hint; authority in tools is the boundary.** Routing uses the identity
  snapshot to keep learners on the learner route; the tools re-read the database. A public-channel
  superadmin therefore *routes* to the back office and the workspace tools still refuse non-DM use —
  the old `never_grants_admin` test asserted the opposite of its name and was replaced by
  `test_public_channel_learner_admin_phrase_routes_to_learner_support` and
  `test_routing_is_not_the_authorisation_boundary`.
- **State additions are additive and defaulted; strings not enums.** Checkpoints written before the
  sprint load unchanged (tested through `MemorySaver` and Postgres).
- **Foundation fix (minimal, in `app/utils/graph.py`).** `prepare_messages` wrapped the system prompt in
  `Message`, whose `content` carries the 3000-character *user-input* cap. Routing context plus
  long-term memory exceeds it (3277–3942 characters measured), and the foundation shape (2470) would
  have crossed it with ~500 characters of memory, failing every turn with a validation error. The system
  message is now built with `Message.model_construct`, keeping the cap for people's messages.
- **Naming.** `CapabilityRoute` with `LEARNER_SUPPORT` / `BACK_OFFICE` / `GENERAL`; metrics labels and
  state values use the same strings.
- **Verification lives in `scripts/`**, follows the established stdlib PASS/FAIL pattern, pipes the
  probe over stdin (never `docker compose cp`), and folds the existing `verify_routing.py`,
  `verify_threading.py` and `verify_isolation.py` in so "what worked before still works" is a measured
  exit code, not a claim.

### Relation to `reports/ceremony_bot_failure_report.md`

Its root causes 1–3 are fixed by this sprint's graph layer: (1) `pending_interrupt` distinguishes a
real interrupt from a failed pending node (`test_stale_failed_node_is_not_an_interrupt`); (2) tools
wear `@guarded_tool` so expected failures become `ToolMessage` results instead of stuck checkpoints;
(3) the executor retry policy retries only transient transport errors, never validation, authorisation
or integrity errors. A "Resolution (2026-09-03)" section was prepended to that report.

### Commands

```bash
cd ai-core
.venv/bin/ruff check app/ alembic/ scripts/ tests/ && .venv/bin/ruff format --check app/ alembic/ scripts/ tests/
APP_ENV=test uv run --frozen --no-sync pyright                       # 0 errors
APP_ENV=test .venv/bin/python -m pytest -q tests/                    # 388 passed, 43 skipped (integration needs SPRINTFLOW_INTEGRATION_DB=1)
SPRINTFLOW_INTEGRATION_DB=1 APP_ENV=test .venv/bin/python -m pytest -q tests/integration   # 3 passed (Postgres)
.venv/bin/python ../scripts/_routing_probe.py probe                  # 15/15, latency distribution
cd .. && python3 scripts/verify_orchestration.py                     # live stack (integrator)
```
