# Review: answer blocked dialogs from Slack

Spec: `docs/progress/BLOCKED-ANSWER.md`. Writer notes: `docs/progress/BLOCKED-ANSWER-done.md`.
Reviewed: the uncommitted working tree (`git diff HEAD` plus the untracked `dialog.py`, tests and fixtures), on top of
`f46314e`. Line numbers refer to the working tree at review time.

Validation: `python -m pytest -q` gives **733 passed, 5 skipped** (matches the writer's count). Two probe tests were
run from the scratchpad against the real bridge and fakes. They were not added to the repo. Both failed and reproduce
M1 and M2 below. No live Slack or Herdr calls were made, and no source or test file was edited.

**Result: 3 MUST-FIX, 11 NICE-TO-HAVE.**

## MUST-FIX

### M1: A stale click wipes the buttons of the live, re-rendered message

- **Where:** `herdr_slackbot/bridge.py:1730-1739` (`_dialog_gone`), reached from `answer_dialog` (`:1699`).
- **Problem:** When the token is unknown, `_dialog_gone` edits `message["ts"]` using the blocks from the click payload.
  Those blocks are the stale version the user clicked. They have their actions stripped and "This question is no
  longer open." appended. But re-rendering (next question, toggled checkbox, review screen) keeps the **same
  message ts** under a new token. A second click that was already sent before the first re-render landed therefore
  overwrites the current message.
- **Failure scenario (reproduced):** A multi-select question is on screen. The user taps `☐ 1. Apple`, then quickly
  `☐ 3. Cherry`. Apple is sent, the dialog re-renders under token T2, and the record stays open. The Cherry click
  carries T1, so `find_dialog(T1)` returns None and `_dialog_gone` replaces the message with the old blocks and no
  buttons. The dialog is still open on the PC, but Slack now shows no buttons, only "no longer open". The user
  cannot answer from Slack any more (a thread reply only works when the dialog has a free-text option). Probe:
  `record(env)` is still open, and the last update of `rec["message_ts"]` has no `actions` block.
  The same code also damages the plain double-click case that `test_double_click_sends_keys_once` covers. The
  second click overwrites the `✅ 1. Yes — answered from Slack` outcome with the original dialog text plus "no
  longer open". The test does not check the final message.
- **Fix:** Never edit a message that belongs to a known dialog. If a current record has `message_ts == message["ts"]`,
  keep the message. At most post a short "That button was out of date; use the updated buttons above." (or
  nothing), and send no keys. For closed dialogs, `_close_dialog` has already written the outcome, so leave that
  message alone too. One option is a small `closed_dialog_ts` list in the thread entry. The simpler option is to
  strip only messages whose ts is not a dialog message of that thread. Add tests: a stale-token click after a
  re-render keeps the buttons, and a double click keeps the `✅ … answered from Slack` outcome.

### M2: The plan-approval fingerprint ignores the plan text

- **Where:** `herdr_slackbot/dialog.py:388` (`_fingerprint(kind, lines[start:end])`), with `_region_start` (`:249`)
  and `_plan_excerpt` (`:279`).
- **Problem:** The fingerprint region starts below the `────` rule. In plan approval, the plan itself is **above**
  that rule, between the `╌╌╌` rules. Two different plans with the same question and options therefore get the
  same fingerprint. Probe: `dialog_plan.txt` and a copy whose step 1 is "Delete every file in the repository
  recursively now." both give `2ae1832e…`.
- **Failure scenario:** The bridge posts plan P1. While the bridge is down (restart, PC sleep), the user answers on
  PC with "Tell Claude what to change", and Claude shows plan P2. After the restart, the record is still open, the
  agent is still `blocked`, and the fingerprint matches. The Slack message still shows **P1**. The user taps
  `1. Yes, auto-accept edits`, and the bridge approves **P2**, which the user never saw, in auto-accept mode. This
  also happens without a restart whenever a `blocked → working → blocked` change is coalesced into one snapshot (the
  manager emits only when `prev != status`). Approval with auto-accept is the most consequential key the bridge can
  send, so it must be covered by the "never a different dialog" rule.
- **Fix:** For `KIND_PLAN`, include `dialog.body` (the visible plan excerpt) and `plan_file` in the fingerprint
  payload. Better, include `title` and `body` for every kind, so the fingerprint covers everything the message
  shows. At post time, a hash of the plan file content can also be stored in the record and compared at click time.
  Add a parser test (different plan text gives a different fingerprint) and a bridge test (a re-plan behind an open
  record re-renders and sends nothing).

### M3: A retried post can leave the record and the posted message out of sync, so option *i* may press a different option

- **Where:** `herdr_slackbot/bridge.py:1548-1564` (`_open_dialog`), used under `_retrying` in `_startup_dialog`
  (`:980`) and by the notifier's retry of `_notify_transition` / the resume path.
- **Problem:** Each attempt re-reads the screen and re-parses it, but the token is `sha1(op)`, the same on every
  attempt. If attempt 1's `chat.postMessage` has an uncertain outcome, attempt 2 finds that message through the
  intent (`_post` → `find_message`) and **reuses** it. It then commits a record built from the attempt-2 screen:
  new fingerprint, new options, same token. At click time the live screen matches the record, so `_send_answer`
  maps the button's index onto the **live** dialog. That index was taken from the *posted*, older dialog.
- **Failure scenario:** The Slack post times out (uncertain outcome). Before the retry (1-4 s backoff), the user
  answers question 1 of an AskUserQuestion on PC, and the screen moves to question 2, or to another permission
  prompt. The retry reuses the posted message (question 1's buttons) and stores question 2's record under the same
  token. The user taps "2. Blue" and the bridge presses `2` on question 2 (for example, "2. Yes, and always
  allow…"). The same reuse happens when `seq` is missing: the op falls back to `task_id` / `t.at`, and a later
  dialog in the same task gets an already-used token.
- **Fix:** Build the record once per op and persist it before the first attempt. Follow the same pattern as
  `root_op`, for example by storing `dialog_pending: {op, token, fields, blocks}` in the entry. Retries then post
  and commit exactly that, and the click-time fingerprint check handles any later change. A smaller fix is
  `token = sha1(op + "\n" + fingerprint)`: a reused message whose screen changed then no longer matches the record.
  Once M1 is fixed, that click becomes "out of date" instead of the wrong key. Add a test: an uncertain first post,
  a changed screen, then a click on the reused message sends no keys.

## NICE-TO-HAVE

### N1: Keypad clicks skip the fingerprint check even when the live screen now parses as a real dialog (deviation 3)

`bridge.py:1759`. A keypad is posted when the screen had not been drawn yet or could not be parsed. If the live
screen **now** parses as a dialog, the user never saw it, and the key still goes out. Fix: skip the fingerprint check
only when the live screen is still unparsed. If `dialog is not None`, re-render (with `DIALOG_CHANGED_TEXT`) and send
nothing. Also consider re-reading the screen once or twice (about 0.3 s apart) in `_open_dialog` before falling back
to a keypad, to follow the spike's "never conclude from a single immediate read".

### N2: Parser robustness: transcript numbered lists and trust wording

- `dialog.py:339-345`. An ordinary idle Claude screen that ends with a numbered list parses as `unknown` with the
  list items as options. Only 4 non-blank lines (rule, `>`, rule, footer) follow it, which is within
  `FOOTER_MAX_LINES`. Probe: `('unknown', ['Install deps', 'Run tests', 'Deploy'])`. `_numbered_options` is also
  tried **before** `_cursor_menu` whatever their position, so an unnumbered menu below such a list loses to the list.
  Today this is contained, because parsing only matters while `blocked` and the click-time fingerprint re-check
  catches most mismatches. It still produces misleading buttons. Fix: require the cursor marker (`❯`/`›`) on one of
  the options. Every real dialog fixture has one, and transcript lists never do. Also pick whichever run (numbered
  or cursor menu) ends lower on the screen. Add parser tests with an idle screen that ends in a numbered list, and
  with a list above an unnumbered menu.
- `dialog.py:304`. Trust is decided by the phrases "trust this folder" / "folder access" anywhere in `pre_text`.
  Probe: a Codex idle screen whose last answer is "To trust this folder you can: 1. … 2. …" is classified as
  `trust`. That classification drives the Codex startup check and the `idle` records. Fix: anchor the trust check
  on the option labels ("Trust and continue", "Yes, I trust this folder"), or require the cursor as above.

### N3: A click stalls the whole bridge's notifier and admission

`bridge.py:2005`. `_handle_transition` holds the global `_admission_lock` while it waits for the answer lock. The
answer lock covers up to 5 s of polling plus Slack retries (1+2+4 s) in `_close_dialog` / `_rerender_dialog`. The
answer itself usually causes a transition for that agent, so every click can briefly stop events for **all**
agents, and every `send()`. There is no deadlock (no path takes answer → admission). Consider doing the Slack
edits after releasing the answer lock, or acquiring the answer lock before the admission lock in the transition path
(`_startup_dialog` would need its key resolved first).

### N4: The answer lock is not stable across provisional → session re-keying

`bridge.py:446`, `:2002`. The lock is looked up by thread key. If a startup (trust) thread is provisional (no session
yet) and `_transition_key` re-keys it to the session while a click is polling, the transition takes a **different**
lock. It reconciles the moved record in parallel and can overwrite `✅ … answered from Slack` with `✅ answered on
PC`. The worker's `_close_dialog(prov_key)` then finds no entry. The deferred prompt is still sent once, because of
the pop under the key lock. Fix: key the answer lock by `terminal_id` (stable across re-keying), or re-resolve the
key after the lock is taken.

### N5: The Codex trust check runs before `codex_prompt_delay`

`bridge.py:923-932`. The screen is read right after `agent start` returns, then the bridge sleeps. If the trust screen
is drawn during that sleep, the prompt is pasted into it, and Enter selects `1. Trust and continue`. This was
already possible before the change, but the change exists to prevent it. Fix: check after the delay, or poll a few
reads.

### N6: A startup-dialog race can post the dialog twice

`bridge.py:946-990`. Suppose the manager first sees the new Claude agent as `unknown`/`idle` and then `blocked`.
The PC path then creates a 🤖 thread and a dialog message, possibly before `_startup_dialog` runs.
`_startup_dialog` reuses the thread but posts a second dialog and overwrites the record, so the first message's
buttons turn into "no longer open" (and M1 applies). Fix: if the entry already has an open `dialog` for this agent,
only add `deferred_prompt`.

### N7: Records and the deferred prompt are not reconciled without a transition

Covered in the writer's open questions. In three cases no transition follows: a Codex trust answered on PC (stays
`idle`), a dialog answered while the bridge was down, or an agent that stays idle after a restart. The buttons then
linger, and the `deferred_prompt` is never sent. Fix: in `start()` / the resume pass, run `_reconcile_dialog` over
every entry that has a `dialog`, then `_send_deferred`. For `idle` records, recheck on a cheap timer (for example
from the resync loop). Also note: `_send_deferred` pops the prompt before `_wait_settled` (up to 20 s). A crash in
that window silently drops it. That is acceptable under at-most-once delivery, but worth a log line.

### N8: A double click after "could not confirm" presses the key twice

`bridge.py:1835-1869`. When nothing changes within 5 s (fast identical re-prompt, or status lag), the record keeps
its token, so a second click that was queued behind the answer lock passes every check and sends the key again. Fix:
store `sent_at` in the record (or rotate the token right after sending, re-rendering the buttons with the unconfirmed
notice). Drop clicks that arrived while the previous answer was in flight.

### N9: The CLI transport passes free text as a positional argument

`herdr_client.py:298`. An answer that starts with `-` ("-v please", "--dry-run") will be parsed by the `herdr` CLI as
an option. Fix: emit `["pane", "send-text", pane_id, "--", text]`, after checking that the CLI accepts `--`.

### N10: Small UX gaps

- `submit_dialog_text` (`bridge.py:1671`) passes no channel or message, so a closed dialog's "no longer open" is
  posted at the top level of the DM, not in the thread. Resolve the thread from the token before it is dropped, or
  from the view's `private_metadata`.
- `LIVE_TEST.md` 7-1 says a re-plan edits the same message. In practice Claude goes `working` after the feedback, so
  the old message closes and a new one is posted. Align the text with that.

### N11: Missing tests

- The M1, M2 and M3 regressions above.
- Restart persistence: a record written by one `Bridge`/`StateStore`, clicked through a new instance.
- The resume path (`bridge.py:~2170`) with an open `dialog` (commits the seq only, no second message).
- A thread reply racing a re-key (N4).
- Parser screens that end in a numbered list, and the trust wording (N2).
- A Block Kit limits test with more than 25 options (the `_actions` split) and a label over 3000 characters (the
  section clip).

## Checked, no finding

- **Lock ordering.** admission → answer → key everywhere. The answer worker never takes the admission lock, and
  `answer_thread_reply` / `_send_deferred` call `send()` only after releasing the answer lock. No deadlock was found.
- **Session safety.** `_dialog_state` resolves the agent by `agent_session` (with a terminal-id check for
  provisional entries), requires `blocked` (or the idle trust screen) and the same fingerprint, and sends to the
  resolved `pane_id`. Cursor movement is excluded from the fingerprint, and unnumbered menus take the cursor from
  the **live** dialog, so up/down counts stay correct if the cursor moved on PC.
- **Deferred prompt, exactly once.** It is popped under the key lock by whichever path wins (the answer worker or a
  settled transition), and restored only if a new dialog comes up. Delivery is at most once (see N7 for the one
  gap).
- **Block Kit.** Button text is at most 75 characters (`OPTION_TEXT_MAX`). Values are small JSON (token + index)
  with no screen text. Sections are clipped at 3000 and chunked by `chunk_lines`. Actions blocks are split at 25
  elements with unique block ids (`dialog_options`, `dialog_options1`, `dialog_ctl`, `dialog_full`). Action ids are
  unique per message (`dlg:opt:<i>`, `dlg:text:<i>`, `dlg:key:<k>`, `dlg:screen`). The block count is capped at 50.
  The plan body gets its code fence rebalanced. The modal's `private_metadata` is well under 3000 characters.
- **Fixtures.** Synthetic, neutral paths (`D:\work\demo…`), no real names. The permission-prompt wrap still
  reproduces. The boxed AskUserQuestion fixture matches the spec's structure.
- **Owner guard, ack-first.** The regex action handler and the view handler sit behind the existing middleware. The
  modal opens from local state only, before any Herdr I/O.
- **Notices (§4).** The `/send` command and the modal keep rejecting blocked agents with the new text. Help text
  updated.

## Deviations

| # | Deviation | Verdict |
|---|---|---|
| 1 | Extra keyword-only args on `dialog_blocks` | **Accept.** Additive; the positional signature matches the spec. |
| 2 | Free text flattened to one line | **Accept.** Safe default until the live test shows how `pane.send_text` treats `\n`. Documented in README and SPEC. Re-evaluate after LIVE_TEST 7-1 (plan feedback is often multi-line). |
| 3 | Keypad skips the fingerprint check | **Accept, with a condition.** Spinners make an exact check useless for unparsed screens, but the check must not be skipped once the live screen parses into a real dialog (N1). |
| 4 | A changed dialog must read the same twice | **Accept.** Avoids rendering half-drawn frames. Costs about 0.3 s per step. |
| 5 | Returns `None` when more than 8 non-blank lines follow the options | **Accept, but not sufficient.** It rejects long transcripts, but an idle screen ending in a list still passes (N2). Add the cursor-marker requirement. |
| 6 | Plan file read only under `…/.claude/plans/*.md`, ≤ 200 KB | **Accept.** A sensible guard against a screen-controlled path. |
| 7 | CLI input commands succeed on non-JSON stdout | **Accept.** A non-zero exit still raises. Verify the output in the live test. |
| 8 | Muted PC agents get no buttons; rejection text says "or answer on PC" | **Accept.** Consistent with the existing mute rule. |

## Verdict

**CHANGES-REQUESTED** (3 MUST-FIX: M1, M2, M3).
