# Send-modal conversation history — recheck of the review fixes

Rechecked `a269893` (Send-modal history: fix review findings H1, H2, N1-N4) against `docs/review/history-en.md` and
the "Review fixes" section of `docs/progress/HISTORY-EN-done.md`. `HEAD` at recheck start: `a269893`.

**Result: PASS (0 blockers, 0 majors).** All six findings are fixed. The record-shape assumptions the implementer
could not check hold on real transcripts, or fail in a harmless direction. The chain walk hides no answered or live
prompt and never fell back on the real tails. Three minor observations are listed below; none blocks.

Validation: **808 passed, 5 skipped** with `python -m pytest -q`. Additional probes were scratch scripts outside
the repo. They ran the committed functions (`_turn_start`, `_live_branch`, `conversation_from_lines`,
`conversation_blocks`, `send_view`) on synthetic records. They also ran, read-only, on the local
`~/.claude/projects` transcripts: 406 files, and for the per-tail checks the same 4 MB tail `conversation()` reads,
which gives 383 tails with turns. Only keys, flags, types and counts were collected. No transcript content was copied
into the repo. Source and tests were not edited, and nothing was committed.

## Findings

| Finding | Status | Evidence |
|---|---|---|
| H1 queued prompts | **Fixed** | `queued_command`/`commandMode: "prompt"` attachments start their own sub-turn (`claude_session.py` `_queued_prompt`, `_turn_start`). Synthetic: prompt → tool_use → queued "also do Y" → answer gives `You: first`, `You: also do Y`, `Agent: did X and Y`. With ESC before the queued prompt, the real answer is kept. On the real tails, 72 queued prompts are now shown. |
| H2 task notifications | **Fixed** | Rendered as a `⚙️ _Background task finished: <summary>_` event, which is still a turn boundary. Real tails: 175 task events, all with a summary. 0 user turns contain `<task-notification>`, and 0 "System message" fallbacks occurred. |
| N1 compact / bash | **Fixed** | Compact summaries become a "Conversation compacted" event (2 in the real tails). `<bash-input>`/`<bash-stdout>`/`<bash-stderr>` records are skipped. Real tails: no user turn starts with a `<bash-` tag. |
| N2 unclosed reminder | **Fixed** | `_REMINDER_RE` strips closed blocks only. A prompt starting with `<system-reminder>` text now stays its own turn (synthetic `q1/a1`, `"<system-reminder> tags: why?"/a2` gives four turns). Reminder-only records are still skipped. Real tails: 0 user turns contain `<system-reminder>`. |
| N3 abandoned branches | **Fixed, verified on real data** | See the chain section below. |
| N4 README | **Fixed** | `README.md:287` and `README.ko.md:287` use "N more agents (see the `list` command)". A new test guards against Hangul in README.md. |

## Assumptions checked on real transcripts

**`queued_command` attachment keys.**
- Top-level record: always `uuid`, `parentUuid`, `timestamp`, `isSidechain`, `sessionId` (often also `rendered`,
  `slug`).
- `attachment`: `type`, `commandMode`, `prompt` (always a str in the local data), `timestamp`, `source_uuid`, plus
  `origin: {kind: "human"}` and sometimes `humanTurn: true`.
- Of the human prompt attachments, 106 out of 106 have `source_uuid`.
- The implementer's `_queued_prompt` reads exactly these fields. List-valued `prompt` is handled defensively.

**Dedupe link (`uuid` / `source_uuid` / `sourceUuid`).**
- `source_uuid` never equals any record's `uuid` (0 of 106).
- The only other records that mention it are `queue-operation` records with `operation: "remove"` (13).
- No `user` record has a top-level `source_uuid` or `sourceUuid` key.
- Each `source_uuid` is unique among queued attachments (0 of 154 reused), so there is no false-positive dedupe risk.
- No queued prompt has an exact-text `user` copy.
- In the rendered real tails, there are 0 adjacent duplicate user turns.
- So `_mark_duplicates` never fires on real data. It is harmless (see O1).

**Are attachments and task notifications on the `parentUuid` chain?** Every `user`/`assistant`/`attachment` record
has a non-empty `uuid` (0 missing), so the "linked record without uuid" fallback never triggers.
- **Attachments:** 41 755 are on the live chain and 17 667 are off it. Off-chain ones are mostly side leaves such as
  `hook_success`. The chain walk does not require attachments to be on it, which is correct.
- **Queued prompts:** all 72 queued prompts in the tails are on the live chain. Hiding them by chain would therefore
  also have been safe; the implementer's choice to never hide them costs nothing.
- **Task notifications:** in whole files, 195 are on the chain and 1 is off. Never hiding events is fine.

**Does the N3 chain walk fall back too often?**
- No. Of 383 tails with turns, 370 reach a root and 13 leave a cut tail before the first turn (accepted by the
  `tail_cut` rule).
- There were 0 fallbacks to linear order: no cycle, no broken parent inside a tail, no missing uuid.

**Does it hide a live prompt?** No answered or live prompt is hidden. The walk hides 46 typed prompts in the tails,
and for every one of them:
- it has **no answer** in its span;
- a **later live typed prompt** exists.

Where the branches split:
- 41 forks: the live side continues with a typed prompt, i.e. an edited prompt or a rewind.
- 2 forks: the live side continues with a system record.
- 3 prompts: they sit on a separate parentless tree in the same file (root is a `hook_success` attachment). They are
  also unanswered and followed by live prompts.

These are exactly the "prompt with no assistant reply" cases from the first review, which are now correctly hidden.

## Regression checks

- **evaluate_lines and the race path:** `evaluate_lines` / `last_answer` (completion results) and the send-modal
  update path (`update_send_modal`, `_apply_send_update`, `_load_preview`, `submit_send_view`) are untouched by
  `a269893`. `_prompt_text`'s signature change has no caller outside the conversation section (`parser._prompt_text`
  is a different function).
- **Block Kit on all real tails** (rendered with a note line): at most 42 blocks per send view, the largest
  section/context text is 2 795 chars, and block ids are unique in every view. Event lines are escaped, truncated to
  300 chars and take one block in the budget.
- **Performance:** worst read + parse + chain walk is 72 ms per tail. `_live_branch` is linear (uuid index, single
  walk).
- **Real-tail output shape:** 1 071 user, 1 110 assistant and 177 event turns. User turns start with plain text,
  `<pasted_content` (genuine typed paste wrappers) or `[`. There are 0 "two answers in a row".

## Minor observations (non-blocking)

- **O1 — Dedupe link never matches.** `claude_session.py` `_mark_duplicates`. Real `source_uuid` values point at
  `queue-operation` entries, not at any record `uuid`, so a queued prompt that Claude Code also wrote as a `user`
  record would be shown twice. This was not observed (0 exact-text copies in 406 files). *Direction:* keep it as is,
  or replace it with a text + timestamp-proximity check. If kept, note in the comment that the link is speculative.
- **O2 — The raw-text fallback can expose bookkeeping.** `claude_session.py` `_prompt_text`. A single text block
  `<system-reminder>…</system-reminder><command-name>/foo</command-name>` is a prompt (not all-command, not
  reminder-only). Stripping leaves only command text, so the raw text including the reminder is shown as "👤 You".
  There were 0 occurrences in real data (no user turn contains `<system-reminder>`). *Direction:* when falling back
  to raw text, still strip the closed reminder blocks, or treat "reminder + command bookkeeping only" as non-prompt.
- **O3 — A second parentless root would hide everything before it.** `_live_branch`. 80 of 406 tails have two roots,
  which is normal: a `hook_success` attachment root plus the first prompt. If Claude Code ever starts a new
  parentless tree after answered turns in the same file, those turns would be hidden. Real data shows only the 3
  unanswered prompts above on such a tree. *Direction:* optional. Hide only prompts whose branch meets the live chain
  (a real fork), and fall back to linear order for records on a different root.
