# Escalation Closure — Technical Report
 
## recognising_replies
 
A reviewer's decision arrives as an ordinary Mattermost message — there is no separate API or slash command for it. The interception happens in `mattermost_ws.py`'s `_handle_posted`, *before* the normal chat dispatch, for one structural reason: direct and group messages are unconditionally routed to the general chat agent (`_should_handle`'s rule 1), so without an early check every reviewer reply would be answered by the LLM as an ordinary question instead of being treated as a decision. The check is scoped to `channel_type in {"D", "G"}` only — escalation handoffs are only ever opened as DMs (`app.services.escalation.open_escalation`), so a public-channel message is never even considered a candidate.
 
## attribution
 
Three signals are tried, in strict trust order, and the first one that produces a match wins:
 
1. **Thread match** — the incoming message's `root_id` equals a ticket's `human_dm_thread_id`. Deterministic by construction: that id is the id of the specific post the bot made when opening that one ticket, so it cannot collide across tickets.
2. **Explicit citation** — the message text contains a well-formed reference (`ESC-000042`, six digits, matching `format_ticket_ref`). Looked up directly by primary reference; the sender's identity is separately verified in `uncertainty_handling` below.
3. **Neither** — never resolved by inference. See the next section.
## uncertainty_handling
 
This is the section with a real design decision behind it, revised after mentor review. The initial design treated "one open ticket, unthreaded reply" as safe to resolve automatically. That was rejected, correctly: an unthreaded, unreferenced message with exactly one candidate ticket is indistinguishable from ordinary chat that happens to arrive while one ticket is open ("sounds good" replying to a completely unrelated remark). The final rule: **an unthreaded, unreferenced reply is never resolved to a ticket, regardless of how many are open.**
 
What differs by candidate count is only *what the person is told*:
- **Zero** open tickets assigned to this reviewer → the message isn't escalation-related at all; it passes through to normal chat untouched (`NOT_ESCALATION`).
- **One or more** open tickets → the reviewer is asked to reply inside the specific thread or cite the reference, listing the open ones by `ticket_ref` so they know what's outstanding (`AMBIGUOUS`). No ticket's state changes in this branch.
A cited reference that doesn't belong to the sender (`WRONG_OWNER`) or doesn't exist at all (`TICKET_NOT_FOUND`) is refused the same way — informative, no guessing, no state change.
 
## composing_the_answer
 
Once a ticket is attributed, `_synthesize_answer` sends exactly two things to the model: the learner's original `question` (stored verbatim on the ticket) and the reviewer's raw reply (verbatim, unmodified). Nothing else — no ticket metadata, no reviewer identity — is included in the prompt, so there is nothing for the model to leak even if it disregarded instructions.
 
## grounding
 
The system prompt (`app/core/prompts/escalation_closure/system.md`) is the actual grounding mechanism, and its core instruction is a distinction rather than a rule list: *expand tone and completeness, never information*. It explicitly tells the model to answer only the part of the question the human's text actually addressed, to say plainly it needs a moment rather than guess when the input is too sparse, and to never name a reviewer, mention a ticket, or reference an internal process. Grounding here is prompt-level, not schema-enforced — the trade-off accepted is that this depends on instruction-following rather than a structural guarantee; a future iteration could add a lightweight post-hoc check (e.g. flagging numbers/dates in the output not present in the input) if false expansion is observed in practice.
 
## delivery
 
Delivery treats the learner and the reviewer asymmetrically, on purpose. Posting to the learner's thread (`ticket.learner_channel_id` / `ticket.learner_thread_id`) is the critical path: the ticket is only marked `resolved` *after* that post succeeds. If it fails, the ticket is left exactly as it was (`waiting_human`) and the reviewer is told honestly that the answer exists but wasn't delivered yet — nothing is silently lost, and nothing is marked done that isn't. The reviewer's own confirmation is posted after the ticket is already closed and is best-effort: if it fails, it's logged but does not reopen or block anything, since the reviewer already knows what they decided.
 
## confirming_to_the_human
 
A short, factual confirmation is posted into the same DM thread the reviewer used, naming only the ticket reference and that delivery succeeded — no repetition of the synthesized answer, no learner identity beyond what the reviewer already knew from the original handoff message.
 
## verification
 
`tests/test_escalation_closure.py` covers, with the database and Mattermost mocked:
- A full round trip (threaded reply → synthesis → learner delivery → reviewer confirmation → closed record), including an explicit assertion that the delivered text never contains a ticket reference or the word "reviewer".
- The specific regression the design review flagged: exactly one open ticket, unthreaded and unreferenced, must still produce `AMBIGUOUS`, not an automatic resolution.
- Multiple open tickets, ambiguous, listing every reference.
- Explicit ticket citation resolving without a thread match.
- A cited ticket belonging to a different reviewer, and a cited ticket that doesn't exist.
- A reply landing on an already-resolved ticket (post-closure handling) — informational reply, no state change, no crash.
- A non-DM channel and an ordinary DM chat message (zero open tickets) both passing through untouched, with zero database calls in the non-DM case.
- A learner-delivery failure leaving the ticket open rather than marking it resolved.
 