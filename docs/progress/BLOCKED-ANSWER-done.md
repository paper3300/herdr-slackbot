# Answer blocked dialogs from Slack: done

Spec: `docs/progress/BLOCKED-ANSWER.md`. Nothing committed (the orchestrator commits). Offline suite:
**733 passed, 5 skipped** (was 651 + 5 skipped).

## What was built

| Area | Files | What |
|---|---|---|
| Parser (§1) | `herdr_slackbot/dialog.py` (new) | `parse_dialog(screen, agent_kind)` → frozen `Dialog` / `Option` exactly as specified; `keys_for(dialog, option_or_index)`; helpers `tail_lines`, `screen_fingerprint` (keypad) |
| Slack message (§2) | `herdr_slackbot/blocks.py` | `dialog_blocks(...)` replaces `blocked_blocks`; also `dialog_closed_blocks`, `dialog_text_view` (free-text modal), `screen_blocks`, `dialog_unconfirmed_blocks`, `without_actions`, value/view parsers, action ids `dlg:opt:<i>`, `dlg:text:<i>`, `dlg:key:<key>`, `dlg:screen`, callback `herdr_dialog_text` |
| Bridge flow (§3, §5) | `herdr_slackbot/bridge.py` | posting + pending record, click / modal / thread-reply answering, confirmation polling, reconciliation on transitions, startup dialogs with the deferred first prompt |
| Slack wiring | `herdr_slackbot/slack_app.py` | one regex action handler (`^dlg:`) + the modal view handler, both behind the owner guard; ack first |
| Herdr client | `herdr_slackbot/herdr_client.py` | `send_keys(target, keys)` (`agent.send_keys`), `send_text(pane_id, text)` (`pane.send_text`, params `{pane_id, text}` checked with `herdr api schema --json`); CLI mapping `pane send-text <pane> <text>` |
| State | `herdr_slackbot/state.py` | `find_dialog(token)`; `dialog` / `deferred_prompt` documented in the layout |
| Notices (§4) | `bridge.py`, `blocks.py`, `notify.py` | `REASON_TEXT[blocked]`: "is waiting for an answer: use the buttons in its thread, or answer on PC." Help text gained a line about answering. No Home-tab text mentioned "confirm on PC". |
| Docs (§6) | `docs/SPEC.md`, `README.md`, `docs/LIVE_TEST.md` | D6/D9/header text, new "Blocked dialogs" section; README feature section incl. the Codex `approvals_reviewer = "auto_review"` note; LIVE_TEST section 7-1 |
| Tests | `tests/test_dialog.py` (new, 26), `tests/test_bridge_dialog.py` (new, 34), additions to `test_blocks.py`, `test_herdr_client.py`, `test_slack_app.py`, `fakes.py` (`send_keys` / `send_text` / `on_keys` / `on_text` / `input_error`); 3 old "confirm on PC" assertions updated |
| Fixtures | `tests/fixtures/transcripts/dialog_*.txt` (10) | synthetic copies of the spike screens: real paths replaced by a neutral path **of the same length** (so the wrapped option in the permission prompt still wraps), account/time text neutralized; plus `dialog_ask_boxed.txt` (`☐ <header>`, `│ `-prefixed two-line question, 3 options + Type something. + rule + Chat about this) |

## How it works (short)

- **Post:** on `Action.BLOCKED` (live transitions and restart resume) the bridge reads the `visible`
  screen, parses it and posts `dialog_blocks` with op `blocked:<terminal>:<seq>` (existing de-dup and
  mute unchanged). The first token is derived from the op, so an uncertain post that is reused on
  retry still matches the record. The record `dialog` in the thread entry holds token, fingerprint,
  kind, options, question, message/thread ts, agent session / terminal / pane, agent kind, name, ws,
  summary, `idle`, created_at.
- **Click:** `dialog_action` acks-first; the free-text button opens its modal with the trigger id
  from local state only. Everything else runs on a worker under the thread's **answer lock**:
  record by token → fresh `agent.get` + screen → state `open | changed | answered | gone | unreadable`.
  Only `open` sends keys. `changed` re-renders the message (note "changed on PC; nothing was sent"),
  `answered` closes it "Already answered or changed on PC", `gone` closes it "ended".
- **Keys:** numbered → digit; unnumbered → `up`/`down` from the cursor + `enter`; numbers ≥ 10 also
  navigate. Free text: digit, 0.3 s, `pane.send_text`, 0.3 s, `enter`.
- **Confirm:** poll every 0.3 s for up to 5 s. Not blocked any more → `✅ <choice> — answered from
  Slack`, buttons gone, record dropped. A changed dialog must read the **same twice** (screen lag)
  before the same message is edited under a new token. No change → buttons kept + "⚠️ Could not
  confirm the answer; check the screen." with [Show screen].
- **Transitions** take the same answer lock (order: admission lock → answer lock → per-key lock), so
  they never race a click. With a record present they reconcile it with the live agent: no longer
  blocked or a different dialog → `✅ answered on PC`; ended → `⏹ ended`. A blocked transition whose
  dialog is still the one shown only records `last_blocked_seq` (no second message).
- **Thread reply** with an open record: free-text option → answered like the modal (fingerprint check
  and polling included); no free-text option (or keypad) → "This question needs one of the buttons
  above."; agent no longer waiting → record closed and the reply goes on as a normal prompt. `/send`
  command and modal keep rejecting blocked agents with the new text.
- **Startup:** `agent start` → `agent_not_ready` and the pane's agent is blocked (or shows a trust
  screen) → thread root (same 🚀 root as a normal new agent) + dialog, prompt kept as
  `deferred_prompt`. Codex: after `agent start` returns and the agent settled, a `trust` dialog on
  screen is handled the same way with `idle: true` (the record then counts as open while the trust
  screen is shown although the status is idle). The deferred prompt is sent exactly once (popped under
  the key lock) when the dialog closes — answered from Slack or on PC (transition to idle/done) — after
  `_wait_settled`, the Codex prompt delay, and normal admission (echo `📨` in the thread). If the agent
  is blocked again at that point the prompt is kept for the next dialog.

## Deviations from the spec

1. `dialog_blocks(name, ws_label, dialog, token)` takes extra keyword-only arguments: `plan_text`,
   `full_id` ([전체 보기] result id), `screen_tail` (keypad) and `note` (e.g. "changed on PC").
2. **Free text is sent as one line** (newlines → spaces). I could not verify how `pane.send_text`
   treats `\n` without sending keys to a live agent, and an LF reaching the TUI as Enter would submit
   a partial answer. Documented in README and SPEC.
3. The pre-send fingerprint check is **skipped for keypad keys** (an unparsed screen often has a
   spinner/clock, which would make every press "stale"); the status must still be `blocked` and the
   session the same. Keypad re-renders are detected with a fingerprint of the last 15 screen lines.
4. The "changed" dialog must be seen twice before re-rendering (spec says poll until the fingerprint
   changes; this avoids rendering a half-drawn frame).
5. `parse_dialog` returns `None` (→ keypad) when numbered options are followed by more than 8
   non-blank lines (a numbered list in the transcript, not a dialog). So the "`unknown` with no
   options" branch in `dialog_blocks` exists but `parse_dialog` never produces that case: an
   unrecognized dialog *with* options is `unknown` and gets normal option buttons.
6. Plan files are read only when the path is `…/.claude/plans/*.md` (≤ 200 KB); `~` expands against
   `Bridge(home_dir=...)` (tests) or `Path.home()`.
7. CLI fallback: `agent send-keys` / `pane send-text` succeed on empty or non-JSON stdout (their
   CLI output format was not verified; see open questions).
8. Muted PC agents still get no blocked message (existing mute rule), so there are no buttons; the
   `/send` rejection therefore says "use the buttons in its thread, **or answer on PC**".

## Open questions / not verified live

- No live test was run (the writer role must not send keys). Section 7-1 of `docs/LIVE_TEST.md` covers
  every path; worth checking first: multi-select "Type something" (digit toggles *and* opens the
  input?), plan "Tell Claude what to change" via the modal, and how `send_text` handles `\n`.
- What `herdr agent send-keys` / `herdr pane send-text` print on success via the CLI transport.
- Codex trust answered **on PC**: Codex stays `idle`, so there may be no transition; then the message
  keeps its buttons and the deferred prompt waits until the next transition or a click (a click finds
  the trust screen gone → closes → sends the prompt).
- Records left from before a bridge restart are reconciled only on the next transition or click.
- Claude's AskUserQuestion shows which tab is current only by color; `title` is the first unanswered
  tab, which matched every captured screen but is a heuristic.

(Round 1 notes above. The first three open questions are answered by the orchestrator's live facts
below, and the last-but-one by N7.)

## Round 2 (review `docs/review/blocked-answer.md`, round 1: CHANGES-REQUESTED)

Suite: **765 passed, 5 skipped** (733 before this round). New tests are marked "review round 1" in
`tests/test_dialog.py`, `tests/test_bridge_dialog.py`, `tests/test_blocks.py` and `tests/test_herdr_client.py`.
The M3 and N3 regression tests were checked against the old code (temporarily reverted): both fail there.

### Orchestrator live facts applied (they override the spec and round-1 deviations)

- **(a) Newlines:** deviation 2 is reverted. Free text is sent as-is, with CRLF/CR normalized to LF and
  surrounding blank lines stripped. The answered label still shows a one-line excerpt.
- **(b) CLI output:** only `pane.send_text` may print nothing (`SILENT_CLI_METHODS`, and only when stdout is
  empty). `agent.send_keys` goes through the normal JSON envelope. Deviation 7 is narrowed to that one method.
- **(c) Multi-select:** the parser now finds the `Submit` row (`Dialog.submit_after`, `cursor_on_submit`), also
  when the cursor is on it. `dialog.text_keys()`: single-select / plan → the digit, then text, then Enter;
  multi-select → `up`/`down` from the **live** cursor onto the Type something row, then the text, and **no Enter**.
  `dialog.submit_keys()`: cursor to the Submit row + Enter. [Next →] on a multi-select question now sends
  `dlg:key:submit` (that sequence). `right` is kept only as a fallback when no Submit row was parsed.
- **(d) `--`:** not added; the text stays positional (a comment in `cli_args` and a test record why).

### Findings

| # | Status | How |
|---|---|---|
| M1 | fixed | `_dialog_gone` now checks the clicked message first (`_dialog_message_state`): the message of an open record (re-rendered under a new token) is left alone and gets "That button was out of date; nothing was sent. Use the updated buttons above.". A closed dialog message (its ts is kept in the thread entry's `closed_dialogs`, last 20, written by `_close_dialog`) keeps its outcome, and only "This question is no longer open." is posted. Only a message that was never a dialog message is stripped. Tests: stale click after a multi-select re-render keeps the new buttons; double click keeps `✅ … answered from Slack`. |
| M2 | fixed | The fingerprint payload is now kind + title + body + plan file + region lines. The plan excerpt above the rule is in `body`, and the cursor is still excluded. At post time the record also stores `plan_hash` (sha1 of the plan file). `_dialog_state` compares it, so a re-plan written into the same file with an unchanged screen still counts as `changed`. Tests: parser (different plan, different fingerprint; cursor move, same fingerprint); bridge (re-plan on screen / rewritten plan file → no keys, re-render). |
| M3 | fixed (full fix, not the smaller one) | `_open_dialog` builds text, blocks and record once per op, with a random token, and persists them as `dialog_pending` before the first attempt. Retries (the notifier's retry, `_retrying` in `_startup_dialog`, resume) post or reuse exactly that message and commit exactly that record (`dialog_pending` is cleared in the same commit). Tokens are no longer derived from the op, so an op reused without a `seq` cannot collide with an old token. Test: uncertain first post, screen changes, retry reuses the message; the record matches the posted message, and a click sends nothing and re-renders. |
| N1 | fixed | A keypad click is sent without the fingerprint check only while the live screen is still unparsed. If it now parses, the message re-renders with "changed on PC" and nothing is sent. `_open_dialog` reads an unparsed screen up to 2 more times (0.3 s apart) before it posts a keypad. |
| N2 | fixed | A numbered run counts as a dialog only when it shows a cursor (on an option or on the Submit row). The numbered run and the unnumbered menu are both evaluated, and the one ending lower on the screen wins. Trust is classified from the option labels only ("…I trust this folder", "Trust and continue"). Tests: idle screen ending in a list → `None`; menu below a list wins; trust wording in an answer is not `trust`. |
| N3 | fixed (restructured) | The transition path takes the agent's answer lock **before** the admission lock. The notifier still waits for an in-flight click on *that* agent (it is one ordered thread), but it no longer holds the admission lock while waiting, so `send()` / admission for other agents keeps working. `_startup_dialog` uses the same order (answer lock, then admission, then the key lock). The key is resolved inside, and the answer-lock id (terminal id) is known before admission. The answer worker never takes the admission lock while holding its lock. I judged this safe: every path that takes both takes answer → admission → key, and `handle_resume` / `_admit` take no answer lock. Test: a click held inside `send_keys`, its transition waiting, and a `send()` to another agent completes. |
| N4 | fixed | The answer lock is keyed by the terminal id (`_answer_id`, falling back to the thread key / pane when Herdr reports no terminal id). The click worker re-resolves its record by token after taking the lock; the thread-reply worker re-resolves by thread ts. `_close_dialog` / `_rerender_dialog` resolve the key by token, and `_send_deferred` follows a re-keyed entry by terminal id (`_key_now`). Tests: same lock id before and after the re-key; re-key while a click is polling (closed on the new key, prompt sent once); thread reply after a re-key. |
| N5 | fixed | For Codex, `codex_prompt_delay` now runs first. Then the trust screen is checked on up to 3 reads, 0.3 s apart. There is no second delay before the prompt. Test: trust screen drawn during the delay. |
| N6 | fixed | `_startup_dialog`: if the thread entry already has an open `dialog` (posted by the notifier), it only commits `deferred_prompt` / `last_blocked_seq` and keeps that message. Test with the PC path posting first. |
| N7 | fixed | `start()` / `activate_owner()` queue a `_Reconcile` item for every entry that has a `dialog` or a `deferred_prompt`. `handle_reconcile` (notifier thread, under the answer lock) closes stale dialogs and then submits `_send_deferred`. `_notify_loop` waits at most 15 s for work; when idle it queues reconciles for `idle` records (Codex trust answered on PC). `_send_deferred` logs when it takes the prompt ("dropped if the bridge stops now"). Tests: answered while down; deferred prompt after a restart; idle recheck. |
| N8 | fixed | On "could not confirm", the message re-renders with the same buttons under a **new token** and the note. The notice is posted with [Show screen] for the new token. A click queued behind the answer is now out of date (M1 path) and cannot press the key again. Every other outcome already closes or re-renders. Test: second click after a timeout sends nothing. |
| N9 | not changed, on purpose | Fact (d): the CLI types `--` literally, and text starting with `-` is typed correctly without it. Documented in `cli_args`; tested. |
| N10 | fixed | The free-text modal carries `{"c", "m", "th"}` (channel, message ts, thread ts) in `private_metadata`, so a submission for a closed dialog replies in the thread and leaves the outcome alone. `LIVE_TEST.md` 7-1: a re-plan now says the old message closes and a new one is posted. Multi-line and multi-select free-text checks were added. |
| N11 | fixed | Tests added: M1/M2/M3; restart persistence (record written by one `Bridge`/`StateStore`, clicked through a new instance); resume with an open `dialog` (commits the seq only, no second message); thread reply racing a re-key; parser lists and trust wording; Block Kit with 27 options (25 + 2 split, block ids `dialog_options`, `dialog_options1`) and a 3500-char label (section clip, button ≤ 75). |

### Deviations after this round

- 2 (flattened free text) is **reverted** by fact (a). 7 is narrowed by fact (b). 3 is narrowed by N1. 5 is
  replaced by the cursor requirement (N2); the footer limit stays.
- The answer-lock id falls back to the thread key (click / thread reply), the pane id (startup) or the session
  (transition) when Herdr reports no terminal id. These fallbacks differ, so the serialization only holds with
  terminal ids. Herdr 0.8.2 reports one for every agent.

### Still open

- Leaving the multi-select "Type something" row with `down` / `up` (for [Next →] after typing) is assumed to work
  like the other rows; fact (c) only covers moving *onto* the row and that `right` is consumed there.
- The multi-select sequences assume the cursor order: checkbox options, the Submit row, then "Chat about this"
  (as drawn on screen).

## Round 3 (review `docs/review/blocked-answer-recheck.md`, round 2: CHANGES-REQUESTED)

Suite: **774 passed, 5 skipped** (765 before this round). New tests are marked "review round 2" in
`tests/test_dialog.py` and `tests/test_bridge_dialog.py`. The two R1 restart tests fail with the round-2
code (temporarily reverted) and pass now.

| # | Status | How |
|---|---|---|
| R1 | fixed | `_reconcile_dialog(key, rec, transition=True)`. The transition path keeps closing a changed dialog (the `blocked` transition that follows posts the new one). `handle_reconcile` (restart recovery, pairing, idle recheck) passes `transition=False`: if the agent is still waiting and the dialog changed, the same message is re-rendered under a new token with `DIALOG_CHANGED_TEXT`. This includes a keypad whose screen now parses. "No longer waiting" and "gone" still close it. Tests: restart with question 1 answered on PC (the same message shows question 2 with buttons, no post is lost, a click works); restart with a keypad whose screen now parses; a transition still closes. |
| R2 | fixed with the live fact (not flattened) | Multi-line text stays as-is. New fixture `dialog_ask_multi_typed.txt`: the orchestrator's live rows, with a synthetic header. Parser changes: (1) the multi-select row above Submit is recognized **by position** as the free-text row, since after typing it shows the text, not "Type something". Its label is the typed text, its continuation lines are joined with `\n` (`hello a\nb`), and it has no description. (2) The reviewer's sturdier Submit scan: if the continuation scan stopped at a line left of the text column, `Submit` is still searched for by its text up to 6 lines below the last option. Slack shows a multi-line label on one line with `↵`. Tests: the typed fixture ('b' on option 3, Submit found, `[Next →]` = `down, enter`, no keys before more text); a second line drawn at column 0 (Submit still found); the untyped row keeps its label; bridge flow: moves, `send_text("hello a\nb")`, no Enter, re-render, `[Next →]` from the live cursor. |
| R3 | fixed | The lock-order comment in `Bridge.__init__` now says answer lock → admission lock → per-key lock. |
| R4 | fixed | `_transition_answer_id` also finds the terminal id through the pane's provisional entry (`find_provisional(t.pane_id)`) when the transition has no info (ended). Test: an ended transition of a provisional thread gets the click's lock id. |
| R5 | fixed (first bullet); second bullet accepted | `_notify_loop` runs the idle recheck when 15 s have passed since the last one, before taking the next item, so steady traffic from other agents no longer starves it. Test with a 0.05 s interval under constant queue traffic. The up to 0.6 s of re-reads in `_read_dialog` (N1) still happen in the notifier under the admission and key locks, but only for screens that do not parse. I left it because moving the read out of the locked section would split `_open_dialog`'s build-once-per-op logic (M3). |

About the README (orchestrator's question): `README.ko.md` and the English rewrite of `README.md` are not mine.
In round 1 I edited the Korean `README.md` (the dialog section, the notice texts and the Codex note). In round 2 the
file I found was already in English, and I only edited its "Answering dialogs" section. I never created
`README.ko.md` and did not touch it in this round.

## Round 4 (review `docs/review/blocked-answer-recheck2.md`, round 3: PASSED, 2 NICE-TO-HAVE)

Suite: **779 passed, 5 skipped** (774 before this round). New tests are marked "review round 3".

| # | Status | How |
|---|---|---|
| N-A | fixed | `parse_dialog(screen, agent_kind, typed_row=None)` treats the row above Submit as the typed free-text row only on **positive evidence**: `typed_row` equals that row's index. The bridge passes `_typed_row_hint(rec)`, the index of the multi-select free-text row in the record the message was built from, to every parse of an existing record (`_dialog_state`: clicks, confirmation polling, reconciles). The evidence chain starts when the row was still labeled "Type something" and is carried through every re-render. Without evidence (a fresh parse, or a row that is not above Submit) the row stays a plain toggle with its description. The fingerprint does not depend on the hint. Tests: the reviewer's probe (no Type-something row, Cherry above Submit → plain toggle, digit `3`); the typed fixture without a hint is a plain row, with a hint on the wrong row it is ignored, with the right hint it gives `hello a\nb`. |
| N-B | fixed (no clearing key) | Records now store `typed` per option (`dialog.typed_text`: the text a multi-select free-text row already holds). The free-text modal for such a row shows "This row already holds typed text. What you send is *added after it* (nothing is cleared)." plus the current text as a code block. A thread reply answered into such a row sends only the text (no Enter, no clearing key) and then posts in the thread "Your reply was *added after* the text this row already held… Current text:" with the row's text after the re-render (falling back to old text + reply). The first answer into an empty row gets no notice. Tests: modal notice with the current text (and none before anything was typed); thread-reply append notice with `hello a\nb\nmore`; no notice for the first reply. |

Test fix: the round-2 bridge test "multi-line text into a multi-select row" now starts from the untyped form of the
same layout (`ASK_MULTI_UNTYPED`), so its record has the free-text row at the index the typed screen uses. Before, it
started from a different question (Apple/Banana/Cherry) and only passed because of the position rule removed in N-A.
