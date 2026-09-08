# Escalation — Handing a Question to a Human

## What this covers

The *send* half of escalation: a learner asks something the agent cannot
ground an answer for, and the agent needs to quietly get that question in
front of the right human, without dragging anyone into the visible channel,
and leave behind a record the closure half (a separate task) can pick up.

Entry point: `app.services.escalation.open_escalation(question)`, exposed to
the learner-support specialist as the `escalate_to_human` tool
(`app/core/langgraph/tools/escalation.py`).

## Reused as-is

- `app/models/escalation_ticket.py` — the `EscalationTicket` model already
  carried every field this needed (both thread ids, `ticket_type`, the
  `open` → `waiting_human` → `resolved` lifecycle).
- `app/services/domain/escalations.py` — `create_escalation_ticket` and
  `set_escalation_status` are used unchanged.
- `app/services/domain/cohorts.py` — `list_cohort_members` resolves the role
  holder; already ordered by `joined_at`, so ties resolve deterministically.
- `app/services/mattermost.py` — `create_direct_channel` + `create_post` open
  and populate the private DM.
- `app/core/langgraph/tools/results.py` — the existing `guarded_tool` /
  `tool_result` / `ResultCode` convention every tool in this repo follows.

## Added

- `Cohort.get_cohort_by_channel_id` (`app/services/domain/cohorts.py`) —
  nothing previously resolved "which cohort is this channel", needed to go
  from an inbound message to a cohort row.
- `RequesterContext.learner_thread_id` (`app/core/requester.py`, threaded
  through `app/services/identity.py::resolve_requester` and set in
  `app/services/conversation.py::answer_and_reply`) — mirrors the same
  `root_id or post_id` rule `_deliver()` already uses to pick a reply target,
  so the ticket records exactly where a future answer must be posted.
- `app/services/escalation.py` — the orchestrator described above.
- `app/core/langgraph/tools/escalation.py` — the tool, added to
  `LEARNER_SUPPORT_TOOLS`.
- `get_open_escalation_ticket_for_learner_thread` (`app/services/domain/escalations.py`) — the idempotency check.
- Migration `0003_escalation_open_thread_unique` — the idempotency race guard.

## choosing_the_human

Two related decisions, confirmed before writing code:

**Which role gets the ticket.** The role is resolved strictly from the stored
cohort-role mapping (`cohort_memberships` joined to `roles`) for a *fixed*
role per ticket type — never from the message. `ROLE_FOR_TICKET_TYPE` maps
`tech → tech_lead` and `ops → ops_support`. Routing never depends on message
content, sprint topic, or any other prompt-derived signal — only on which
cohort the message arrived in (via the channel) and the static ticket type.

**Whether to split tech vs ops at all.** The refusal signal this task
integrates with (a specialist failing to ground an answer) carries no
category — it is just "I don't know." Classifying it would mean guessing a
category from the question text, which is exactly the kind of message-driven
routing this task is required to avoid. Rather than adding a text classifier
whose output would silently decide which human gets contacted, every
escalation defaults to a single role: `DEFAULT_TICKET_TYPE = EscalationType.TECH`
(routes to `tech_lead`). The split is still fully supported in the data model
and in `open_escalation(ticket_type=...)` — a future caller that structurally
knows it is asking on behalf of an operational flow (not by reading the
message) can pass `EscalationType.OPS` explicitly. Nothing about today's
integration needs to.

## idempotency

Re-running against the same thread — a retried webhook delivery, or the
model calling the tool twice for one turn — must not open a second ticket or
send a second DM.

Two layers, matching the pattern this repo already uses for the same class of
problem in `back_office.create_cohort` / migration `0002_sprints_name_ci`:

1. **Check-then-act.** Before touching the cohort, role, or Mattermost at all,
   `open_escalation` looks for a non-resolved ticket already covering this
   learner thread (`get_open_escalation_ticket_for_learner_thread`). If one
   exists, nothing new is created or sent — the result describes that
   ticket's actual current state (`ESCALATION_ALREADY_OPEN`) instead.
2. **The race.** Two concurrent calls can both pass step 1 before either
   writes. Migration `0003_escalation_open_thread_unique` adds a partial
   unique index — `UNIQUE (learner_thread_id) WHERE status <> 'resolved'` —
   so the database itself refuses the second insert. `create_escalation_ticket`
   is wrapped in `try/except IntegrityError`; on conflict, the losing call
   re-reads and returns the winner's ticket instead of raising, the same
   shape as `back_office.create_cohort`'s existing race handling.

The index is partial, not a plain unique constraint, because a thread can
legitimately be escalated more than once over its lifetime: once a ticket is
`resolved`, a follow-up question in the same conversation opens a new one.
Only one ticket may be *in flight* (`open` or `waiting_human`) per thread at
a time.

## missing_human

When a cohort has nobody holding the required role, the request does not
fail. The ticket is still created — `status=open`, `assigned_human_id=None`
— so the question is on record rather than lost. A `escalation_no_human_available`
warning is logged with the cohort and role for whoever is tracking
unassigned cohorts. The learner receives a plain, honest sentence:

> I don't have a confident answer for that. I've logged it as ESC-000031,
> but this cohort doesn't have a tech lead assigned yet, so I can't hand it
> to someone right now — it's on record and will be picked up once one is.

This is deliberately different from the "handed off" message — it never
implies a person is already looking into it, because nobody is. A ticket
left `open` with no assigned human is also the natural hook for a future
chaser/reassignment job, though building that is out of this task's scope.

A related, distinct failure — a human *was* found but the Mattermost DM
failed to open or post — is handled separately: the ticket keeps its assigned
human and stays `open` (not `waiting_human`) so a retry can pick it back up,
and it's logged at `error` level (`escalation_dm_handoff_failed`) rather than
`warning`, since it's a system fault worth paging on, not a cohort
configuration gap. The learner is told, just as honestly, that a human is
assigned but hasn't actually been reached yet — the wording never claims a
colleague is already on it, since that would not be true until the DM
actually lands.
