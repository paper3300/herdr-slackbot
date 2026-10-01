# Completion messages show the conversation since the previous notification

Status: spec (2026-10-01). Builds on the send-modal history (`claude_session.conversation`,
`blocks.conversation_blocks`, docs/progress/HISTORY-EN*.md).

## Decisions (user, 2026-10-01)

- The ✅ completion/result message in an agent's DM thread shows **the conversation since the
  previous notification posted in that thread**, not only the last answer. That includes
  prompts typed on the PC, prompts queued while the agent was working, the intermediate answers
  and the final answer, plus `⚙️` event lines (background task finished, compacted). Over time
  the thread reads as the whole conversation without repeating anything.
- blocked (dialog) messages stay as they are (no history). The next completion message covers
  what happened meanwhile.

## Behaviour

- Source: Claude session JSONL via the existing conversation parser (same filtering: no tool
  calls/results, no reminders, abandoned branches hidden, ...). Codex / no session file / parse
  failure / empty → today's result message, unchanged.
- Cursor: per thread entry, persist a cursor (e.g. `history_cursor`) that identifies the last
  conversation item already covered by a posted message: the JSONL record `uuid` of that turn
  (add a stable id to `Turn`, e.g. the record uuid), with its timestamp as a fallback when the
  uuid is not in the tail any more. Advance it only after the result message was posted (same
  `_commit`/op idempotency as `last_result_seq`). Muted/skipped notifications do not advance it.
- Range = turns after the cursor up to and including the finished turn. No cursor yet (first
  message in the thread, older state files, cursor not found in the tail and timestamp
  fallback impossible) → only the latest turn (its prompt(s) + answer), never the whole
  session.
- Slack-originated prompts: the prompt is already visible in the thread (the user's thread
  reply / the send confirmation root). Do not show it again: skip the user turn whose text
  equals the pending task's prompt (normalized whitespace). Other prompts in the range are
  shown.
- The final answer stays the main body and keeps today's guarantees: it must always be shown
  (tail-truncated if needed), `[View full]` when truncated, recap line, header/context unchanged.
  Earlier items in the range are rendered above it (`👤 You · time` / `🤖 name · time` context
  line + section, events as one context line), compressed first when the budget is short:
  older items dropped with `_… N earlier messages not shown — View full_`.
- `[View full]` (.md upload) must contain the whole range (all prompts/answers/events since the
  cursor, untruncated) whenever anything was truncated or dropped.
- Slack message limits: ≤ 50 blocks per message, section text ≤ 3000, keep total text well below
  Slack's limits (e.g. same ~12 000 char budget as the modal; final answer gets priority).
  Fallback `text` stays short.
- The result for a Slack task still uses `since` (answer must be newer than the prompt) — keep
  that check; the history is additive.
- Restart/resume path (`handle_resume`) and the deferred-result paths that post results must
  use the same code (single place, e.g. inside `post_result`).

## Tests

- Synthetic JSONL: PC prompt A → answer, notification posted (cursor), then PC prompts B and
  queued C, answers → next message shows B, C, answers; nothing of A.
- First message in a thread shows only the latest turn.
- Slack task prompt not duplicated; other PC prompts in the same range shown.
- Cursor uuid missing from tail → timestamp fallback; both missing → latest turn only.
- Muted notification does not advance the cursor; duplicate op does not double-advance.
- Budget: many turns + huge final answer → final answer present, ≤ 50 blocks, sections ≤ 3000,
  View full contains the full range.
- Codex / missing transcript → previous output byte-identical.
- Existing result/notify/resume tests keep passing.

## Done

`python -m pytest -q` green (now 812 passed / 5 skipped). One commit on `main`, not pushed.
Notes in `docs/progress/THREAD-HISTORY-done.md`. Update README.md / README.ko.md /
docs/LIVE_TEST.md where results are described.
