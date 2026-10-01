# Re-review: answer blocked dialogs from Slack (round 2)

Previous review: `docs/review/blocked-answer.md` (CHANGES-REQUESTED, 3 MUST-FIX). Writer's notes: the "Round 2" section
of `docs/progress/BLOCKED-ANSWER-done.md`. I reviewed the uncommitted working tree against the live facts the
orchestrator gave: (a) newlines are sent as they are; (b) multi-select "Type something" means moving the cursor with
up/down and then typing, with no Enter; (c) Submit means cursor to the Submit row, then Enter; (d) no `--`.

Validation: `python -m pytest -q` gives **765 passed, 5 skipped** (matches the writer). I ran scratchpad probes against
the real bridge and fakes; they were not added to the repo. The round-1 probes (M1, M2, and the N2 parser screens) now
pass. One new probe fails; see R1. No source or test file was edited, and no live Slack or Herdr calls were made.

**Result: 1 MUST-FIX (a regression from the N7 fix), 4 NICE-TO-HAVE.**

## Round-1 findings, verified in the code

| # | Verdict | Evidence |
|---|---|---|
| M1 | **fixed** | `_dialog_gone` (`bridge.py:1850`) consults `_dialog_message_state` (`:1744`). If the message is open, it only posts `DIALOG_STALE_TEXT`. If the message is closed (the ts is in `closed_dialogs`, written by `_close_dialog` at `:1686-1690`), it only posts the notice. It strips only a message that was never a dialog. My round-1 multi-select probe now passes. |
| M2 | **fixed** | `_fingerprint` (`dialog.py:323`) now covers kind, title, body (the plan excerpt above the rule) and plan_file, plus the region lines, with the cursor still excluded. `plan_hash` is set in `_render` (`bridge.py:1608`) and compared in `_dialog_state` (`:1668-1670`). The round-1 probe now shows different fingerprints. |
| M3 | **fixed** | `_open_dialog` (`bridge.py:1618-1639`) builds the text, the blocks and the record once for each op. It saves them as `dialog_pending` before `_post`, and clears it in the same `_commit`. Tokens are now random (`uuid4`), no longer derived from the op. Retries post or reuse exactly the persisted message and record, and the click-time check handles any later screen change. |
| N1 | **fixed** | `_answer_locked:1885` (and the thread-reply path `:1911`) sends a keypad key without the fingerprint check only while `dialog is None`. `_read_dialog` (`:1561`) re-reads an unparsed screen twice before falling back to a keypad. |
| N2 | **fixed** | `_options_block` (`dialog.py:269`) requires a cursor (on an option or on the Submit row) and picks the candidate that ends lowest on the screen. `_classify:343` detects trust from the option labels only. Probes: an idle screen ending in a list gives `None`; a Codex answer that mentions "trust this folder" gives `None`. |
| N3 | **fixed** | `handle_transition` (`:2143`) takes the answer lock and then the admission lock. `_startup_dialog` (`:996`) takes answer, then admission, then the key lock. `handle_reconcile` takes the answer lock only. No path takes admission (or a key lock) and then an answer lock. `send`/`_admit`/resume take no answer lock, and the answer worker never takes the admission lock. No deadlock. |
| N4 | **fixed** | The lock is keyed by the terminal id (`_answer_id`, `_transition_answer_id` at `:1533-1540`). Click and reply workers re-resolve their target after taking the lock (`:1824`, `:1841`). `_close_dialog`/`_rerender_dialog` resolve the key by token, and `_send_deferred` follows a re-key through `_key_now`. The fallbacks without a terminal id differ; see R4. |
| N5 | **fixed** | `run_new_agent:938-945` runs the delay first, then `_codex_trust_shown` (3 reads), with no second delay. |
| N6 | **fixed** | `_startup_dialog:1003-1005` keeps an existing record and only commits `deferred_prompt` / `last_blocked_seq`. |
| N7 | **fixed, but introduces R1** | `_queue_recovery` (`:401`), `handle_reconcile` (`:1723`), `_recheck_idle_dialogs` (`:1737`) and the notifier idle timeout (`:2102`). |
| N8 | **fixed** | On timeout, `_confirm_answer:2003-2011` re-renders under a new token with the note. A queued second click then takes the M1 "open" path and presses nothing. |
| N9 | **accepted as not changed** | Live fact (d). The reason is recorded in `cli_args` and in a test. |
| N10 | **fixed** | The modal's `private_metadata` carries `{c, m, th}` (`open_dialog_text_modal:1777`), and `submit_dialog_text` passes them on. LIVE_TEST 7-1 wording is updated. |
| N11 | **fixed** | Tests added for M1-M3, restart persistence, resume with an open dialog, a re-key race, parser lists and trust wording, and Block Kit (27 options, a 3500-character label). |

Next/Submit navigation (fact c): `_rows`/`_moves`/`submit_keys`/`text_keys` (`dialog.py:432-485`) follow the drawn row
order: checkbox options, then Submit, then "Chat about this". They start from the **live** cursor, including when the
cursor is on the Submit row. Probes on `dialog_ask_multi.txt`:

| Case | Keys |
|---|---|
| Submit, cursor on option 1 | `down×4, enter` |
| Type-something row, cursor on option 1 | `down×3`, no Enter |
| Submit, cursor on the Submit row | `enter` |
| Type-something row, cursor on the Submit row | `up` |

`[Next →]` sends `submit`, and falls back to `right` only when no Submit row was parsed (`blocks.py:619-620`). An empty
move list skips `send_keys` (`bridge.py:1958`). No regression was found in this navigation.

## MUST-FIX

### R1: Restart recovery closes a dialog that is still waiting and posts nothing in its place

- **Where:** `herdr_slackbot/bridge.py:1711-1721` (`_reconcile_dialog`), called from `handle_reconcile` (`:1723`),
  which `_queue_recovery` (`:401-406`) queues on every start and pairing.
- **Problem:** `_reconcile_dialog` closes the record as `✅ answered on PC` whenever the state is `changed`. That
  includes a keypad record whose screen now parses. In the transition path this is correct, because a new
  `blocked` transition follows and `_notify_transition` posts the new dialog. In the restart path no transition
  follows: the manager seeds pane state on first sight without emitting one. A `_Resume` for a Slack task with an
  open dialog also only commits the seq (`_settle_resumed`).
- **Failure scenario (reproduced):** A two-question AskUserQuestion is posted, and the bridge stops (restart, PC
  sleep). On PC, the user answers question 1, and question 2 is now waiting (the agent stays `blocked`). The bridge
  starts, and recovery reconciles the record as `changed`. The message becomes `✅ answered on PC`, the record is
  dropped, and **nothing new is posted**. The agent is blocked with no buttons in Slack and no notice, until some
  later transition. Probe: `block(env, ASK_SINGLE)`, the screen changed to `ASK_MULTI`, `restart(env)` → `record(env)
  is None` and 0 new posts. The last update of the old message is the closed "💬 … Color — Pick a color?" block.
  Before round 2, the record survived the restart and the first click re-rendered it. So this is a regression from
  N7.
- **Fix:** In the reconcile-without-transition path, when the agent is still `blocked` and the state is `changed`,
  re-render the message under a new token (`_rerender_dialog(..., note=DIALOG_CHANGED_TEXT)`) instead of closing it.
  This also covers a keypad whose screen now parses. Alternatively, close it and then `_open_dialog` a new message
  with op `blocked:<terminal>:<seq>` (respecting `last_blocked_seq`). Keep the current close behaviour for
  transitions. Add a restart test with a changed dialog on screen (blocked), and one with a keypad whose screen now
  parses.

## NICE-TO-HAVE

### R2: Multi-line free text on a multi-select "Type something" row is unverified

`bridge.py` `_answer_keys` (fact a) sends newlines as they are for every free-text option, including the multi-select
row. Fact (a) was verified in an input, and fact (b) says Enter on this row unchecks it. It is not established
whether an LF typed into the row behaves as a line break or as Enter. The parser also depends on how a wrapped or
multi-line row is drawn. Probe: if the second line is drawn left of the text column, `_attach_continuations`
(`dialog.py:236`) stops at it and the `Submit` row is no longer found (`submit_after=None`). `[Next →]` then falls
back to `right`, which the text row consumes. Suggest flattening newlines for multi-select rows only, until LIVE_TEST
7-1 covers "multi-line text in multi-select Type something, then Next". Alternatively, find `Submit` by scanning
between the last checkbox option and the next option instead of stopping at the first less-indented line.

### R3: Out-of-date lock-order comment

`bridge.py:380-381` still says "Order: admission lock, then this lock, then the per-key lock". The code and the block
comment at `:1527-1530` now use answer → admission → key. Fix the comment so nobody "restores" the old order.

### R4: The answer-lock id can differ between an ended transition and a click on a provisional agent

`_transition_answer_id` (`:1536`): an ended transition carries no `info`, so the terminal id is looked up through
`get_thread(t.session)`. For a thread that is still provisional (keyed `prov:…`), that lookup misses, and the lock id
falls back to the session. A click holds the lock keyed by the terminal id. The two then run concurrently. The effect
is benign (both close the message: "ended" or "sent, then the agent ended"). Suggest also looking up the entry by
`t.pane_id`, or by the terminal id remembered by the manager, before falling back. This is the same class of issue as
the writer's note on fallbacks.

### R5: The idle recheck and the notifier's latency

- `_notify_loop` (`:2102`) rechecks idle (Codex trust) dialogs only after 15 s with no notifier work. With steady
  activity from other agents, it never runs. Track the time of the last recheck and run it when that is older than
  15 s, whatever the queue holds.
- `_read_dialog` can now sleep up to 0.6 s in the notifier thread, while the transition path holds the admission and
  key locks (`_notify_transition` → `_open_dialog`). This only happens for screens that do not parse. It is
  acceptable, but it goes against the goal of N3.

## Note (not a finding)

`README.ko.md` is untracked and linked from `README.md:3` ("English | 한국어"). It does not appear in the Round 2 notes.
The orchestrator should confirm it belongs in this change set before committing.

## Deviations after round 2

| Deviation | Verdict |
|---|---|
| 2: free text flattened | **Reverted** by fact (a). Accept. See R2 for the multi-select row. |
| 3: keypad skips the fingerprint check | **Accept.** Narrowed as asked in N1. |
| 5: footer limit | **Accept.** Now backed by the cursor requirement (N2). |
| 7: silent CLI success | **Accept.** Narrowed to `pane.send_text` with empty stdout. |
| Answer-lock fallbacks without a terminal id | **Accept.** Herdr 0.8.2 always reports a terminal id; see R4 for one gap. |

## Verdict

**CHANGES-REQUESTED** (1 MUST-FIX: R1).
