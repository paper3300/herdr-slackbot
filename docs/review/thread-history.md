# Thread history in completion messages review

Reviewed `e8209ad` (Completion messages show the conversation since the previous notification) against
`docs/progress/THREAD-HISTORY.md` and `docs/progress/THREAD-HISTORY-done.md`. `HEAD` at review start: `e8209ad`.
Source locations refer to that commit.

**Result: 0 blockers, 1 major.** When a result post is reused through op idempotency, the cursor is recomputed from
a newer transcript read than the one the posted message showed. The cursor then jumps past conversation that was
never posted, and that part of the thread is skipped permanently. Two minors and one observation follow.

Validation: **826 passed, 5 skipped** with `python -m pytest -q`.
- Bridge-level probes ran as a scratch pytest file outside the repo. It reuses the `env` fixture, `Transcript`
  helper and fake Slack transport (including the `Accepted` "posted, response lost" fault).
- Read-only checks on the local `~/.claude/projects` transcripts (406 files) collected keys, counts and lengths only.
  No transcript content was copied into the repo.
- No live Slack or Herdr calls were made. Source and tests were not edited, and nothing was committed.

## Major

### T1 — A reused result post commits a cursor computed from a newer read: conversation is skipped

- **Location:**
  - `herdr_slackbot/bridge.py` `post_result` (`:2277`–`2320`). It always recomputes `agent_result` and
    `_history_range` and returns `{"history_cursor": span.cursor}`, even when `_post` did not post.
  - `_post` (`:479`–`507`). When the op's intent already has a `ts`, or `find_message` finds the earlier post, it
    returns that `ts` without posting the newly built blocks.
  - Callers that commit the returned cursor: `_notify_transition` (`:2262`–`2269`), `_complete_pending_now`
    (`:1489`–`1490`, inside `_retrying`) and `_settle_resumed` (`:2396`–`2397`).
- **Problem:** The message in Slack is the one built on the first attempt. The cursor committed on the reuse
  attempt comes from whatever the transcript holds at that later moment. For a Slack task the op is stable
  (`result:<task_id>`, `_result_op`), so every later path that settles the same task reuses the first post:
  - the next live transition while the task is still pending;
  - admission settle;
  - `handle_resume` after a restart.

  Any turns that finished in between are never shown, but the cursor moves past them.
- **Realistic failure:**
  1. A Slack task finishes. `chat.postMessage` is accepted, but the response is lost (timeout →
     `SlackUncertainError`). The pending task and the intent stay.
  2. The owner types a follow-up on the PC, and it finishes.
  3. That transition still carries the pending task, so it uses the same `result:T1` op. `_post` finds the earlier
     message and reuses it.
  4. `post_result` returns the cursor of the PC follow-up's answer, and the commit stores it.

  The PC follow-up and its answer are in no message, and no later message covers them. A crash between Slack
  accepting a post and `_commit`, followed by PC activity before `handle_resume`, has the same effect.
- **Reproduction:** Scratch probe with the repo's test env.
  - **Probe 1:** cursor at `a0`, pending task T1 → Slack prompt → "Slack task done". The first result post gets the
    `Accepted()` fault (`SlackUncertainError`; the post is visible in the fake Slack). Then a PC prompt "PC follow-up"
    → answer, then a working→done transition.
    - Observed: still **1** result message, `pending_task` cleared, `history_cursor = a-pc`.
    - "PC follow-up" appears in no posted message.
    - The next result (after another PC turn) does not contain it either.
  - **Probe 2:** `post_result(op="result:X")` is called and its commit "lost". A new turn is appended, and
    `post_result(op="result:X")` is called again.
    - The reuse returns cursor `a3` while the posted message only covered `two`/`second`.
    - The next message does not contain `three`/`third`.
  - The existing test `test_duplicate_op_posts_once_and_does_not_double_advance` passes only because the transcript
    does not change between the two attempts.
- **Fix direction:**
  - Tie the cursor to the content actually posted. Compute the range once and store the cursor (e.g.
    `{"history_cursor": …}`) in the post intent before posting (`_add_intent` / `_store_intent`). When `_post` reuses an
    accepted or found post, return the stored cursor instead of recomputing.
  - Simpler, safe variant: have `_post` report "reused", and let `post_result` return `{}` in that case. The cursor
    then stays, and the next result repeats at most that range instead of skipping.
  - Add a regression test for an uncertain post, a PC turn and a later transition (and for resume).

## Minor

- **N1 — A completion with no new turn repeats the latest turn's prompts and intermediate items.**
  `history.py` `_after_cursor` (`return i + 1 if i < end else None`) → `select_range` falls back to
  `_latest_turn_start`.
  - When the finished answer is the cursor itself, the range becomes the whole latest turn again: its PC prompt(s),
    queued prompts, events and intermediate answers. The unit test encodes this
    (`select_range(..., {"id": "a3", ...})` → `["C", "D", "Answer CD"]`).
  - This happens when an idle→working→idle flip produces a COMPLETED transition with a new seq but no model turn,
    e.g. a local slash command, or a status flap while the answer is unchanged. Before this commit, such a flip
    re-posted only the final answer. Now it also re-posts "👤 You: …" and the rest of the turn, contrary to "never
    repeating".
  - Probe: one PC turn, then a status flap with no new turn gave a second message that again shows "PC prompt one".
  - *Fix:* when the cursor id equals the final answer's id (or `cursor.at >= final.at`), use an empty history
    (today's plain result) and keep the cursor.
- **N2 — `newer_cursor` lets a cursor without a timestamp replace a timestamped one** (`history.py:newer_cursor`).
  If the new `at` is None (a record without `timestamp`), the check returns True. A later post could then move the
  cursor "back" in id terms. Locally, every matched answer had a timestamp (359 of 359), so this is a hardening nit.
  *Fix:* never replace a timestamped cursor with an untimestamped one, unless the ids show it is newer.

## Observation

- **The answer is not found when its prompt lies beyond the 4 MB conversation tail.** The result uses an 8 MB tail
  (`TAIL_BYTES`), the conversation 4 MB. In 1 of 360 local files with a JSONL answer, the latest prompt sits outside
  the 4 MB tail. `select_range` then returns None: the plain result is posted and the cursor stays. That is safe (at
  most a repeat next time) and needs no change. Optionally read the conversation with the same 8 MB tail in
  `post_result`.

## Requested checks

| Area | Status / evidence |
|---|---|
| **Cursor: consecutive notifications** | **Pass**, except after a reused post (T1). The range runs from the turn after the cursor to the finished answer. Turns typed after the finish are left for the next result. The cursor advances only through the caller's `_commit` with the op. |
| **Cursor: mute** | **Pass.** A muted completion never calls `post_result`, so the cursor stays and the next result covers the muted stretch (test + code). After a very long muted stretch, the cursor can leave the 4 MB tail with no anchor; the result then shows only the latest turn, as the spec allows. |
| **Cursor: duplicate op** | **Partial (T1).** A duplicate transition (same seq) returns before posting. A reused op posts once, but its cursor comes from a fresh read. |
| **Cursor: restart/resume** | **Partial (T1).** `handle_resume` → `_settle_resumed` uses the same `post_result`, and the cursor is committed with the op. A reused post after a crash between accept and commit can skip turns. |
| **Cursor: re-keyed provisional threads** | **Pass.** `rekey_thread` merges `{**existing, **entry}`, so a cursor on the session entry survives the move. Provisional entries have no transcript (session unknown), so they get no history. `handle_resume` re-reads the entry after the re-key. |
| **Matching the finished answer** | **Pass on real data.** In 359 of 360 local files with a JSONL answer, `select_range(turns, res.text, None)` found the **newest** assistant turn, with the same `at` as `answered_at` and a record uuid. The remaining file is the tail-size case above. Identical final texts occur in 1 file and resolve to the newest, which is correct. With queued prompts, the answer is the last sub-turn's, the same message `evaluate_lines` returns. An interrupted latest turn gives no JSONL answer, so the screen result is posted with no history and the cursor stays. A cursor answer hidden later (abandoned branch) falls back to the anchored timestamp lookup. |
| **Slack limits** | **Pass.** `room = MAX_BLOCKS − len(base) − 3` bounds the message at 50 blocks, and sections are ≤ 3000. I placed a cursor at every earlier answer of every local transcript and built `result_history_blocks`. Results: at most **50** blocks (exactly at the limit, never over), largest section 2 893 chars, total text ≤ 12 525 chars (context labels are outside the 12 000 body budget), fallback `text` unchanged and short. |
| **View full content** | **Pass.** With history, `[View full]` uploads `range_markdown(span.turns)`: every prompt, answer and event of the range, untruncated, including the skipped Slack prompt and the whole final answer. It is offered whenever the answer was truncated or a history item was cut or dropped. Largest `.md` from the local data: 74.5 KB. |
| **Slack-prompt skip** | **Pass.** Every Slack-originated prompt (thread reply, send modal, `/herdr new` first prompt, deferred prompt) goes through `send`, which stores `truncate(_norm(text), 300)`. The same normalisation is applied to the transcript text. The local JSONL never stores `[Pasted text #N]` placeholders: 0 of 1 211 prompt records, 408 of them multi-line. So the comparison runs on the full typed text. Only the last matching user turn is dropped, and a range left empty posts today's message. |
| **Regressions: Codex / no transcript / screen fallback** | **Pass.** `res.source != "jsonl"`, `kind != claude`, a missing or empty conversation, or an exception all fall back to the unchanged `result_blocks` path, byte-identical per the parametrized test. The `since` check for Slack tasks is untouched. |
| **Regressions: result/notify/resume paths** | **Pass.** The `last_result_seq` / `pending_task` commits keep their shape, with the cursor merged in. Blocked messages are unchanged. The extra 4 MB read happens in the same worker/notifier context as the existing 8 MB result read. |
