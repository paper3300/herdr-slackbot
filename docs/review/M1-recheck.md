# M1 re-review

Reviewed `git diff d7798fe a3fe606`, the revised `docs/SPEC.md`, milestone notes, implementation, and tests.

**Original findings:** 8 fixed, 3 partially fixed; #9 and #10 excluded by the user's confirmed spec decisions. No finding is wholly unfixed.

**Open issues:** 0 blocker, 5 major, 2 minor. This includes the 3 remaining issues below and 4 new findings (3 major, 1 minor).

Validation: `python -m pytest -q` → **194 passed, 5 skipped**. Additional isolated reproductions used the test fakes, thread barriers, and temporary files. Live tests were not enabled. Source and test files were not edited.

## Status of each original finding

| Finding | Status | Verification / remaining issue |
|---|---|---|
| #1: mutation repeated through CLI | **Fixed** | EOF and write/read failures after submission now raise `HerdrOutcomeUnknown`; mutating methods are not retried through the CLI. Tests cover prompt/start/create, write failure, safe read retry, failure before submission, and CLI timeout. |
| #2: snapshot overtakes buffered events | **Fixed** | The original reconnect scenario now emits one working-to-done transition. Buffered blocked/done hints fetch the current snapshot and cannot replay older statuses over it. The new approach introduces a separate lost-task regression, N1 below. |
| #3: old event assigned to replacement session | **Fixed** | The manager retains session identity and ends A before seeding B. Both queued A events and detection on an already subscribed pane have regression tests. Cross-generation stale lookups remain an issue under #6. |
| #4: replayed destructive lifecycle hints | **Fixed** | Close, exit, and old-pane move hints now verify live state. Tests preserve a live subscription and its working status, including when lookup fails. Actual moves introduce the separate N2 regression below. |
| #5: failed lookup consumes completion | **Fixed** | Lookup failure leaves state unchanged, marks the pane dirty, and schedules a retry. Retry and healthy-stream resync recover one correctly attributed completion in the original scenario. |
| #6: shutdown/in-flight subscription race | **Partially** | Stream installation checks its generation, and the original stop-during-open case passes. An old dispatcher can still survive a timed-out join and apply a stale lookup to a restarted manager; see R6. |
| #7: failed save changes memory | **Fixed** | `_commit()` writes a candidate copy before publishing it. Tests cover failed clear/insert/remove and a subsequent unrelated save. Failed counter commits leave memory unchanged and deliberately burn the reserved high-water number. |
| #8: counter reuse after corruption | **Partially** | A valid high-water file preserves numbering, and immediate unrecoverable allocation is blocked. The disabled state is not durable, allowing a later restart to issue 1; see R8. |
| #9: mute suppresses PC blocked alert | **Excluded — user decision** | The appended spec explicitly allows this behavior. Not re-raised. |
| #10: PC blocked-to-done completion | **Excluded — user decision** | The appended spec explicitly requires this behavior. Not re-raised. |
| #11: caller-owned mutable state | **Fixed** | Incoming fields are deep-copied. Tests mutate the original task and nested list and confirm both stored and persisted values remain unchanged. |
| #12: PID write failure leaks lock | **Fixed** | Acquisition now releases the OS lock on PID publication failure. The fault-injection test confirms another instance can immediately acquire it. |
| #13: notice filtering removes code | **Partially** | The original deeply indented `important_call()` and Codex cases pass. The broader character-class heuristic still removes valid punctuation-led trailing content; see R13. |

## Remaining issues from the original review

### R6. An old dispatcher can mutate the restarted manager

- **Severity:** major
- **Location:** `herdr_slackbot/events.py:231`, `herdr_slackbot/events.py:282`, `herdr_slackbot/events.py:327`
- **Problem:** `stop()` returns after a bounded join even if the dispatcher is still inside `agent.get`. The generation check protects stream installation, but not lookup results, reconciliation, or callbacks. The old dispatcher subsequently uses the current shared `_subs` and `_panes`.
- **Concrete failure scenario:** Start with session A working. Block the dispatcher's lookup after capturing A's done snapshot, call `stop()` until its join times out, replace A with B, and restart. The new manager correctly tracks B. Release the old lookup: it sees the new subscription, reconciles the stale A snapshot, emits an ended transition for B, and changes `known_session()` back to A. A barrier-controlled reproduction produced exactly `B -> A` and `(session=B, status=unknown, ended=True)` from the old worker.
- **Suggested fix:** Carry the run generation through dispatcher operations and reject obsolete results before every state mutation and emission. Alternatively, prevent restart until all old workers have actually terminated, with cancellable/bounded RPCs. Add a stop/start test with an outstanding lookup held beyond `join_timeout`; assert B remains tracked and receives no old-generation callback. The current restart test stops idle workers only.

### R8. Counter recovery restrictions disappear across restarts

- **Severity:** major
- **Location:** `herdr_slackbot/state.py:107`, `herdr_slackbot/state.py:122`, `herdr_slackbot/state.py:133`
- **Problem:** Unrecoverable counter state is represented only by the in-memory `_counter_ok` flag. The corrupt state file is moved aside. A subsequent load can treat the directory as new, or trust a newly written zero counter despite an unreadable high-water file.
- **Concrete failure scenario:** With corrupt legacy state and no high-water file, the first load disables allocation and moves the file away. Restart again: both primary files are missing, so allocation succeeds with 1. This was reproduced. Also reproduced with both files corrupt: the disabled store performs an unrelated `upsert_thread()`, persisting counter 0; reopening trusts this valid JSON despite the corrupt high-water file and returns 1. Both paths bypass `recover_counter()` and can reuse issued names.
- **Suggested fix:** Persist the recovery-required condition and retain it across unrelated writes and restarts. Do not equate missing primary files with a pristine store when corruption evidence exists. Require verified recovery before clearing that condition. Add second-restart and unrelated-write-then-restart tests for missing and corrupt high-water files.

### R13. The replacement notice heuristic still deletes response content

- **Severity:** minor
- **Location:** `herdr_slackbot/parser.py:44`, `herdr_slackbot/parser.py:105`
- **Problem:** `NOTICE_RE` matches any punctuation-led line indented at least 20 spaces, including `)`, `}`, comments, and bullets. Restricting removal to trailing lines reduces the affected area but does not identify an actual UI notice.
- **Concrete failure scenario:** A Claude capture without a turn-end marker contains `call(` followed by a closing `)` aligned with 42 spaces, immediately above the prompt box. `fallback_tail(..., agent_kind='claude')` returns only `call(`. This was reproduced. The raw fallback still loses a nonblank code line.
- **Suggested fix:** Match known UI notice content rather than a generic non-word character, preserving unknown trailing lines. Add a fallback test with aligned closing punctuation and no turn-end marker, alongside an actual update notice. The new tests protect identifier-led code but do not cover this case.

## New findings

### N1. Snapshot-only status handling loses complete fast tasks

- **Severity:** major
- **Location:** `herdr_slackbot/events.py:299`, `herdr_slackbot/events.py:343`, `herdr_slackbot/events.py:349`
- **Problem:** Status event payloads are discarded in favor of the state returned by a later lookup. If a whole working/settled cycle completes before dispatch, the manager loses the evidence that work occurred. A larger sequence number does not help: equal before/after statuses emit nothing, and idle-to-done does not satisfy the notification completion rules.
- **Concrete failure scenario:** Seed an idle pane, queue working and done events for the same session, and let both dispatch after live state has reached done. The manager emits only idle-to-done; `decide_notification()` returns `none` even with a pending Slack task. Starting from done emits no transition at all. Both were reproduced. A short task or a dispatcher delayed by another callback therefore never sends its required completion and can leave its pending task set.
- **Suggested fix:** Preserve evidence of activity from queued events for a verified session/subscription generation while retaining snapshot deduplication. Reconcile that activity with the settled snapshot so a completed task is not reduced to an idle/done snapshot comparison. Add batched working/done and working/idle tests without waiting for a callback between events, including initial done and muted pending Slack work. The threaded test currently waits for the working callback before allowing completion, masking this regression.

### N2. Moving an agent incorrectly ends its session and clears pending work

- **Severity:** major
- **Location:** `herdr_slackbot/events.py:310`, `herdr_slackbot/events.py:357`
- **Problem:** A move rechecks the old pane first. Because the agent is now absent there, `_apply_absent()` emits an ended transition. The destination then seeds the same session as if first seen. The code equates departure from a pane with the end of the agent session, although the spec binds threads and work to sessions.
- **Concrete failure scenario:** Move working session S from `w1:p1` to `w2:p9`. The manager emits `(S, working -> unknown, ended=True)` while S continues working at the destination. Passing that transition to the existing notification decision clears S's pending Slack task. A later completion can then be suppressed by mute or by ending in idle. The move and pending-clear behavior were reproduced; the existing move test checks only subscription locations.
- **Suggested fix:** Reconcile source and destination together, transferring the tracked status/session when the destination contains the same session. Apply equivalent relocation handling during full resync, and emit ended only when the session actually disappears or is replaced. Test a moved working session with a muted pending task and require one eventual completion without clearing pending work during the move.

### N3. Incomplete JSONL output is accepted as the final result

- **Severity:** major
- **Location:** `herdr_slackbot/claude_session.py:120`, `herdr_slackbot/claude_session.py:145`, `herdr_slackbot/claude_session.py:195`
- **Problem:** `_decode()` silently discards an incomplete trailing record, and the caller cannot distinguish a complete transcript from a read taken during an append. The reader also accepts assistant text with a missing/null stop reason, rejecting only `tool_use`. `agent_result()` treats such text as authoritative and skips the screen fallback.
- **Concrete failure scenario:** A final assistant message occupies multiple JSONL records with the same message ID. The first text block is complete but the second record is only partially written when read. A file-based reproduction returned `source='jsonl'`, text `first block`, and zero screen reads even though a second block was visibly incomplete at EOF. A partial newer prompt similarly returned the previous answer; a message with `stop_reason=None` returned its partial text.
- **Suggested fix:** Preserve information about incomplete trailing records and require evidence that the selected response is complete before trusting it. Retry a bounded number of times while an append is unfinished, then return `None` to use the fallback if completeness remains uncertain. Add file-based tests for partial next-message records, partial subsequent blocks of the same message, and unterminated assistant output; assert that partial/old text is not accepted as the final JSONL result.

### N4. Structurally invalid JSONL records prevent the promised fallback

- **Severity:** minor
- **Location:** `herdr_slackbot/claude_session.py:86`, `herdr_slackbot/claude_session.py:160`, `herdr_slackbot/claude_session.py:186`
- **Problem:** Syntactically valid records are used without validating nested message shapes or duration values. `last_answer()` catches only `OSError`, so parsing errors escape `agent_result()` instead of selecting the screen source.
- **Concrete failure scenario:** A readable transcript contains a valid prompt/answer followed by a `turn_duration` record with `durationMs: "bad"`. `agent_result()` raises `ValueError` instead of invoking the supplied screen callback; this was reproduced. A non-dictionary `message` can similarly raise `AttributeError`. The current malformed-file test covers only invalid JSON syntax.
- **Suggested fix:** Validate record shapes and optional scalar fields before using them. Treat unusable transcript data as unavailable, or ignore invalid optional duration data while keeping a trustworthy answer. Add malformed nested-message and duration tests that assert the result path remains usable and invokes the fallback when the answer cannot be trusted.

## New spec features checked

- **Claude JSONL:** File lookup, current-turn/message grouping, fixture results, interruption/tool-use fallback, and the normal JSONL-first path are covered and pass. N3 and N4 identify remaining failures in deciding whether file content is usable.
- **Codex options:** The implementation and tests cover all five specified effort values, default high effort, listed model visibility, static fallback, configured default model, and no Codex permission-mode field. No additional actionable finding in this portion of the diff.
