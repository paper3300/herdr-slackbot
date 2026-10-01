# Re-review: answer blocked dialogs from Slack (round 3)

Previous: `docs/review/blocked-answer-recheck.md` (CHANGES-REQUESTED, 1 MUST-FIX: R1). Writer's notes: the "Round 3"
section of `docs/progress/BLOCKED-ANSWER-done.md`. I reviewed the uncommitted working tree against the orchestrator's
live fact for R2. That fact: an LF sent with `pane send-text` into the multi-select "Type something" row inserts a
line break (it does not submit or uncheck); the continuation is drawn at the description indent; `down` then moves to
Submit. `README.md` / `README.ko.md` were ignored except the "Answering dialogs" section, as instructed.

Validation: `python -m pytest -q` gives **774 passed, 5 skipped** (matches the writer). I re-ran my round-1 and
round-2 scratchpad probes, including the R1 restart probe: all 5 now pass. A new parser probe is described under
N-A. No source or test file was edited, and no live Slack or Herdr calls were made.

**Result: 0 MUST-FIX, 2 NICE-TO-HAVE.**

## Round-2 findings, verified in the code

| # | Verdict | Evidence |
|---|---|---|
| R1 | **fixed** | `_reconcile_dialog(key, rec, transition=True)` (`bridge.py:1715-1733`) now handles a changed dialog in two ways. On the transition path it still closes it (the `blocked` transition that follows posts the new dialog). On `handle_reconcile` (`:1744`, restart / pairing / idle recheck; `transition=False`) it re-renders the same message under a new token with `DIALOG_CHANGED_TEXT`. "Gone" and "answered" still close. A keypad whose screen is still unparsed is left alone, and one whose screen now parses is re-rendered. My round-2 probe (question 1 answered on PC while the bridge was down, then a restart) now keeps a live record with buttons for question 2. |
| R2 | **fixed** (with the live fact) | Multi-line text is sent as it is. Parser (`dialog.py`): the continuation scan still finds `Submit` for the live layout. A text-based fallback scan (`:245-256`, `SUBMIT_SCAN_MAX`) handles a line drawn left of the text column. The row above Submit in a multi-select question is recognized **by position** as the typed free-text row (`_typed_row`, `:261`, applied at `:424-427`). Probe on `dialog_ask_multi_typed.txt`: option 3 = `'hello a\nb'`, checked, `free_text=True`; `submit_after=2`; `[Next →]` = `down, enter`; the text keys before more typing are `[]`. The untyped fixture keeps `Type something` as the free-text row. Slack joins the lines with ` ↵ ` (`blocks.py:551`). |
| R3 | **fixed** | The comment at `bridge.py:380-382` now reads answer lock → admission lock → per-key lock, consistent with the code. |
| R4 | **fixed** | `_transition_answer_id` (`:1537-1544`) falls back to the pane's provisional entry (`find_provisional`), so an ended transition of a provisional thread gets the click's terminal-keyed lock. |
| R5 | **fixed** (bullet 1); bullet 2 **accepted** | `_notify_loop` (`:2111-2126`) runs the idle recheck on a 15 s wall-clock interval, whatever the queue holds. Bullet 2 (re-reads under the admission lock) was left as it is, to keep M3's build-once-per-op logic intact. I agree with that trade-off: it only affects screens that do not parse, and costs at most 0.6 s. |

## Regressions checked

- **Lock order and deadlocks.** No new lock paths. `handle_reconcile` takes only the answer lock, and
  `_rerender_dialog` takes the key lock inside it. No path takes the admission lock or a key lock and then an answer
  lock.
- **Restart re-render versus the deferred prompt.** `handle_reconcile` sends `deferred_prompt` only when no dialog is
  open. A dialog re-rendered at restart keeps the prompt waiting until it closes, which is correct.
- **Idle recheck.** It re-queues only `idle` records. A trust screen that is still shown keeps its fingerprint, so
  the 15 s recheck does not keep re-rendering it.
- **Next/Submit navigation.** Unchanged for untyped screens (round-2 sequences re-probed). The typed layout follows
  the live fact.
- **README "Answering dialogs".** It matches the behaviour: [Next →] goes to the Submit row; multi-line text is sent
  as it is; typing ticks the box; an "out of date" button gets a notice; restart gives "answered on PC".

## NICE-TO-HAVE

### N-A: The row above Submit counts as free text even when it is a regular option

- **Where:** `dialog.py:424-427`.
- **Problem:** Any checkbox row directly above `Submit` becomes a typed free-text row, unless it is already labeled
  "Type something" or is "Chat about this". Claude's AskUserQuestion always adds the "Type something" row, so the
  captured layouts are fine. But the rule has no positive evidence that text was typed.
- **Probe:** `dialog_ask_multi.txt` with the Type-something row removed gives
  `('Cherry\nA small, tart fruit full of antioxidants', free_text=True)`. Cherry's button would open a modal, and a
  submitted text would move the cursor onto Cherry and type into a row that is not an input. Typed characters could
  act as shortcuts there (a digit toggles an option).
- **Fix:** Treat the row as typed only on positive evidence. For example, the bridge passes in that the previous
  record (or the untyped parse) had a free-text option at that index. Or require that the dialog has no
  "Type something" row **and** that the row is checked with no description-style line. If the evidence is missing,
  keep the row a plain toggle.

### N-B: A second free-text answer on the typed row appends to the earlier text

- **Where:** `bridge.py` `_answer_keys` / `text_keys` for multi-select.
- **Problem:** After text was typed, the row is still free text. A second modal or thread-reply answer moves the
  cursor onto the row and `send_text` **appends**, producing "hello a\nb" + "new". That is probably not what the user
  means by "answer again".
- **Fix:** Either say so in the modal ("added to the text already typed: …"), or clear the row first once a clearing
  key is verified live (for example `ctrl+u`). The label ("4. hello a ↵ b") already shows the current text.

## Deviations

No new deviations. The round-2 table stands. R5 bullet 2 is accepted (see above).

## Verdict

**PASS** (0 MUST-FIX).
