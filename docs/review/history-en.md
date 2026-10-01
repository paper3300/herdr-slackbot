# Send-modal conversation history + English UI review

Reviewed `f245cd1` (English UI strings) and `8836cc3` (send modal shows the conversation history) against
`docs/progress/HISTORY-EN.md` and `docs/progress/HISTORY-EN-done.md`. `HEAD` at review start: `8836cc3`. Source
locations refer to that commit.

**Result: 0 blockers, 2 majors.** Both majors are transcript-parsing gaps that make the history attribute text to the
wrong speaker on real Claude Code transcripts: prompts typed while the agent was working are dropped, and background
task notifications are shown as the owner's own prompts.

Validation: **795 passed, 5 skipped** with `python -m pytest -q`. Additional probes used scratch scripts outside
the repo. They ran `conversation_from_lines` / `conversation_blocks` / `send_view` on synthetic records and, read-only,
on the local `~/.claude/projects` transcripts (406 files, 1 297 prompt records). Only record types, flags and counts
were collected. No transcript content was copied into the repo. No live Slack calls or Herdr commands were made, and
source and tests were not edited.

## Major findings

### H1 — Prompts typed while the agent is working are missing; the answer is attributed to the earlier prompt

- **Location:** `herdr_slackbot/claude_session.py:344`–`359` (`conversation_from_lines`, turn starts at `:351`),
  relying on `_is_prompt` (`:145`), which only looks at `type: "user"` records.
- **Problem:** When the owner types on the PC while Claude is working, Claude Code records that message as an
  `{"type": "attachment", "attachment": {"type": "queued_command", "commandMode": "prompt", "prompt": "<text>", ...}}`
  record inside the running turn, not as a `user` prompt record. The parser ignores attachments. As a result:
  - the queued prompt never appears in the history, although the spec says PC-typed prompts are included;
  - the turn's final answer (usually the answer to the queued message) is shown under the *earlier* prompt;
  - if the turn also contains an interrupt (ESC, then the queued message is processed), `_final_text` returns None
    for the whole turn. The real final answer then disappears as well.
- **Evidence:** In the local transcripts, there are 106 `queued_command` attachments with `commandMode: "prompt"`
  (human origin), and 104 of them have no matching `user` record anywhere in the file. 55 turns contain such a
  queued prompt, and 7 of them are fully dropped because of the interrupt rule. Synthetic reproduction:
  `user "first question"` → `assistant tool_use` → `attachment queued_command "also do Y"` → `assistant "did X and Y"
  end_turn` gives `[("user", "first question"), ("assistant", "did X and Y")]`. "also do Y" is missing.
- **Failure scenario:** The owner asks Alpha to "run the tests", then types "actually, also revert the migration"
  while it runs. The send modal shows "👤 You: run the tests" followed directly by "🤖 Alpha: reverted the migration
  and …". The follow-up the owner sends from Slack is then based on a conversation that never happened.
- **Fix direction:** Treat a non-sidechain `attachment` with `attachment.type == "queued_command"` and
  `commandMode == "prompt"` as a turn boundary/user message. Take the text from `attachment.prompt` (str, or a list
  of text/image blocks) and the record `timestamp`. Skip task-notification-mode entries (see H2). Dedupe against a
  `user` record carrying the same text and `source_uuid`, which is the rare case where both exist. Apply the interrupt
  rule per sub-turn, not to the whole span. Add a fixture with a queued prompt, with and without a preceding
  interrupt.

### H2 — Background task notifications are shown as "👤 You" with raw XML

- **Location:** `herdr_slackbot/claude_session.py:145` (`_is_prompt`), `:305`–`317` (`_prompt_text`); rendering
  `herdr_slackbot/blocks.py:514`.
- **Problem:** When a background task (Bash `run_in_background`, background agent, Monitor …) finishes, Claude Code
  appends a `type: "user"` record whose content is `<task-notification>…</task-notification>`. The record has
  `origin: {"kind": "task-notification"}` and `promptSource: "system"`, and it is not `isMeta`. `_is_prompt` accepts
  it, and `_prompt_text` keeps the text because only `<command-`/`<local-command` prefixes and system reminders are
  removed. The history renders it as the owner's prompt, with escaped tags shown verbatim (`<task-notification>
  <task-id>…`). The agent's reaction to the notification is shown as the answer to it.
- **Evidence:** In the local transcripts, 194 of 1 297 accepted prompt records (15 %) are task notifications.
  Synthetic reproduction gives `[…, ("user", "<task-notification>\n<task-id>x…"), ("assistant", "bg done: result")]`.
- **Failure scenario:** An agent that uses background shells shows several "👤 You" messages full of
  `<task-id>`/`<status>` markup that the owner never wrote. These messages use up the block/char budget, push real
  prompts out of the visible window and misstate who said what.
- **Fix direction:** Recognise these records by `origin.kind == "task-notification"` / `promptSource == "system"`, or
  by a leading `<task-notification>` tag as a fallback for older versions. Keep them as a turn boundary, so the
  previous answer is still the previous turn's answer. Do not render them as "You": either skip them or render one
  short context line (e.g. `⚙️ background task finished` plus the `<summary>` if present). Add a fixture.
  (`evaluate_lines` also treats them as prompts. That is pre-existing and arguably correct for completion results,
  so it is out of scope here.)

## Minor findings

- **N1 — Other non-human user records shown as "👤 You"** (`claude_session.py:145`, `:305`). Compact summaries
  (`isCompactSummary: true` / `isVisibleInTranscriptOnly: true`, "This session is being continued from a previous
  conversation…") appear as a 1 500-char "You" message (5 in the local data). Bash-mode records
  (`<bash-input>`/`<bash-stdout>`) appear with raw tags. *Fix:* skip `isCompactSummary` / `isVisibleInTranscriptOnly`
  records, which could become a `_… earlier messages omitted_` line. Render or skip `<bash-*>` records explicitly.
- **N2 — An unclosed `<system-reminder>` in a typed prompt swallows the rest of it** (`claude_session.py:289`, the
  `|\Z` alternative). A prompt that mentions the tag, e.g. "why does `<system-reminder>` appear?", is cut to
  "why does \`". If the tag starts the prompt, the prompt becomes empty: it no longer starts a turn, and its answer
  replaces the previous turn's answer (synthetic: `q1/a1`, `"<system-reminder> tags: why…"/a2` gives
  `[("user","q1"), ("assistant","a2")]`). *Fix:* strip only closed `<system-reminder>…</system-reminder>` blocks, or
  only whole text blocks that start with the tag. Never let reminder stripping decide whether a record is a turn
  boundary.
- **N3 — Abandoned branches are shown** (`claude_session.py:344`, plausible, not fully verified). Transcripts are a
  `parentUuid` tree. A rewind / edited prompt leaves the old branch in the file, and the linear parse shows both
  branches. Locally, 66 of 1 297 prompts in 36 of 406 files are not on the `parentUuid`/`logicalParentUuid` chain of
  the latest record. This count may include chain breaks that are not rewinds. *Fix:* walk the chain back from the
  latest user/assistant record and keep only on-chain records. If the chain is broken inside the tail, fall back to
  linear order.
- **N4 — Stale Korean in the English README** (`README.md:287`): "the rest are collapsed into "외 N개" (N more)". The
  Home overflow line is now "`{n} more agents (see the `list` command)`". *Fix:* use the English text.

## Requested checks

| Area | Status / evidence |
|---|---|
| JSONL turn parsing on real transcripts | **Fails on H1/H2, minors N1–N3.** Otherwise the evaluate_lines rule holds on the real shapes: one line per content block sharing `message.id`, `text` lines with `stop_reason: tool_use` before a tool call, thinking-only lines and `end_turn` text. Tool results, interrupts, meta/command records and attachments other than `queued_command` are correctly ignored. Synthetic API-error assistant records (`<synthetic>`, `stop_sequence`) are shown as an answer, which is acceptable. |
| Block Kit limits | **Pass.** On the 25 largest local transcripts (3.2–17 MB), the whole send view had at most 42 blocks (≤ 40 preview), the largest section was 2 483 chars, JSON was ≤ 42 KB, and block ids were unique. Escape-heavy probes (`&`×5000 head, `<`×5000 tail) stayed at 2 471 / 2 644 chars. Budget arithmetic: ≤ 1 + 2·18 + 1 preview blocks with a note, ≤ 12 000 + one ≤ 3 000 message chars. Context lines escape the agent label. Mrkdwn is `verbatim: true`. `preview_N` ids do not collide with `target`/`prompt`/`preview_head`/`preview_body`. |
| Send-modal race protections | **Pass, unchanged.** `update_send_modal`, `_apply_send_update`, `_load_preview` and `submit_send_view` are untouched by `8836cc3`. The JSONL read runs in `preview_blocks` inside the worker, outside `_send_lock`, after the `_is_current` early-out and before the `gen`/`target` recheck. The existing send-preview race tests pass. |
| 4 MB tail read performance | **Pass.** Read ≤ 12 ms, parse ≤ 21 ms per file on the largest local transcripts. This is less than the 8 MB `TAIL_BYTES` read that `agent_result` already did for the old preview. A tail made mostly of huge lines (one 6.6 MB file: 210 lines, no prompt in the last 4 MB) yields no turns and falls back to the last-response path, which is acceptable. |
| Remaining Korean user-facing strings | **Pass for the bot.** No Hangul in `herdr_slackbot/`, `scripts/`, `herdr-plugin.toml`, `.env.example`. README.md keeps the `[한국어]` language link (fine) and one stale overflow phrase (N4). README.ko.md uses the English labels with Korean prose, as specified. |
| Fallbacks | **Pass.** Codex, a missing or empty transcript and read exceptions keep the previous last-response output, including the busy note (covered by `tests/test_conversation.py`). |
