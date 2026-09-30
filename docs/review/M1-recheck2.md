# M1 second re-review

Reviewed commit `a1be1b48d40766f1fc2ca0cfec61ed1175ccaeaa` (`M1 recheck fixes`) against `docs/SPEC.md`, including **Decisions added during implementation**, and `docs/review/M1-recheck.md`. Findings already recorded in `docs/review/M2.md` are excluded.

**Requested items: 2 fixed, 5 partially fixed.** The original regression tests pass, but additional scenarios below prevent closing five items.

**Open findings: 0 blocker, 3 major, 3 minor**, including one new minor regression (N5).

Validation: `python -m pytest -q` in a temporary `git archive a1be1b4` extraction: **319 passed, 5 skipped**. Additional isolated reproductions used the existing fakes, real temporary transcript/state files, injected I/O failures, and a thread barrier. The provisional-move reproduction connected the actual subscription manager to the bridge. N5 was also compared with the parent commit's transcript reader. No live Slack/Herdr operations were performed. Source and repository tests were not edited; the parallel M2 changes were not used for these results. Line references below are for `a1be1b4`.

## Status per requested item

| Item | Status | Verification / remaining issue |
|---|---|---|
| R6: old dispatcher mutates restarted manager | **Partially fixed** | The original held-lookup A-to-B replacement test passes: B is no longer replaced by A and no obsolete transition is emitted. However, the old lookup still clears the new run's dirty flag and cancels its scheduled recovery; see R6 below. Remaining severity is minor. |
| R8: counter recovery lost across restarts | **Partially fixed** | Missing/corrupt high-water files, unrelated writes, repeated loads of unrecoverable state, and explicit recovery pass the added tests. A salvaged legacy counter is still lost on the next restart, and a failed marker write also permits reuse; see R8. |
| R13: notice filter deletes punctuation-led code | **Fixed** | The added cases preserve aligned `)`, `}`, `//`, `-`, `#`, and `]);` in both Claude parsing and fallback while removing the actual update notice. The original failure is closed. |
| N1: complete fast tasks disappear | **Partially fixed** | Batched working/done and working/idle events, initial done, muted Slack work, and the threaded case pass. A preceding resync or a failed working-event lookup still consumes the only evidence of activity; see N1. |
| N2: moves end live sessions / lose pending work | **Partially fixed** | Successful destination-first moves, resync discovery into an empty destination, and bridge handling of supplied move transitions pass. Destination failures, an old-pane event arriving first, source reuse, and provisional-session moves still lose session continuity; see N2. |
| N3: incomplete JSONL accepted as final | **Fixed** | Partial next prompts, partial subsequent blocks, null stop reasons, successful append during retry, and bounded retry exhaustion pass. The original incomplete-tail cases now use the screen fallback. N5 concerns a complete but structurally invalid newer record. |
| N4: malformed JSONL prevents fallback | **Partially fixed** | The original string duration, nested-message shapes, and injected reader exception no longer escape. Non-finite numeric durations still raise during result formatting outside the fallback guard; see N4. |

## Remaining findings

### R8. A salvaged counter is still forgotten on the next restart

- **Severity:** major
- **Location:** `herdr_slackbot/state.py:141`, `herdr_slackbot/state.py:145`, `herdr_slackbot/state.py:110`
- **Problem:** Corrupt legacy state is renamed before the salvaged counter is made durable. When a counter can be salvaged and no high-water file exists, `_load()` retains that floor only in memory and does not create a recovery marker. The next process sees a pristine directory. In the unrecoverable branch, marker-write errors are logged and swallowed after the primary corruption evidence has already been moved away.
- **Concrete failure scenario:** Write `{"counter": 42, "threads": ` to `state.json`, with no high-water file. Load once: `counter_ok=True`, counter 42. Restart before any mutation: `next_counter()` returns **1**. This requires no injected I/O failure. Separately, with wholly corrupt legacy state, inject an `OSError` only when writing the recovery marker; the first store disables allocation, but reopening again returns **1**. Both were reproduced.
- **Suggested fix:** Persist the salvaged floor or recovery requirement before removing the primary evidence. Abort loading safely if that persistence fails, and recognize existing corruption backups when deciding whether a store is new. Add salvage-then-restart and marker-write-failure tests; neither path may allocate below the recovered floor without explicit recovery.

### N1. Resync and lookup failures still erase fast-task activity

- **Severity:** major
- **Location:** `herdr_slackbot/events.py:299`, `herdr_slackbot/events.py:376`, `herdr_slackbot/events.py:383`
- **Problem:** The activity hint exists only for one `recheck()` call and is considered only while accepting a newer snapshot sequence. A resync can advance that sequence before queued status events are dispatched. A failed lookup also drops the working hint; its retry retains only the pane ID.
- **Concrete failure scenario:** Seed S idle, let it run working-to-done, then process a resync before its queued working/done events. The resync emits `idle -> done`; both events are ignored at the already-consumed sequence. The sole decision is `none`, even for a muted pending Slack task. Separately, fail the lookup for the working event once, then process the done event: the same `idle -> done` / `none` result occurs. Both were reproduced. Later identical snapshots cannot recover the completion.
- **Suggested fix:** Retain verified activity evidence across lookup retries and snapshot reconciliation, with session/subscription identity and deduplication. A snapshot must not make an unaccounted working hint unusable. Add tests with resync ahead of the queued events and a transient lookup failure on the only active hint, asserting one eventual completion.

### N2. Move reconciliation still depends on destination success and ordering

- **Severity:** major
- **Location:** `herdr_slackbot/events.py:331`, `herdr_slackbot/events.py:358`, `herdr_slackbot/events.py:398`; `herdr_slackbot/bridge.py:273`, `herdr_slackbot/bridge.py:733`
- **Problem:** Destination-first processing preserves state only if the destination is successfully reconciled before the source is removed. `_apply_absent()` checks other already-tracked panes, rather than establishing whether the session is live elsewhere. Adoption also requires a nonempty session ID; `_PaneState` does not retain terminal identity for a provisional agent. The bridge's terminal lookup runs only when the manager emits a transition.
- **Concrete failure scenarios, reproduced:**
  - Move working S from `w1:p1` to `w2:p9`; fail either the destination lookup or its subscription once. The source is still removed and emits `(S, unknown, ended=True)`. Recovery seeds the destination after S's working state has already been lost.
  - Dispatch a buffered status event for the old pane before the move event. Its lookup finds the source empty and ends S even though S is live at the destination. This can occur across the independent status and lifecycle streams.
  - Reuse the source pane for B before resync, with the source listed first. Reconciliation ends S while processing B, then seeds S at the destination. Reordering absent-pane processing alone does not preserve the move.
  - Start with a session-less Codex working in `w1:p1`, terminal X, and a muted pending Slack task under `pending:term-X`. Move it to `w2:p9`, where it finishes idle and reports session REAL before the move is processed. The actual manager/bridge integration emits **zero transitions and zero posts**; the provisional task remains pending under its old pane. Matching provisional threads by terminal in the bridge cannot recover a transition the manager never emitted.
- **Impact:** Known-session false ends clear pending work and post an incorrect termination notice; provisional moves can leave completion undelivered. These arise before the independently reviewed M2 notification/delivery issues.
- **Suggested fix:** Reconcile relocation by session across the live snapshot before ending/replacing source state, and defer destructive handling when destination verification fails. Preserve terminal identity for provisional agents and transfer their active state through session discovery. Add manager-to-bridge move tests with pending muted work, destination failures, reversed event order, source reuse, and a session appearing at completion. The added provisional bridge test supplies an already-correct transition manually.

### R6. An obsolete lookup can cancel the current run's retry

- **Severity:** minor
- **Location:** `herdr_slackbot/events.py:304`, `herdr_slackbot/events.py:291`
- **Problem:** `_dirty.discard(pane_id)` in `recheck()` and `resync()` is outside the generation guards. Rejecting the later `_reconcile()` does not undo that earlier mutation.
- **Concrete failure scenario:** Hold the old dispatcher's A/done lookup beyond `stop()`'s join timeout and restart tracking working B. Let B finish, but fail its completion lookup so the new run marks the pane dirty. Release A's old lookup: B remains the known session, but its dirty flag disappears. Dispatching the scheduled `_Recheck` produces no callback; the manager still reports B working while live B is done. A subsequent full resync recovers it. This was reproduced with a barrier and an explicit retry dispatch.
- **Suggested fix:** Check the worker generation under the lock before clearing dirty state, including in resync. Extend the held-lookup test to establish new-generation retry work before releasing the old worker. The residual impact is a skipped lookup retry and delayed notification until full resync, rather than the original session replacement.

### N4. Numeric duration validation still allows a formatting exception

- **Severity:** minor
- **Location:** `herdr_slackbot/claude_session.py:105`, `herdr_slackbot/claude_session.py:261`
- **Problem:** `_duration_ms()` accepts positive infinity/NaN, and `format_duration(answer.duration)` executes after the exception handler in `agent_result()`. Consequently, not every transcript failure reaches the screen fallback.
- **Concrete failure scenario:** Append a complete `turn_duration` record containing the valid JSON numeric literal `"durationMs": 1e309` after a valid answer. Python decodes it as infinity. `agent_result()` raises **OverflowError** while rounding the duration, with **zero screen reads**. This was reproduced with a real temporary JSONL file.
- **Suggested fix:** Require finite duration values and treat invalid duration metadata as absent. Include answer-to-result formatting in the guarded transcript path. Test oversized exponent values and non-finite values as well as strings.

## New finding

### N5. Invalid newer message shapes can silently return the previous answer

- **Severity:** minor
- **Location:** `herdr_slackbot/claude_session.py:93`, `herdr_slackbot/claude_session.py:159`, `herdr_slackbot/claude_session.py:166`
- **Problem:** The new shape normalization turns an invalid message into an empty dictionary and then ignores it when finding the latest prompt/assistant. A newer user or assistant record can therefore disappear from turn selection. The reader treats an older answer as trustworthy instead of using the available screen result.
- **Concrete failure scenario:** Write a complete old prompt and terminal assistant answer `OLD ANSWER`, followed by a newline-terminated `{"type":"user","message":["malformed new prompt"]}`. Call the result reader without `since`, as for PC-originated completion. At `a1be1b4` it returns **source=jsonl, text=OLD ANSWER, screen reads=0**. The parent reader raises `AttributeError` on the same input; silently selecting the previous turn is introduced by this commit. The new malformed-record tests allow either JSONL or screen output without checking which turn the JSONL answer belongs to.
- **Suggested fix:** Distinguish safely ignorable optional metadata from malformed records that may establish a newer turn or answer. Such records should invalidate the candidate and trigger fallback. Add malformed-newer-prompt and malformed-newer-assistant tests that assert old text is never reported as the current result.
