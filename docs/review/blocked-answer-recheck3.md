# Re-review: answer blocked dialogs from Slack (round 4)

Previous: `docs/review/blocked-answer-recheck2.md` (PASS, 2 NICE-TO-HAVE: N-A, N-B). Writer's notes: the "Round 4"
section of `docs/progress/BLOCKED-ANSWER-done.md`.

Validation: `python -m pytest -q` gives **779 passed, 5 skipped** (matches the writer). All my earlier scratchpad
probes still pass (5/5). The round-3 N-A probe now gives a plain toggle. No source or test file was edited, and no
live Slack or Herdr calls were made.

**Result: 0 MUST-FIX, 1 NICE-TO-HAVE (cosmetic).**

## Round-3 findings, verified in the code

| # | Verdict | Evidence |
|---|---|---|
| N-A | **fixed** | `parse_dialog(..., typed_row=None)` (`dialog.py:393`) turns the row above Submit into a typed free-text row only when `typed_row == submit_after`, the row is a checkbox row, and it is neither "Chat about this" nor already labeled "Type something" (`:427-432`). The bridge passes `_typed_row_hint(rec)` (`bridge.py:2387`): the index of the record's multi-select free-text row. It passes it only when parsing against an existing record (`_dialog_state:1666`), so fresh parses (`_read_dialog`, the startup/Codex checks) never get a hint. Probes: no Type-something row → Cherry stays a plain toggle; the typed fixture without a hint → a plain row `hello a` (description `b`). The fingerprint is computed from the screen lines, not from the options, so the hint cannot move a record between `open` and `changed`. |
| N-B | **fixed** | Each record option stores `typed` (`typed_text`, `dialog.py:464`; stored at `bridge.py:1603`). The modal for such a row shows "added after it (nothing is cleared)" with the current text (`blocks.py:680-685`). A thread reply into such a row posts the append notice with the text it holds after the re-render (`bridge.py:1935-1940`). A first answer into an empty row gets no notice. The notice text is escaped and capped at 1500 characters (`_code`), within the section limit. |

## Evidence chain for the hint (regression check)

- **Untyped → typed.** The first record parses "Type something" normally (`free_text`, `checked=False`), so the hint
  is set. Confirmation polling parses the typed screen with that hint, and the re-rendered record keeps
  `free_text=True`, `checked=True`, so the next click (`[Next →]` = `down, enter`) still carries the hint.
- **Single-select Q1 → multi-select Q2 on the same message.** Q1's free-text row has `checked=None`, so there is no
  hint. Q2 appears untyped and gets `free_text` from its label, which starts a new chain. No wrong hint is carried
  across questions.
- **Chain gap (accepted).** If text was typed on the PC before Slack ever parsed that question, the row shows as a
  plain toggle that displays the typed text. It sends only the digit the user sees, so there is no safety issue.
  This is the intended conservative outcome of N-A.
- **Wrong-row hint.** It is ignored: the index must equal `submit_after`, and a row already labeled free text is left
  as it is.

## The adjusted round-3 test

`test_multi_line_text_into_a_multi_select_row_then_next` (`tests/test_bridge_dialog.py:881`) now starts from
`ASK_MULTI_UNTYPED` (`:841`). That is the typed fixture with row 3 reverted to `[ ] Type something`. Before, it started
from a *different* question (Apple/Banana/Cherry, free-text row at index 3) and then switched to the typed One/Two
layout (index 2). It only passed because the position rule, now removed, guessed the free-text row.

The new start is the realistic sequence: the same question, first untyped, then typed. The test still asserts
everything the round-2 fact requires:
- `send_text("hello a\nb")` with no Enter and no moves (the cursor is already on the row);
- the re-rendered label `hello a ↵ b`, with `free_text` kept;
- `[Next →]` = `["down", "enter"]`, reaching the review screen.

**The adjustment is legitimate and hides no bug.** Its old premise was the heuristic N-A removed.

## NICE-TO-HAVE

### N-C: The thread-reply notice may show a guessed text after a re-key (cosmetic)

`_typed_now` (`bridge.py:1943`) calls `self._key_now(key, None)`. With no terminal id, `_key_now` never follows a
provisional → session re-key, so the record is not found. The notice then falls back to `already + "\n" + text`,
which is usually identical to the real text. Pass `rec.get("terminal_id")` (or resolve the key by the new token) to
show the actual current text.

## Verdict

**PASS** (0 MUST-FIX).
