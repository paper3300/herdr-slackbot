# Send-modal conversation history + English UI

Status: spec (2026-10-01). Two independent changes; implement both, one commit each.

## A. Send modal shows the conversation, not only the last response

Today `/herdr send` (and the Home tab Send buttons) show the agent's **last response** between
Agent and Prompt (`Bridge.preview_blocks` → `claude_session.agent_result` →
`blocks.last_response_blocks`). Change it to show the **conversation so far** (user prompts and
the agent's answers, in order).

### Source
- Claude with a session id: read the session JSONL (`claude_session.find_session_file`).
  New pure function in `claude_session.py`, e.g.
  `conversation_from_lines(lines) -> list[Turn]` where `Turn(role: "user"|"assistant", text, at)`.
  - user turn = a real prompt record (reuse `_is_prompt`; skip tool results, meta/command
    records, interrupts, sidechains, `isMeta`, local-command output, system reminders).
    Text only (string content or `text` blocks); images → `[image]`.
  - assistant turn = the **final text** of each answered turn (same rule as `evaluate_lines`:
    text blocks of the last assistant message of the turn; skip tool_use-only messages and
    thinking). One assistant entry per user prompt; an interrupted/unanswered turn just has
    no assistant entry (optionally a short `_(interrupted)_` / `_(no answer yet)_`).
  - Read a larger tail than result reading (e.g. 4 MB constant); a partial first line is
    dropped as today. If the tail cut off the start, show "earlier messages omitted".
  - Prompts typed on the PC are included too (the session is the whole conversation).
- No JSONL (codex, missing session file, parse failure): fall back to today's behaviour
  (last response via screen parser) — unchanged output.

### Rendering (`blocks.py`, new `conversation_blocks(...)`, replaces `last_response_blocks` use in the send modal)
- Chronological, oldest at top, newest right above the Prompt input.
- Each message: a small context line (`👤 You` / `🤖 <agent name or kind>` · relative time),
  then a section with the text (mrkdwn via `to_mrkdwn`, as today).
- Slack limits: modal ≤ 100 blocks, section text ≤ 3000 chars, whole view payload must stay
  reasonable. Budget: newest-first fill up to ~40 blocks and ~12 000 chars total (constants);
  each message truncated individually (user prompts: keep the head; assistant answers: keep
  the tail like `tail_excerpt`, with the cut mark). Older turns that don't fit → one context
  line at the top: `_… N earlier messages not shown_`.
- Keep the existing fixed block ids that matter for state preservation (`BLOCK_TARGET`,
  `BLOCK_PROMPT`); preview blocks need unique ids (e.g. `preview_0..n`) and must not collide.
- Working/blocked agent: show the history that exists plus a note line
  ("Working — the current turn is not finished yet" / "Waiting for an answer on a dialog"),
  instead of only the busy note. (Sending is still rejected for those states as today.)
- Keep all existing race protections of the send-modal update path (generation counter,
  `sel_ts` ordering, hash retry rules, short `_send_lock` sections, no I/O under the lock) —
  see docs/review/send-preview*.md. Only the content of the preview changes.

### Tests
- Synthetic JSONL fixture with several turns incl. tool calls, tool results, a sidechain,
  an interrupted turn, a meta record, an image prompt → expected turn list.
- Budget/truncation tests (many turns, one huge answer, block count ≤ 100, sections ≤ 3000).
- Codex/no-session fallback still produces today's blocks.
- Existing send-preview race tests keep passing.

## B. English UI strings

All user-visible Slack text produced by the bot must be English. Current Korean strings
(found by grepping `[가-힣]` in `herdr_slackbot/`):

| Where | Korean | English |
|---|---|---|
| blocks.py PREVIEW_CUT_MARK | …(앞부분 생략) | …(earlier part omitted) |
| PREVIEW_LOADING | 불러오는 중… | Loading… |
| PREVIEW_EMPTY | 아직 응답이 없습니다 | No messages yet |
| PREVIEW_FAILED | 응답을 불러오지 못했습니다 | Couldn't load the conversation |
| PREVIEW_BUSY | 작업 중입니다 — 아직 응답이 없습니다 | Working — no answer yet |
| PREVIEW_PICK | 에이전트를 고르면 마지막 응답이 여기에 표시됩니다 | Pick an agent to see the conversation here |
| Home buttons | ➕ 새 에이전트 / 📤 보내기 / 🔄 새로고침 | ➕ New Agent / 📤 Send / 🔄 Refresh |
| Home row button | 보내기 | Send |
| Home overflow | 외 {n}개 에이전트 (목록: `list` 명령) | {n} more agents (see the `list` command) |
| header | 마지막 응답 | Last response (fallback path) |
| 전체 보기 buttons | 전체 보기 | View full |
| bridge.py relative time | 방금 / N분 전 | just now / N min ago |

- Also fix comments/docstrings mentioning these labels (`results.py`, `slack_manifest.py`,
  `.env.example`), and grep once more for any remaining Korean in `herdr_slackbot/`,
  `scripts/`, `herdr-plugin.toml`, `.env.example` (Korean inside **parser regexes / test
  fixtures that match Claude/Herdr screen output** must stay if it is matching data, not UI).
- Update tests that assert the Korean labels.
- README.md: drop the "UI labels are currently in Korean" note and use the English labels
  (`[Send]`, `[➕ New Agent]`, `[View full]`, …); describe the conversation history in the
  send modal. README.ko.md: keep Korean prose but use the English button labels and describe
  the history. Screenshots stay as they are (note nothing).

## Done criteria
- `python -m pytest -q` all green (currently 779 passed / 5 skipped).
- Two commits (A, B) on `main`, not pushed. Write notes to `docs/progress/HISTORY-EN-done.md`.
