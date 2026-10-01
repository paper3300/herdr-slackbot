# Thread history — recheck of the review fixes

Rechecked `a828f47` (Thread history: fix review findings T1, N1, N2) against `docs/review/thread-history.md` and the
"Review fixes" section of `docs/progress/THREAD-HISTORY-done.md`. `HEAD` at recheck start: `a828f47`.

**Result: PASS (0 blockers, 0 majors).** T1, N1, N2 and the tail-size observation are fixed. My earlier scratch
probes now show the expected behaviour, and so does a new resume probe. Intent handling for every other op is
unchanged. One observation follows; it does not block.

Validation:
- `python -m pytest -q`: **832 passed, 5 skipped**.
- Scratch probes ran from outside the repo, reusing the test `env`, the `Transcript` helper and the fake
  transport's `Accepted` fault.
- A read-only check on the local `~/.claude/projects` transcripts collected counts and timings only.
- Source and tests were not edited, and nothing was committed.

## Fixes

| Finding | Status | Evidence |
|---|---|---|
| **T1** reused post commits a fresh cursor | **Fixed** | `post_result` computes the range once and passes `{"history_cursor": …}` as the intent payload. `_post_tracked` stores it with the intent before posting (`_add_intent`). The intent with `ts` is persisted by `_accepted`. On reuse (intent `ts`, or a `find_message` hit), the **stored** payload is returned, and an older intent without one returns `{}`. If the lookup proves the earlier attempt absent, the payload is replaced before the new post. A reused cursor is committed only if it is not behind the current state. See the probes below. |
| **N1** no new turn repeats the latest turn | **Fixed** | `_after_cursor` returns `end + 1` when the cursor id is at or after the answer, or when `cursor.at ≥ answer.at`. `select_range` then returns `[final]` with `advance=False`, so today's plain result is posted and the cursor is unchanged. Probe P2: the second message no longer contains "PC prompt one". Real data: for all 361 local files with a JSONL answer, re-running `select_range` with the cursor the first result would store gives `[final]` / `advance=False`. |
| **N2** untimestamped cursor replaces a timestamped one | **Fixed** | `newer_cursor` returns False when the old cursor has an `at` and the new one does not. `select_range.advance` trusts the turn order when the cursor was placed by id or time, and the timestamp comparison only for an unplaced cursor. |
| **Observation** answer outside the 4 MB tail | **Fixed** | `_history_range` reads the conversation with `claude_session.TAIL_BYTES` (8 MB, the result read's tail). The send modal keeps 4 MB. Real data: 361 of 361 local files with a JSONL answer now find it (previously 359 of 360), and the worst read + parse with 8 MB is 78 ms. |

## Probes rerun

| Probe | Before (`e8209ad`) | Now (`a828f47`) |
|---|---|---|
| **P3** uncertain post (`Accepted`) of Slack task T1 → PC follow-up → working→done reuses `result:T1` | 1 result; cursor jumped to `a-pc`; the PC follow-up was never shown | 1 result (reused); cursor stays at `a-sl`. The next result **contains** the PC follow-up. |
| **P1** `post_result(op="result:X")` → commit lost → new turn → same op again | reuse returned cursor `a3`; `three`/`third` never shown | reuse returns the stored cursor `a2`. The next message **contains** `three`/`third`. |
| **P4** (new) resume after a crash between post and commit: in-process intents cleared, a PC turn while down, then `handle_resume(SID, "T1")` | — | The persisted intent is `{"ts": …, "payload": {"history_cursor": a-sl}}`. Resume reuses the post (still 1 result), commits cursor `a-sl`, clears the pending task and drops the intent. The next result contains the turn typed while the bridge was down, and does not repeat the Slack answer. |
| **P2** status flap without a new turn | second message repeated the PC prompt | plain three-block result, cursor unchanged |

## Regression check: other ops

- **`_post` is now a thin wrapper.** It returns `_post_tracked(...)[0]`. With no payload (every caller except
  `post_result`), the code paths are the same as before:
  - `op is None` posts directly;
  - an accepted intent returns its `ts`;
  - otherwise the lookup runs, with the same `lookup_too_early` rule;
  - a new intent is `{"since", "ts"}`. `_add_intent` adds `payload` only when it is non-empty, and the
    payload-replace branch is skipped because `None == None`;
  - Slack errors keep or drop the intent as before.
- **Probe on the other ops.** I ran an `Accepted` fault, a cleared in-process cache and a reconcile retry for
  `started:`, `blocked:`, `ended:` and `unstarted:` ops, plus a legacy float-valued intent. Before and after the
  reuse, the intents held exactly `since`/`ts` (no `payload` key), `_post` returned a `str` ts, and the reconcile
  reused the found post instead of posting again.
- **Readers of `post_intents`.** Only `_intent`, `_store_intent`, `_drop_intent` and `_commit` read them. `_commit`
  reads only `since`, so the extra key is ignored when pruning. A re-key merges the entry as before.
- **Existing tests.** The idempotency, uncertain-post, dialog, resume and send-preview tests all pass unchanged.

## Observation (non-blocking)

- **A PC turn that finishes while a Slack task's post is unresolved shows up only in the next result.** In P3, the
  PC follow-up's own completion is absorbed by the reused `result:T1` post, as before this feature. The follow-up is
  no longer lost: the cursor stays, and the next result covers it. That next result only arrives when the agent
  completes another turn, so the thread can lag until then. If that matters, post the reconciled range on the next
  completion with a fresh op. No change is needed for correctness.
