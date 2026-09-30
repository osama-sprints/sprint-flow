# Proactive Daily Standups — Unexpected-User Scenario Catalog

What happens when a user does something unexpected while answering a daily
standup prompt. Each row names the behaviour, where it is handled in code, and
the test that locks it in. Status legend:

- **HW** = handled in code (parser / classifier / dispatcher)
- **GD** = guarded (rejected, deduped, or tolerated without data corruption)
- **IM** = intentionally ignored (accepted by design, no special handling)

## Reply content — what the user types

| # | Scenario | Behaviour | Status | Where | Test |
|---|----------|-----------|--------|-------|------|
| 1 | Sends the reply in the prompt thread | The expected path — accepted as a standalone, prompt closes `answered`, entry stored | HW | `ingest_standup_reply` / `_classify_reply` | `test_standups.py::test_parse_*`, integration `test_ingest...` |
| 2 | Sends numbered `1. 2. 3.` items | Parsed into did / will / blockers exactly | HW | `_NUMBERED_RE` numbered pass | `test_parse_numbered_*` |
| 3 | Sends a bare non-standup message (no number, no label) | One flat `what_i_did` line, nothing lost | HW | flat fallback | `test_parse_flat_fallback_...`, `test_not_a_standup_when_attribution_fails` |
| 4 | Sends a 4th+ numbered item (e.g. `4. ...`) | Extra items are ignored, first three win | HW | numbered pass stops at slot 3 | `test_parse_numbered_items_beyond_the_third_are_ignored` |
| 5 | Uses a double-digit number (`10. ...`) | A lone `10.` item no longer mangles its digit into `0.` | HW | `\d+` in `_NUMBERED_RE` | `test_parse_numbered_double_digit_item_keeps_its_digit` |
| 6 | Mixes numbered items with a `blockers:`/`blocked:` label | The label line starts the third slot | HW | numbered pass carve-out | `test_parse_numbered_then_blocker_label_starts_the_third_slot` |
| 7 | Uses `blocked:` instead of `blockers:` | Same field | HW | `_BLOCK_LABELS` | `test_parse_numbered_blocked_label_carries_continuation_lines` |
| 8 | Capitalises labels (`DONE:`, `PLAN:`) | Matched case-insensitively | HW | `_match_label` casefold | `test_parse_uppercase_labels` |
| 9 | Wraps lines in markdown (`**1. x**`, `` `done:` x ``) | Structural markers stripped for matching; raw text still stored verbatim | HW | `_strip_markdown` | `test_parse_markdown_bold_*`, `test_parse_backtick_wrapped_labels` |
| 10 | Puts a space before the colon (`done : x`) | Parsed cleanly | HW | label `rest.lstrip(".:")` | `test_parse_labeled_space_before_colon` |
| 11 | Indents numbers with tabs | Parsed | HW | `^\s*\d+` | `test_parse_numbered_tab_indented` |
| 12 | Leaves an empty numbered item (`1.`) | Stored truthfully as an empty section, later items unaffected | HW | section list logic | `test_parse_numbered_empty_first_section_stores_truthfully` |
| 13 | Starts with a number but parentheses style (`(1) ..., (2) ...`) | Treated as flat text, nothing dropped | GD | no match on `(` | `test_parse_numbered_with_parenthetical_is_flat` |
| 14 | Replies only in emoji | Stored as flat text, not an acknowledgement | HW | ack detection | `test_parse_emoji_heavy_reply_is_not_lost` |
| 15 | Replies in a non-English language | Stored verbatim as flat text, never an error | HW | flat fallback | `test_parse_german_date_word_is_flat_not_an_error` |
| 16 | Sends blank / whitespace-only reply | Empty standup, treated as no content | HW | blank guard | `test_parse_blank_reply` |
| 17 | Leading blank lines before the numbered list | Ignored, numbered parse still wins | HW | blank-line skip | `test_parse_leading_blank_lines_do_not_hide_numbered` |
| 18 | Uses `0.` for an item | Tolerated as a section | HW | `\d+` | `test_parse_numbered_zero_leading_item_is_tolerated` |
| 19 | Markdown bold inside a numbered item body (`3. **db down**`) | Emphasis stripped from extracted content | HW | `_strip_markdown` on group | `test_parse_markdown_bold_wrapped_blocker_text` |

## Acknowledgement / bot chatter

| # | Scenario | Behaviour | Status | Where | Test |
|---|----------|-----------|--------|-------|------|
| 20 | Answers "thanks!" / "k" / "sure thing" | Routed to the agent as a thank-you, NOT stored as a standup | HW | `is_ack_only` | `test_is_ack_only` |
| 21 | Answers "10/10" (rating) | NOT an ack — stays unprocessed content | GD | length guard | `test_is_not_ack_only` |
| 22 | Answers a short positive with emotion lost in stripping (`k.`, `okay!`) | Still an ack | HW | normalisation | `test_is_ack_only` |
| 23 | Answers "well noted" | Now recognised as an ack | HW | `ACK_ONLY_WORDS` | `test_is_ack_only` |
| 24 | Answers "done but tired" (partial match) | NOT an ack — words must cover the whole reply | GD | `ACK_ONLY_WORDS` membership | `test_is_not_ack_only` |
| 25 | Mentions the bot elsewhere in a DM (`@bot ...`) | Ignored for standup purposes unless reply in a prompt thread | GD | mention-guard position | `test_starts_with_mention` |

## Threading and attribution

| # | Scenario | Behaviour | Status | Where | Test |
|---|----------|-----------|--------|-------|------|
| 26 | Replies in someone else's prompt thread | `NOT_A_STANDUP`; nothing stored, other prompt stays `dispatched` | GD | `_find_reply_prompt` learner match | `test_reply_in_someone_elses_prompt_thread_is_not_ours` |
| 27 | Resends the exact same post (webhook redelivery) | No-op — unique constraint on reply provenance | GD | DB unique + ingest guard | probe check 2 (redelivery) |
| 28 | Sends a second reply the same day | `duplicate`; raw preserved, confirmation re-sent, entry unchanged | GD | duplicate guard | probe check 2 / integration duplicate test |
| 29 | Replies after the day is already `missed` | Captured as `late`; prompt stays closed | HW | `late` classification | probe check 3 |
| 30 | Sends a message in a brand-new DM with no prompt | `NOT_A_STANDUP`, nothing stored | GD | `_find_reply_prompt` | `test_not_a_standup_when_attribution_fails` |

## Timing and timezones

| # | Scenario | Behaviour | Status | Where | Test |
|---|----------|-----------|--------|-------|------|
| 31 | User in a different timezone than the server | Prompt goes at the user's configured local hour, converted to UTC | HW | `dispatch_at_for` + `STANDUP_RECIPIENT_TZ` | probe check 5, `test_dispatch_at_for_*`, `test_local_date_of_*` |
| 32 | Learner west of UTC answering early in the morning | Prompt not claimable until the local hour arrives | HW | `claim_due_prompts` due-check | probe checks 5 |
| 33 | Learner east of UTC (Tokyo 20:00 UTC) | Local standup day rolls forward | HW | `local_date_of` | `test_local_date_of_rolls_forward_a_day_east_of_utc` |
| 34 | DST transitions (winter vs summer) | Dispatch hour still maps to the correct UTC instant | HW | `zoneinfo` / `astimezone` | `test_dispatch_at_*` (NY winter/summer) |
| 35 | Weekend days | Prompts still run (documented choice); nothing special | IM | — | probes |
| 36 | DST double-hour / gap at dispatch time | `zoneinfo` resolves to a real instant or falls back safely | HW | `astimezone` | `test_dispatch_at_zone_west_of_utc_same_utc_day` |

## Cohort / membership edge cases

| # | Scenario | Behaviour | Status | Where | Test |
|---|----------|-----------|--------|-------|------|
| 37 | Sprint completed | Learners are never prompted again | HW | active-sprint filter | probe check 5 (inactive cohort) |
| 38 | Channel membership removed (inactive `ChannelRole`) | Learner never prompted | HW | membership filter | probe check 5 |
| 39 | Membership toggles mid-sprint | The check is per-pass, so it self-heals next pass | HW | per-pass filter | — |
| 40 | Cohort has zero active learners | Dispatched day produces no prompts, no crashes | HW | empty reduce | dispatcher pass |

## Delivery / reliability

| # | Scenario | Behaviour | Status | Where | Test |
|---|----------|-----------|--------|-------|------|
| 41 | Mattermost returns no DM channel | Prompt skipped this pass (exponential backoff), retried later | HW | `STANDUP_RETRY_BACKOFF_SECONDS` | `test_deliver_prompt_retries_then_gives_up` |
| 42 | Mattermost post creation fails | No delivery, count stays pending, retried | HW | `STANDUP_MAX_ATTEMPTS` | `test_deliver_prompt_retries_then_gives_up` |
| 43 | Two dispatcher workers race for the same prompt | At most one claims via the lease | HW | `claim_due_prompts` lease (`FOR UPDATE SKIP LOCKED`) | `test_claim_lease_makes_rows_exclusive_then_reusable` |
| 44 | Dispatcher restarts mid-pass | Leases expire (`lease_seconds`); prompt becomes claimable again | HW | lease TTL | `test_claim_lease_makes_rows_exclusive_then_reusable` |
| 45 | Server wall clock is UTC-biased | Dispatch never uses server local time — always user-local | HW | `dispatch_at_for` | probe 5 |
| 46 | Configures off / unknown timezone (`STANDUP_USE_SERVER_TZ`) | Falls back to server UTC scheduling instead of crashing | HW | config fallback | settings test |
| 47 | Inactive `STANDUP_ENABLED` | Worker exits without scheduling; ingestion still accepted | HW | `start()` gate | — |

## Ops

| # | Scenario | Behaviour | Status | Where | Test |
|---|----------|-----------|--------|-------|------|
| 48 | `verify_standups` run twice | Rows cleaned up first; deterministic re-run | HW | probe teardown | probe check 6 |
| 49 | Migration applied twice / downgrade | Idempotent Alembic revisions; downgrade drops tables cleanly | HW | migration 0005 | `alembic check` (no new drift) |
| 50 | Multiple prompts for one day (idempotency) | Unique per sprint/learner/day — re-ensure returns the same row | HW | `ensure_prompt` | `test_ensure_prompt_is_idempotent` |

## Summary

All scenarios are either handled in code, guarded against corruption/replay, or
deliberately ignored. The two intentional behaviours worth knowing:

- **A 4th+ numbered item is dropped** rather than merged into blockers (design
  choice: first three items are the contract).
- **Weekend prompts are still dispatched** (design choice: a filled standup is
  better than a silent day; no weekend special-casing).