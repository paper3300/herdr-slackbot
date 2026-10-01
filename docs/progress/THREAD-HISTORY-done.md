# Completion messages show the conversation since the previous notification — done

Spec: `docs/progress/THREAD-HISTORY.md`. One commit on `main` (`e8209ad`), not pushed. Tests: **826 passed / 5 skipped**
(812 before; +14 in `tests/test_thread_history.py`, all synthetic records). The live bridge was not run or restarted.

## What changed

- `claude_session.Turn` got `id`: the uuid of the prompt / event record, or of the answer's last record (defaults to
  None, so existing constructions still work). `_final_text` also returns that uuid.
- New `herdr_slackbot/history.py` (pure):
  - `select_range(turns, final_text, cursor)`. The finished turn's answer is the newest assistant turn whose text
    equals the posted answer (`res.text`, same join rule as `evaluate_lines`). The range is the turns after the
    cursor up to and including that answer. Returns None when the answer is not in the conversation.
  - Cursor lookup. First by `id`. If the id is not in the tail, by `at`, but only with an anchor: a turn at or
    before the cursor time must exist, so a tail that starts after the cursor never becomes "since the cursor". With
    no usable cursor (none yet, junk, no anchor, or the cursor at/after the answer), the range is only the latest
    turn: everything after the previous answer, i.e. its prompt(s), queued prompts and events plus the answer.
    Never the whole session.
  - `skip_slack_prompt` drops the last user turn whose `truncate(_norm(text), PROMPT_EXCERPT)` equals the pending
    task's stored prompt excerpt.
  - `newer_cursor` lets a cursor only move forward.
  - `range_markdown` builds the untruncated range for [View full].
- `blocks.py`:
  - The fill loop of `conversation_blocks` is shared as `_history_messages`. The modal output is unchanged.
    `_message_body` now also reports whether it cut.
  - New `result_history_blocks(header, ctx, history, body, result_id, recap, limit, markdown, agent_label=,
    answered_at=, now=)`. Layout: header, context, optional `_… N earlier messages not shown — View full_`, history
    (`👤 You · HH:MM` / `🤖 name · HH:MM` + section, events one `⚙️` line), `🤖 name · HH:MM` label, recap, the final
    answer section (built by the unchanged `result_blocks`, so today's truncation and `[View full]` rules hold), then
    the button. History fills newest-first within `MAX_BLOCKS` (50) minus the fixed blocks, and within
    `RESULT_HISTORY_MAX_CHARS` (12 000) minus the fixed text. `needs_full` = final answer truncated, or a history item
    cut or dropped.
  - `clock_time()`: messages stay in Slack, so they show absolute local times (`HH:MM`, with the date on another day)
    instead of the modal's relative ones.
- `bridge.post_result` is the single place, used by `_notify_transition`, `_complete_pending_now` and
  `_settle_resumed` (`handle_resume`):
  - History is computed only when `res.source == "jsonl"`. The `since` check for Slack tasks is unchanged; a failed
    check falls back to the screen, which means no history.
  - Codex, no transcript, read errors and an answer missing from the conversation all post today's
    `result_blocks` path unchanged. So does a range left empty after skipping the Slack prompt.
  - With history, `[View full]` uploads `range_markdown` of the whole range, including the skipped Slack prompt and
    the final answer.
  - `post_result` now returns the fields to commit (`{"history_cursor": {...}}` or `{}`). Each caller merges them into
    its existing `_commit(key, op, ...)`, so the cursor advances atomically with `last_result_seq` / the op intent and
    only after the post. A retried attempt of the same op reuses the accepted post and recomputes the same cursor.
    A muted or skipped notification never calls `post_result`, so the cursor stays and the next result covers that
    period.
- State docstring documents `history_cursor`. README.md / README.ko.md (result text) and docs/LIVE_TEST.md (section
  7) describe the thread history.

## Tests (`tests/test_thread_history.py`)

- Range unit test: from the cursor, latest-turn-only, timestamp fallback (with anchor), no anchor / junk cursor gives
  the latest turn, cursor at the answer, answer not found.
- Slack-prompt skip on normalized text and the stored truncated excerpt; cursor ordering.
- Bridge, PC prompt A → result (first message: the latest turn; cursor = A's answer). Then PC prompt B → answer, PC
  prompt C → tool → queued D → task event → final answer. The next result shows B, its answer, C, D and the ⚙️ line in
  order, with the final answer last and nothing of A; the cursor moves.
- First message with several old turns shows only the latest.
- Slack task: the PC prompt in between is shown, the Slack prompt is not; the pending task is cleared and the cursor
  advanced. A Slack prompt alone gives today's three-block message.
- Muted completion: no post and no cursor change; the next unmuted result covers the muted turn.
- Duplicate op: same transition again gives no second post. `post_result` retried with the same op reuses the post
  and returns the same cursor. An older cursor is never written over a newer one.
- Budget: 60 turns since the cursor plus a ~35 000-char final answer. The final answer is present, ≤ 50 blocks,
  sections ≤ 3000, total text within budget, the "not shown — View full" line and the button. The uploaded `.md` has
  every prompt/answer untruncated and the final answer's end.
- `result_history_blocks` unit: 200 huge items plus a long event stay ≤ 50 blocks; layout order of label / recap /
  body / button.
- `handle_resume` posts through the same history path and commits the cursor.
- Codex and a Claude agent without a transcript: the blocks equal those of the pre-history path, and no cursor is
  stored.

## Decisions / deviations

- **The final answer keeps today's truncation** (`result_blocks`: head kept up to `RESULT_MAX_CHARS`, then [View full]).
  The spec says "tail-truncated if needed" but also "keeps today's guarantees". I kept today's behaviour so the main
  body is unchanged.
- **Answer label line.** When history is shown, a `🤖 name · HH:MM` context line sits right above the recap/final
  answer, so the answer is not read as part of the previous history item. Header and context are unchanged.
- **Absolute times** in thread messages (see above); the modal keeps relative times.
- **Cursor = the finished answer's turn** (`id` + `at`). If the result came from the screen (JSONL answer missing, or
  the `since` check failed), the cursor is not moved, so the next JSONL-backed result also covers that turn. This
  repeats at most that one turn and loses nothing.
- **Matching the answer by text** (newest assistant turn with the same text): `agent_result` gives no record id.
  Prompts typed after the finish but before the read are outside the range and covered next time.
- **Time anchor rule** for the timestamp fallback, as described above. It is the safest reading of "timestamp fallback
  impossible".

## Review fixes

Review: `docs/review/thread-history.md` (0 blockers, major T1, minors N1/N2, one observation). Everything is fixed in
one commit, which also adds the review file and this done file. **832 passed / 5 skipped** (826 before; +6 tests in
`tests/test_thread_history.py`; one unit assertion changed for N1). Not pushed.

- **T1 — the cursor belongs to the message actually posted.** `post_result` computes the range once. The cursor goes
  into the payload of the op's post intent (`post_intents[op]["payload"] = {"history_cursor": …}`), which is persisted
  before the post. The new `_post_tracked(...)` (wrapped by the unchanged `_post`) returns
  `(ts, payload, reused)`:
  - An accepted intent (`ts` set) or a post found by `find_message` is reused, and the **stored** payload is
    returned, never the current attempt's.
  - A reused intent without a payload (older intent) returns `{}`, so the cursor stays.
  - If the lookup finds nothing and this attempt posts again, the intent's payload is first replaced by this
    attempt's.
  - A reused cursor is committed only if it is not behind the stored one (`newer_cursor` against fresh state).

  Callers are unchanged: they commit whatever `post_result` returns, together with the op. Regression tests:
  - uncertain post (`Accepted` fault) → PC turn → later transition reusing `result:T1`. The cursor is the Slack
    answer, and the next result contains the PC follow-up;
  - a reused op after a "lost" commit with a new turn in between returns the first cursor; an old intent without a
    payload returns `{}`;
  - resume after a crash between post and commit (in-process intents cleared): `handle_resume` reuses the post and
    commits its cursor, and the next result covers the turn typed before the restart.

  These three tests fail when the reuse path returns the fresh payload (checked by temporarily reverting it).
- **N1.** A cursor at or after the finished answer (same id, or `at >= answer.at`) gives a range of just the answer
  with `advance=False`. `post_result` then posts today's plain result and leaves the cursor as it is. Test: a status
  flap with no new turn posts the three-block result without the earlier prompt; the cursor is unchanged.
- **N2.** `newer_cursor` never lets a cursor without a timestamp replace a timestamped one. `select_range` decides by
  turn order instead (`HistoryRange.advance`):
  - placed by id or by time → advance;
  - unplaced → only a timestamp comparison can allow it.
- **Observation.** `post_result` reads the conversation with the result's 8 MB tail
  (`claude_session.TAIL_BYTES`); the send modal keeps 4 MB. Test: a spy checks the `max_bytes` passed.
