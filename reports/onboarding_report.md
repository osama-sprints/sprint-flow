# Task 5 — Proactive Role-Aware Onboarding

## What this does

When a new account registers on Mattermost, ai-core is notified via the
`new_user` WebSocket event (already handled in `mattermost_ws.py` for team
auto-join). This task adds two behaviors on top of that arrival:

1. **Immediate greeting** — a DM welcoming the person, personalized by role
   when a role is known.
2. **A follow-up 3 days later** — a check-in message, re-checking their role
   in case it wasn't known at greet time.



## Files

- `app/models/onboarding.py` — the `OnboardingState` table.
- `app/services/onboarding.py` — role resolution, the idempotent greet, and
  the follow-up poller. All the actual logic lives here.
- `app/services/mattermost_ws.py` — one added call to `handle_arrival()`
  inside the existing `_handle_new_user`, no other changes.
- `app/main.py` — starts/stops the poller as a background `asyncio` task
  alongside the existing WebSocket listener.


## Data model

```
onboarding_state
  user_id                 varchar   primary key  (Mattermost's user id)
  role                    varchar   nullable
  greeted_at              timestamp nullable
  next_followup_due_at    timestamp nullable
  followups_sent          integer   not null, default 0
```



## Property 1: Idempotency

The brief requires that greeting be safe to attempt more than once, since the
`new_user` event can be delivered more than once. `handle_arrival()` does a
check-and-claim inside a single DB transaction before sending anything:


Because `user_id` is the primary key, a second call for the same id either
finds `greeted_at` already set (returns immediately) or is blocked at the DB
level from creating a conflicting row. 



## Property 2: Durability

The brief requires a follow-up due in 3 days to survive a restart. A background `asyncio` loop (`followup_poller`)
polls every 5 minutes for rows that are due and sends them.

Because the due time is never held only in memory, a restart doesn't lose
anything — the next poll after startup just re-queries what's due and picks
up exactly where it left off.





## Testing

Run `python3 scripts/verify_task5_onboarding.py` with the stack up. It
registers a fresh test user, confirms the greeting was recorded, calls the
arrival handler a second time to prove the no-op, forces a follow-up due
date into the past, restarts `ai-core`, and confirms delivery after restart.

