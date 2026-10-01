# Send-modal conversation history + English UI — done

Spec: `docs/progress/HISTORY-EN.md`. Both parts done on `main`, not pushed. Order as requested: B first, then A.

## Commits

| Commit | Part | Tests before commit |
|---|---|---|
| `f245cd1` English UI strings | B | 779 passed / 5 skipped |
| `8836cc3` Send modal shows the conversation history | A | 795 passed / 5 skipped (+16 new in `tests/test_conversation.py`) |

Baseline before any change: 779 passed / 5 skipped. The live bridge and Slack were not touched.

## B. English UI (`f245cd1`)

- `blocks.py`: preview notes (`…(earlier part omitted)`, `Loading…`, `No messages yet`, `Couldn't load the conversation`,
  `Working — no answer yet`, `Pick an agent to see the conversation here`), Home buttons `➕ New Agent` / `📤 Send` /
  `🔄 Refresh`, row button `Send`, overflow `{n} more agents (see the \`list\` command)`, header `Last response`,
  both `View full` buttons.
- `bridge.py`: relative time `just now` / `N min ago` (older than an hour: `%m-%d %H:%M`, as before).
- Comments: `results.py`, `slack_manifest.py`, `.env.example`, `blocks.py` docstrings.
- Tests updated: `test_blocks.py`, `test_home.py`, `test_send_preview.py`.
- README.md: dropped the "labels are in Korean" note, uses `[Send]`, `[➕ New Agent]`, `[📤 Send]`, `[🔄 Refresh]`,
  `[View full]`. README.ko.md: Korean prose, English button labels (also in one screenshot alt text).
- Final grep for `[가-힣]` in `herdr_slackbot/`, `scripts/`, `herdr-plugin.toml`, `.env.example`: no hits. Korean remaining
  in `tests/` is test data (screen transcripts, prompts, names, unicode input), not UI labels.

## A. Conversation history (`8836cc3`)

- `claude_session.py`
  - `Turn(role, text, at)`, `Conversation(turns, truncated)`, `CONVERSATION_TAIL_BYTES = 4 MB`.
  - `conversation_from_lines(lines)`: a turn starts at a real prompt (`_is_prompt`, plus the prompt must have visible text
    once `<system-reminder>…</system-reminder>` and `<command-…>/<local-command-…>` blocks are removed — a reminder-only
    user record therefore does not split a turn). Images → `[image]`. The answer is the text of the turn's last
    assistant message (same rule as `evaluate_lines`): none if the turn was interrupted, the final message is/contains
    `tool_use`, or — for the latest turn only — the message has no terminal `stop_reason` yet (still streaming).
    Thinking is ignored; sidechains and malformed records are skipped.
  - `conversation(session_id, cwd, base, max_bytes)`: reads the tail via a new `_read_tail` (which also reports whether
    the read started mid-file → `truncated`); `read_tail` keeps its signature. Returns None on missing file / read errors.
- `blocks.py`
  - `conversation_blocks(turns, agent_label, now, omitted=, note=)`: `👤 You · <time>` / `🤖 <name> · <time>` context line
    + mrkdwn section per message; newest-first fill within `PREVIEW_MAX_BLOCKS = 40` and `PREVIEW_MAX_TOTAL_CHARS =
    12000`; answers keep the tail (`tail_excerpt`, `PREVIEW_CHARS` 2500, cut mark), prompts keep the head
    (`head_excerpt`, `PREVIEW_USER_CHARS` 1500, `…(rest omitted)`); every section ≤ 3000 after escaping. Top line:
    `_… N earlier messages not shown_` (`N+` when the tail was also cut) or `_… earlier messages omitted_`. Note line
    for working/blocked at the bottom. Block ids `preview_0..n` (no collision with `target`/`prompt` or the old
    `preview_head`/`preview_body`).
  - `relative_time(at, now)` helper (used by both paths) and `head_excerpt`.
- `bridge.py` `preview_blocks`: Claude agent with a session id → `conversation()`; if it has turns, render the history
  (with `PREVIEW_WORKING` / `PREVIEW_BLOCKED` note for those statuses). Otherwise (Codex, no session file, empty
  transcript, exception) the previous behaviour is unchanged, including the busy note for working/blocked agents.
  Nothing in the update path (`gen`, `sel_ts`, hash retry, `_send_lock` sections) was changed; the JSONL read happens in
  the preview worker, outside the lock, like the previous result read.
- Tests (`tests/test_conversation.py`): synthetic JSONL (tool calls/results, sidechain, reminder-only record, meta,
  slash-command and local-command records, image prompt, interrupted turn, streaming last answer), the real fixture,
  garbage/partial lines, cut-tail flag, block order/ids/labels, many-turn budget, char budget with huge prompt + huge
  answer, one huge fenced answer, note/omitted lines, whole send view ≤ 100 blocks with unique ids, verbatim mrkdwn,
  bridge integration (history shown, busy notes, Codex / missing / empty / broken transcript fallbacks). Existing
  send-preview race tests (S1/S2 etc.) pass unchanged.
- Docs: README.md / README.ko.md describe the history; `docs/LIVE_TEST.md` send-modal checks updated.

## Deviations / decisions

- **README history text went into commit A**, not B: B was committed first and describing a feature that did not exist
  yet in that commit would have been wrong. B only changed labels.
- **Extra docs touched**: `docs/LIVE_TEST.md` and `docs/SPEC.md` still had Korean button labels (`[전체 보기]`, `[보내기]`,
  …); switched to the English labels in commit A (not listed in the spec, harmless).
- **Interrupted / unanswered turns have no marker** (the optional `_(interrupted)_` was skipped): the prompt simply has no
  answer below it. For working/blocked agents the note line covers the current turn.
- **Empty transcript → fallback**: a session file with no prompts uses the old last-response path (safest; same output
  as before).
- **Older turns without a `stop_reason`** are accepted as answered when a later prompt exists; only the latest turn
  requires a terminal stop reason. A file caught mid-append is not retried (the garbled last line is just skipped) —
  the preview is informational and the next selection re-reads it.
- The spec file `docs/progress/HISTORY-EN.md` was committed with A; this done file is left uncommitted.

## Review fixes

Review: `docs/review/history-en.md` (0 blockers, majors H1/H2, minors N1–N4). One fix commit:
`a269893` Send-modal history: fix review findings H1, H2, N1-N4 — **808 passed / 5 skipped** (795 before; +13 tests in
`tests/test_conversation.py`, all synthetic records, nothing copied from real transcripts). Not pushed. The review file
was committed with the fix; this done file stays uncommitted.

All changes are in `claude_session.py` (conversation section) and `blocks.conversation_blocks`; `evaluate_lines`
(completion results) and the send-modal update/race path are unchanged.

- **H1 — queued prompts.** A non-sidechain, non-meta `attachment` with `attachment.type == "queued_command"` and
  `commandMode == "prompt"` now starts a turn (text from `attachment.prompt`, str or text/image blocks; record
  `timestamp`). Each queued prompt is its own sub-turn, so the final answer goes under it and the interrupt rule
  applies per sub-turn (ESC + queued message no longer drops the real answer). Other command modes are ignored.
  Dedupe: when a later typed `user` record has the same text and is linked by `source_uuid` (its `uuid`,
  `source_uuid` or `sourceUuid` equals the attachment's `source_uuid`), the later copy is not shown again but stays a
  turn boundary and its answer is still shown. Tests: queued without / with a preceding interrupt, block prompt,
  task-notification mode, duplicate.
- **H2 — task notifications.** A `user` record with `origin.kind == "task-notification"`, or a text starting with
  `<task-notification>` (older versions), is a turn boundary rendered as one context line
  `⚙️ _Background task finished: <summary>_` (summary from `<summary>`, ≤ 200 chars; no raw tags). Other
  `promptSource == "system"` records become `⚙️ _System message_`. The agent's reaction is shown as an answer after
  the line; the previous turn keeps its own answer. Events are a new `Turn.role == "event"` and take one block in the
  budget. Tests: shape with origin, tag fallback, system source, rendering, budget.
- **N1.** `isCompactSummary` / `isVisibleInTranscriptOnly` records are a boundary rendered as
  `⚙️ _Conversation compacted (earlier messages summarized)_` (an event rather than the omitted line, so it sits at the
  right place in the timeline). Bash-mode records (all text starting with `<bash-input>` / `<bash-stdout>` /
  `<bash-stderr>`) are skipped and are not turn boundaries (they never get a model answer).
- **N2.** Only closed `<system-reminder>…</system-reminder>` blocks are stripped (the `|\Z` alternative is gone). Whether
  a record starts a turn no longer depends on the stripped text: a record is skipped only when every block is text made
  solely of closed reminder blocks; a prompt left empty by stripping falls back to its raw text. Tests: unclosed tag
  at the start, inline mention, trailing closed reminder, reminder-only record.
- **N3 — implemented, with fallbacks.** `_live_branch` walks from the latest `user`/`assistant` record via `parentUuid`
  (`logicalParentUuid` when `parentUuid` is null, i.e. across a compact boundary) and returns the on-chain uuids.
  Linear order is kept (nothing hidden) whenever the chain cannot be trusted: any `user`/`assistant`/`attachment` record
  without a uuid, a cycle, a non-string parent, or a parent missing from the tail. A missing parent is accepted
  only when the tail was cut (`tail_cut`, passed by `conversation()`) **and** no turn starts before the record where
  the chain leaves the tail. When the chain is trusted, only typed `user` prompts off the chain are hidden, together
  with their answer. They still end the previous turn's span, so an abandoned answer never lands under a live prompt.
  Queued prompts and events are never hidden by the chain, because their place in the tree could not be verified (see
  below). Tests: rewind/edited prompt, compact boundary via `logicalParentUuid`, broken parent (complete and cut file),
  missing uuid, cycle, cut tail leaving before the first turn, linked queued prompt + event kept. The real fixture
  (`claude_session.jsonl`, trimmed, parents missing) falls back to linear order and still gives the same turns.
- **N4.** README.md overflow text is now `"N more agents (see the \`list\` command)"`; README.ko.md got the same label.
  A test checks that README.md has no Hangul except the `한국어` language link.

Limits / decisions:

- I tried to check real record shapes (attachment keys, whether attachments sit on the `parentUuid` chain) with a
  read-only key/count probe over `~/.claude/projects`. The permission classifier denied it, so the shapes come only
  from the review text. Because of that:
  - the dedupe link is matched on any of `uuid` / `source_uuid` / `sourceUuid`;
  - queued prompts and events are kept even off-chain. In a rewound branch that contains a queued prompt, that prompt
    (and its answer) can still show up — the same as before the fix, never a hidden live message.
- `evaluate_lines` still treats task notifications as prompts (pre-existing, out of scope, as the review notes).

## Re-check fixes

Re-check: `docs/review/history-en-recheck.md` = PASS with minor observations O1–O3. All three are addressed in one
commit (this file and the re-check file are committed with it). **812 passed / 5 skipped** (808 before; +4 synthetic
tests in `tests/test_conversation.py`). Not pushed.

- **O1** — no behaviour change. `_mark_duplicates`' docstring now says the `source_uuid` link is speculative: real
  `source_uuid` values point at `queue-operation` records, so the guard has never been seen to match.
- **O2** — the raw-text fallback in `_prompt_text` still removes closed `<system-reminder>` blocks. `_reminder_only` is
  replaced by `_bookkeeping_only`: a record with no image whose text blocks are all empty or `<command-…>` /
  `<local-command-…>` bookkeeping once reminders are removed is not a prompt and does not start a turn. Side effect: a
  whitespace-only typed record no longer starts an empty turn.
- **O3** — `_mark_abandoned` hides an off-chain typed prompt only when its own ancestry (`parentUuid`, or
  `logicalParentUuid` where `parentUuid` is null; shared `_parent_of`) reaches the live chain, i.e. a real fork. A
  prompt on a separate parentless root, or whose ancestry leaves the tail or loops, stays in linear order (shown). The
  walk is memoized, so it stays linear in the number of records. As a result, the 3 unanswered prompts on a separate
  `hook_success` root that the re-check found in real data are now shown again, as unanswered prompts.
