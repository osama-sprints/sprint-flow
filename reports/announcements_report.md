# SprintFlow — Safe Cohort Announcements — Report

Code: `app/models/announcement.py`, `app/services/announcements.py`.
Verification: `tests/test_announcement_bugfixes.py` (9 tests, all passing).

**Two items are explicitly still pending and are not being reported as
done** — see the end of `verification` and the notes inline below. This
report documents what is confirmed working today, not the full target state.

## preview/confirmation design

`create_announcement_preview` builds the exact preview (final text, cohort
id, resolved channel, resolved audience, delivery mode) and writes an
`Announcement` row with `confirmation_status="pending"` — no Mattermost API
call happens on this path at all.

Confirmation is a separate, later call to `confirm_and_dispatch_announcement`,
which atomically claims the row (`UPDATE announcements SET
confirmation_status='confirmed' WHERE id=:id AND confirmation_status='pending'`)
before doing anything else. Only if that claim succeeds (`rowcount == 1`)
does the function proceed to the rate-limit check and then the Mattermost
call. A decline/cancel goes through `cancel_announcement`, which is the same
atomic-claim pattern (`WHERE confirmation_status='pending'`) but sets
`outcome=CANCELLED` instead — a cancelled announcement never reaches
`send_to_mattermost`.

## deduplication mechanisms

The idempotency guarantee rests entirely on the single atomic `UPDATE ...
WHERE confirmation_status='pending'` in `confirm_and_dispatch_announcement`.
Postgres executes that statement as one atomic operation; only one concurrent
caller can see `rowcount == 1`, every other caller (replay or genuine
concurrent confirmation) sees `rowcount == 0` and returns
`"already_processed"` without ever calling Mattermost. This is checked
against a mocked session in `tests/test_announcement_bugfixes.py`'s sibling
confirmation tests (from the earlier round), which simulate the race with an
`asyncio.Lock()` inside the mock to model "only the first caller wins."

**Pending**: that mock proves the *logic* is correct, not that Postgres
itself enforces it under genuine concurrent transactions. A real
`tests/integration/test_announcements_db.py` running two actual concurrent
`confirm_and_dispatch_announcement` calls against a live database (via
`asyncio.gather`) has not been completed and run yet. This is the single most
important remaining verification step, since it's the one property a mock
cannot actually prove.

## authorization and scoping enforcement

Fixed today: `resolve_announcement_channel` previously accepted a
`channel_id` directly from the caller, which was an arbitrary-channel-override
vulnerability — the exact thing the task brief prohibits. It now takes
`cohort_id` only, resolves the `Sprint`/cohort record from the database
(`sprint_repo.get_sprint(cohort_id, session)`), checks `sprint.status ==
"active"` (refusing inactive/archived cohorts with `ValidationFailed`), and
only then authorises against `sprint.channel_id` — the channel used is always
the one read from the stored record, never a caller-supplied value.

Recipient resolution reuses the real channel-role data: `resolve_recipients_by_role`
lists active `ChannelRole` rows for the resolved channel and filters by role
key; `resolve_recipients_by_usernames` looks up each username and requires
`channel_repo.get_role_for_user_in_channel` to return a role (not just that
the username exists) — a real but non-member username is rejected, not
silently allowed through.

## audit schema

`Announcement` (`app/models/announcement.py`): `id`, `requester_id` (FK
users), `cohort_id` (FK cohorts), `resolved_channel_id`, `exact_text`,
`delivery_mode`, `confirmation_status` (pending/confirmed/cancelled),
`mattermost_post_id` (null until actually sent), `outcome`
(`AnnouncementOutcome`: at least PENDING/SENT/CANCELLED/RATE_LIMITED/FAILED —
see pending note below), `status_changed_at`, plus `created_at`/`updated_at`
from `DomainBase`.

Confirmed-covered outcomes: `SENT` (dispatch success, `mattermost_post_id`
stored), `CANCELLED` (decline path), `RATE_LIMITED` (rate check failed after
claim), `FAILED` (Mattermost call raised — caught and recorded rather than
crashing).

**Pending**: the brief requires an audit row for `unauthorized` attempts too.
Currently, `resolve_announcement_channel` raises `AuthorisationRefused`/
`ValidationFailed` *before* any `Announcement` row is created — meaning an
unauthorized or inactive-cohort attempt today produces **no audit trail at
all**. Closing this requires either writing a row in an `outcome=UNAUTHORIZED`
(or similar) state at the point of refusal, or catching the exception one
layer up (in the tool/node that calls this service) and writing the audit
row there before re-raising/relaying the refusal. Not yet done.

## threat model analysis

| Threat (from the brief) | Mitigation | Status |
|---|---|---|
| Accidental message | Explicit preview step; nothing is posted until a separate confirmed call | Done |
| Cross-cohort leak | Channel resolved strictly from the stored cohort record, never caller-supplied | Fixed today |
| Duplicate delivery (replay/concurrent confirm) | Atomic `UPDATE ... WHERE status='pending'` claim | Logic done; real concurrency proof pending |
| Rate-limit bypass via rephrasing | Enforced by a Postgres count query in code, not model judgment — no natural-language input reaches this check at all | Done |
| Unauthorized sender | Authorization checked before channel resolution or any dispatch work | Enforcement done; audit trail for the refusal itself pending |

## verification

`tests/test_announcement_bugfixes.py` — 9 unit tests, all passing, each
targeting one of four confirmed bugs found and fixed today:
1. Channel resolved from the stored cohort, not accepted from the caller (3 tests).
2. `cohort_id` correctly reaches the `Announcement` row on preview (1 test).
3. `resolve_recipients_by_role` calls `channel_repo.list_channel_roles` with
   the real signature (2 tests).
4. `resolve_recipients_by_usernames` calls
   `channel_repo.get_role_for_user_in_channel` with the real signature (3
   tests).

All nine mock the database session; none of them touch a real Postgres
instance.

**Explicitly still pending, not yet done:**
- A real integration test (`tests/integration/test_announcements_db.py`)
  proving idempotency under genuine concurrent/replayed confirmation against
  a live database, and proving rate-limiting and the full audit trail against
  real rows rather than mocks.
- An audit row for unauthorized/refused attempts (see `audit schema` above).

These two items should be closed before this feature is considered fully
meeting the brief's success standard, which names both "deterministically
enforced... backed by complete PostgreSQL audit logs" and "passing
verification tests" as requirements, not optional extras.
