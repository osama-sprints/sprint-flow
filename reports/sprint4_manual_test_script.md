# Sprint 4 Manual Test Script

Use this script in Mattermost to manually test the three Sprint 4 capabilities.

## Before Starting

1. Start the stack:

```bash
make up
make bootstrap
```

2. Open Mattermost at `http://localhost:8065`.
3. Log in as an administrator whose email is included in `ADMIN_EMAILS`.
4. Confirm the bot is online. The default bot username is:

```text
sprintflow-assistant
```

5. Use a private test channel or a direct message with the bot. Replace `sprintflow-assistant` below if your bot username is different.
6. Wait for the bot reply after each message. For threaded conversations, reply in the same thread.

For an automated comparison after the manual test:

```bash
make verify-fast
python3 scripts/verify_scheduling.py
python3 scripts/verify_calendar_integration.py
python3 scripts/verify_standups.py
```

## Test 1: Ceremony Scheduling

### 1.1 Successful scheduling with confirmation

Send this message in a DM or test channel:

```text
@sprintflow-assistant schedule sprint planning for tomorrow at 2 pm Europe/Berlin
```

Expected bot behavior:

- It shows the ceremony type.
- It shows the local time and UTC time.
- It asks for explicit confirmation.
- It does not create the ceremony yet.

Reply:

```text
yes
```

Expected result:

- The bot confirms that the ceremony was scheduled.
- Exactly one ceremony is created.

### 1.2 Declining must not write

Send:

```text
@sprintflow-assistant schedule a retrospective for tomorrow at 4 pm Europe/Berlin
```

Reply:

```text
no
```

Expected result:

- The bot says that nothing was scheduled.
- No ceremony is created.

### 1.3 Unclear confirmation must not write

Send:

```text
@sprintflow-assistant schedule a retrospective for tomorrow at 5 pm Europe/Berlin
```

Reply:

```text
maybe later
```

Expected result:

- The bot treats the answer as not confirmed.
- No ceremony is created.

### 1.4 Ambiguous time

Send:

```text
@sprintflow-assistant schedule a retrospective for tomorrow at 2
```

Expected result:

- The bot asks whether 2 means 2 AM or 2 PM, or asks for a timezone/time clarification.
- No ceremony is created.

### 1.5 Timezone conversion

Send:

```text
@sprintflow-assistant schedule daily standup for tomorrow at 9 am Africa/Cairo
```

Expected result:

- The confirmation displays the Cairo local time.
- It also displays the equivalent UTC time.
- Replying `no` leaves the database unchanged.

### 1.6 Duplicate scheduling

First schedule and confirm:

```text
@sprintflow-assistant schedule sprint planning for tomorrow at 2 pm Europe/Berlin
```

```text
yes
```

Send the same request again in the same channel:

```text
@sprintflow-assistant schedule sprint planning for tomorrow at 2 pm Europe/Berlin
```

Expected result:

- The bot reports that the same ceremony already exists or refuses the duplicate.
- It does not create a second identical ceremony.

### 1.7 Conflict detection

Create and confirm a ceremony at a fixed time:

```text
@sprintflow-assistant schedule a retrospective for tomorrow at 3 pm Europe/Berlin
```

```text
yes
```

Then request an overlapping ceremony:

```text
@sprintflow-assistant schedule sprint planning for tomorrow at 3:15 pm Europe/Berlin
```

Expected result:

- The bot reports the conflict.
- Under the default refuse policy, no overlapping ceremony is created.

### 1.8 Amendment and cancellation

List ceremonies:

```text
@sprintflow-assistant list my ceremonies
```

Amend one future ceremony:

```text
@sprintflow-assistant move ceremony <CEREMONY_ID> to tomorrow at 4 pm Europe/Berlin
```

Reply if confirmation is requested:

```text
yes
```

Cancel it:

```text
@sprintflow-assistant cancel ceremony <CEREMONY_ID>
```

Reply:

```text
yes
```

Expected result:

- The move requires confirmation and changes the scheduled instant.
- The cancellation requires confirmation and changes the status to cancelled.
- A cancelled ceremony is not sent reminders.

## Test 2: Proactive Ceremony Reminders

Reminder delivery is controlled by the background poller. The normal windows are 24 hours and 1 hour before the ceremony. The poller checks approximately every five minutes.

### 2.1 One-hour reminder

Schedule a ceremony approximately one hour from now. Use an explicit time and timezone:

```text
@sprintflow-assistant schedule a retrospective for <TODAY_OR_TOMORROW> at <TIME> <TIMEZONE>
```

Example:

```text
@sprintflow-assistant schedule a retrospective for tomorrow at 2 pm Europe/Berlin
```

Reply:

```text
yes
```

Add at least one active member to the channel and wait for the poller window.

Expected result in the member DM:

- One reminder identifies the ceremony type.
- The reminder contains the local time and timezone.
- The reminder identifies the organizer.
- The reminder is posted to the member DM, not the public channel.

### 2.2 Reminder restart/deduplication

After the first reminder arrives, restart only the ai-core service:

```bash
docker compose restart ai-core
```

Wait for the next poll cycle.

Expected result:

- No second reminder is sent for the same ceremony, member, and window.
- A different reminder window, such as 24h versus 1h, is allowed to send once.

### 2.3 Cancelled ceremony is skipped

Schedule a ceremony, confirm it, then cancel it before the reminder window:

```text
@sprintflow-assistant schedule a retrospective for tomorrow at 6 pm Europe/Berlin
```

```text
yes
```

```text
@sprintflow-assistant cancel ceremony <CEREMONY_ID>
```

```text
yes
```

Expected result:

- No reminder is sent for the cancelled ceremony.

### 2.4 Member scope filtering

Use two test users in the same channel:

- User A: active member
- User B: inactive or removed member

Schedule a ceremony inside a reminder window.

Expected result:

- User A receives the reminder.
- User B receives no reminder.
- A member of a different channel receives no reminder.

## Test 3: Proactive Daily Standups

Standup prompts are sent automatically at the configured local prompt hour. The exact hour is controlled by `STANDUP_PROMPT_LOCAL_HOUR` and each user's timezone.

### 3.1 Prompt content

When the prompt arrives in your DM, it should ask three questions:

1. What did you do?
2. What will you do next?
3. Are there any blockers?

Expected result:

- The prompt is sent as a DM.
- It is sent according to your local timezone, not the server timezone.

### 3.2 Submit a structured standup

Copy and send this as a reply to the prompt thread:

```text
1. Completed the ceremony test suite and fixed reminder deduplication.
2. I will review the integration results and prepare the report.
3. Blocked by the remaining Mattermost deployment review.
```

Expected result:

- The bot confirms that the standup was recorded.
- The saved fields are:
  - What I did: `Completed the ceremony test suite and fixed reminder deduplication.`
  - What I will do: `I will review the integration results and prepare the report.`
  - Blockers: `Blocked by the remaining Mattermost deployment review.`
- The original raw message is preserved.

### 3.3 Submit labeled sections

For another test day, reply with:

```text
Done: finished the database tests
Plan: run the CI workflow
Blockers: waiting for review
```

Expected result:

- The same three fields are parsed correctly.
- The bot confirms the submission.

### 3.4 Submit a flat reply

Reply with:

```text
I spent the day fixing the scheduler and have no blockers.
```

Expected result:

- The complete text is preserved as the first field.
- The bot does not lose or reject the response merely because it is not numbered.

### 3.5 Duplicate reply

Send a second answer for the same prompt:

```text
1. A second answer for the same day
2. Nothing new
3. None
```

Expected result:

- The bot says that a standup was already submitted for today.
- The original standup entry is not overwritten.
- The duplicate raw reply is retained for audit/history if configured.

### 3.6 Normal DM is not swallowed

Send a normal question that is not a standup answer:

```text
@sprintflow-assistant what is the status of the current sprint?
```

Expected result:

- The message goes to the normal assistant conversation flow.
- It is not recorded as a standup response.

### 3.7 Inactive learner filtering

Remove the learner role or mark the membership inactive, then wait for the next dispatch cycle.

Expected result:

- No new standup prompt is sent.
- An existing pending prompt is closed or skipped rather than delivered to the inactive member.

### 3.8 Late reply

After the local standup day has closed, reply to the old prompt:

```text
1. Late update
2. Continue normal work
3. None
```

Expected result:

- The reply is classified as late.
- It is preserved as a raw reply.
- It does not create or modify the closed day's standup entry.

## Database Checks

Use these commands only for verification, not to modify rows:

```bash
make psql
```

Useful checks:

```sql
SELECT id, status, scheduled_at, time_zone
FROM ceremonies
ORDER BY id DESC
LIMIT 10;

SELECT ceremony_id, recipient_mm_id, window, sent_at
FROM ceremony_reminders
ORDER BY sent_at DESC
LIMIT 20;

SELECT id, learner_id, local_date, timezone, status, prompt_post_id
FROM daily_standup_prompts
ORDER BY id DESC
LIMIT 20;

SELECT learner_id, log_date, what_i_did, what_i_will_do, blockers, raw_response
FROM daily_standups
ORDER BY id DESC
LIMIT 20;
```

## Pass Criteria

The manual run passes when:

- Scheduling never writes before a clear `yes`.
- `no` and unclear confirmations create nothing.
- Times show both the requested timezone and UTC.
- Duplicate scheduling is refused.
- Cancelled and inactive scopes receive no reminders or prompts.
- Each reminder window sends at most one DM per member.
- Standup replies are parsed, attributed, deduplicated, and preserved correctly.
- Normal assistant questions still reach the normal conversation flow.
