# Manual standup GUI test script

Copy-paste script to test the proactive standup feature end-to-end through the
Mattermost GUI. Tested against the post-merge stack (`feature/standup-collection`
merged with `origin/dev`, head +d32cf4519a86+).

Stands up: `sprintflow-assistant` bot -> prompt DM -> reply parser -> channel
summary. Uses two real accounts that exist in the dev Mattermost:

| Account          | Role in the test                       |
|------------------|----------------------------------------|
| `admin`          | superadmin / scrum master (opens sprint, assigns roles, summarizes) |
| `channel_a_user` | the "learner" who receives the prompt DM and replies |

URL: `http://localhost:8065` (log in the two users in two browser tabs,
join them to **General**).

---

## 0. Preflight (terminal, once)

```bash
docker compose ps                                   # all three services healthy
curl -s http://localhost:8065/api/v4/system/ping    # 200
docker compose exec ai-core python -c "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:8000/health').read().decode())"
#                          ^-- standup_dispatcher must show { "running": true }
```

## 1. Bootstrap a sprint (admin, in **General**)

Paste these one at a time, waiting for each answer before the next:

```
@sprintflow-assistant Assign @channel_a_user the learner role
```

> Expect `[ROLE_ASSIGNED]` (or `ROLE_ALREADY_ASSIGNED`). If you get
> `[AUTHORISATION_REFUSED]`, the post-merge authorisation layer has not mapped
> `admin` to superadmin yet — check `assign_role`/superadmin bootstrap before
> continuing.

```
@sprintflow-assistant Open a sprint called Sprint 1
```

> Expect `[SPRINT_OPENED] ...`. (Omit dates to start today for the configured
> sprint length; never overlaps an open/planned sprint.)

## 2. Receive the prompt (channel_a_user)

Within ~1–2 minutes (dispatcher polls every 30 s; `STANDUP_PROMPT_LOCAL_HOUR`,
default 09:00 local — if your clock is already past 09:00 the DM arrives on the
next poll), the bot DMs the learner:

```
Good morning <name> — time for a quick standup.

Reply with three short lines:
1. What did you do?
2. What will you do next?
3. Any blockers?
```

> **Tip:** to force delivery at any time of day, set
> `STANDUP_PROMPT_LOCAL_HOUR` in `.env` to an hour that has already passed and
> recreate ai-core (`docker compose up -d --force-recreate ai-core`).

## 3. Reply cases — all as channel_a_user, **in the DM thread with the bot**

### A. Numbered reply (happy path)

```
1. Fixed the parser tests
2. Finish the feature merge
3. None
```

> Expect: recorded. Verify in step 6.

### B. Label-led / freeform reply

```
blockers: none
done: parser hardening
next: verify the merged schema
```

> Expect: recorded (label-led form is parsed).

### C. Bare acknowledgement

```
noted
```

> Expect: recorded as a bare acknowledgement (parser's ack-only path).

### D. Nonsense — must NOT become a standup

```
asdfghjkl
```

> Expect: not recorded; the message falls through to normal chat (the bot may
> answer casually). Nothing appears in the summary.

### E. Scope guard — replying in the **channel**, not the DM

Paste in **General** (as channel_a_user):

```
1. Unauthorised channel reply
2. Should be ignored
3. Blocked
```

> Expect: NOT recorded. Reply ingestion is DM-only; a channel reply is treated
> as a normal chat message. Missing from the summary.

## 4. Summarise (admin, in **General**)

```
@sprintflow-assistant Summarize today's standups for this channel
```

> Expect: a summary naming the channel + date, listing `channel_a_user`'s
> recorded entries and who is still missing.

```
@sprintflow-assistant Who has not submitted a standup today?
```

> Expect: confirms which members are outstanding.

## 5. Expected verdict table

| #  | Input                                      | Expected result                                        |
|----|--------------------------------------------|--------------------------------------------------------|
| 1  | assign learner role                        | `ROLE_ASSIGNED`                                        |
| 2  | open sprint                                | `SPRINT_OPENED`                                        |
| 3  | numbered reply (DM)                        | recorded                                               |
| 4  | label-led reply (DM)                       | recorded                                               |
| 5  | bare `noted` (DM)                          | recorded as acknowledgement                            |
| 6  | gibberish (DM)                             | discarded, normal chat                                 |
| 7  | numbered reply in channel (not DM)         | not recorded (DM-only ingestion)                       |
| 8  | "Summarize today's standups"               | summary lists learner's entries + who is missing       |

PASS = rows 3–8 behave as above; no `Traceback` in
`docker compose logs --tail=100 ai-core`; `standup_dispatcher` stays `running`
with `errors: 0` in `/health`.

## Troubleshooting

- No DM after 2 min:
  `docker compose exec -T postgres psql -U sprintflow -d sprintflow -tAc "SELECT id,learner_id,status,dm_channel_id,post_id FROM daily_standup_prompts ORDER BY id DESC LIMIT 5;"`
- Dispatcher stalled: check `/health` `standup_dispatcher.last_summary`
  (`ensured` should be >= 1, `sent` 1 per DM) and
  `docker compose logs --tail=50 ai-core | grep -i standup`.
- Bot answers `AUTHORISATION_REFUSED` on sprint open: the requester needs
  scrum-master/tech-lead/superadmin scope in that channel; fix the superadmin
  mapping first.