# M2 re-review

Reviewed `598c7e5e1ed6b20dc34b291e4d85f7f5e53392e8` (`M2 fixes`) against `docs/SPEC.md`, including **Decisions added during implementation**, and findings #1–#11 in `docs/review/M2.md`. Issues in `docs/review/M1-recheck2.md` are excluded.

**Original findings: 8 fixed, 3 partially fixed.** No item is wholly unfixed. **Open findings: 0 blocker, 6 major, 0 minor**, comprising three remaining findings and three additional findings in the changed code.

Validation: `python -m pytest -q` in an isolated `git archive 598c7e5` extraction: **352 passed, 5 skipped**. Additional reproductions used real Bolt dispatch with mocked authorization, fake Herdr/Slack clients, thread barriers, and injected Slack failures before and after simulated acceptance. The new-agent provisional-binding case was also compared with the parent commit. No live Slack messages or Herdr mutations were sent. Source and repository tests were not edited; concurrent M1 changes were not used. All line references below refer to `598c7e5`.

## Status per original finding

| # | Status | Verification / remaining issue |
|---|---|---|
| 1: Herdr I/O before acknowledgment / expired modal triggers | **Fixed** | Commands acknowledge first and use `response_url`; both modals open a loading view before Herdr reads. Submission probes have a bounded wait. In an additional reproduction using the actual Bridge and default Bolt processing, a 3.4-second `list_agents()` returned HTTP 200 acknowledgment in under 0.01 seconds and subsequently delivered the response. Loading-view ordering and slow-submission tests pass. |
| 2: non-atomic admission and thread creation | **Partially fixed** | Queued and concurrent send-worker tests now submit one prompt and create one root. The notifier uses a different lock, so it can still race admission to create a second root for the same session; see R2. |
| 3: queued reply reaches replacement agent | **Fixed** | Named/unnamed queued thread replies, queued direct sends, and replacement between admission and delivery are rejected by session/terminal checks. Delivery addresses the pane and rechecks identity before each attempt. The new startup-wait identity regression is separately reported as N1. |
| 4: retry after unknown prompt outcome | **Fixed** | Stall/unknown outcomes with an empty, unreadable, or partially pasted screen now submit once and remain uncertain unless activity is observed. The screen only affects warning text. The retained fresh-agent retries are limited to the documented pre-input resolution errors and recheck identity. |
| 5: delayed completion clears newer task | **Partially fixed** | The original queued A-completion versus B-send reproduction passes: admission settles A first, and the old transition cannot clear B. However, resume can still use A's live snapshot to clear B after admission runs between its two lock sections; see R5. |
| 6: unrelated ended event adopts provisional thread | **Fixed** | Ended events return their existing session key without migration; live transition migration requires matching terminal IDs. Added old-ended and mismatched-live-terminal tests pass. The separate admission-path problem is N2. |
| 7: mute writes obsolete provisional key | **Fixed** | Transition-driven and restart-driven migration tests resolve the button through its channel/thread binding, mute the real entry, and update the button value. An unknown stale control creates no entry. |
| 8: resume and live completion both post | **Fixed** | Startup resume is queued on the notifier. Both processing orders post once. An additional barrier reproduction holding resume's lookup while a live completion runs also produced one result and cleared the task. Concurrent admission remains a separate gap under R5. |
| 9: transient Slack failure drops notification | **Partially fixed** | Pre-acceptance failures, rate limits with Retry-After, root-success/result-failure, and background retry cases pass. Accepted-but-unacknowledged posts are duplicated on retry (R9). A rekeyed provisional resume can also disappear from retry processing (N3). Retries are bounded to 30 attempts; no durable outbox was added. |
| 10: tracebacks bypass token redaction | **Fixed** | The configured stdout and file handlers use `RedactingFormatter`. Fabricated tokens are removed from rendered exception messages, chained exceptions, stack information, and ordinary messages. The corresponding tests pass. |
| 11: quoted Windows cwd split at spaces | **Fixed** | `Main cwd="D:\My Project" inspect files` preserves the entire cwd and prompt. Quoted workspace/target names, multiline prompt text, literal backslashes, and unmatched-quote errors pass. |

## Remaining findings

### R2. Admission and the notifier can still create two thread roots

- **Severity:** major
- **Location:** `herdr_slackbot/bridge.py:348`, `herdr_slackbot/bridge.py:740`, `herdr_slackbot/bridge.py:776`, `herdr_slackbot/bridge.py:979`, `herdr_slackbot/bridge.py:1009`
- **Problem:** `_admit()` protects root lookup/creation with `_admission_lock`, while notification-driven `_ensure_thread()` runs under `_lock(key)`. These do not exclude each other. The admission path acquires the session lock only later when writing pending state. Both callers can observe no root, post one, and overwrite the persisted binding.
- **Concrete failure scenario:** S has finished PC work and has no thread yet; its valid completion is queued. Start a Slack send to settled S and hold its first root post. Process the queued completion through `handle_transition()`: the notifier creates a root and posts the result. Release the send worker: it creates a second root and overwrites S's binding before prompting. A barrier reproduction produced roots `1000.0001` and `1000.0003`; the result belongs to the first, while persisted state points to the second. The first thread is orphaned even though only one prompt was submitted.
- **Suggested fix:** Use the same canonical session/terminal lock for root lookup, posting, binding, and migration in admission and notification paths. Reload state under that lock before creating a root. Add a send-worker versus notifier test, in addition to the existing two-send-worker test.

### R5. Resume can clear a newer task using an older snapshot

- **Severity:** major
- **Location:** `herdr_slackbot/bridge.py:1057`, `herdr_slackbot/bridge.py:1076`, `herdr_slackbot/bridge.py:1078`, `herdr_slackbot/bridge.py:1102`
- **Problem:** `handle_resume()` reads the live agent while holding the first session lock, then releases it and reacquires a lock before deciding what to do. It reloads `pending_task` without checking that this is the task whose live state it just read. Serializing resume with the notifier does not serialize it with admission workers. `_clear_task()` then receives the newer task's own ID, so its identity guard cannot protect that task.
- **Concrete failure scenario:** A is pending and done. Pause resume immediately after its first lock is released, retaining A's done snapshot. Admit B: `_complete_pending_now()` posts A, B is reserved, and B starts working. Resume then acquires its second lock, loads B, and evaluates A's done status. `last_result_seq` suppresses another A post, but resume still clears B. A barrier reproduction ended with `pending_task=None`, live status **working**, and only A's result posted.
- **Suggested fix:** Carry the original task ID across lookup and lock changes. Under the final canonical lock, require that the same task remains pending and that the snapshot belongs after its admission boundary; otherwise discard/restart the resume operation. Add a resume-versus-admission interleaving test. The existing resume-versus-transition tests do not cover this worker.

### R9. Retrying an accepted post duplicates messages and can orphan roots

- **Severity:** major
- **Location:** `herdr_slackbot/bridge.py:262`, `herdr_slackbot/bridge.py:348`, `herdr_slackbot/bridge.py:947`, `herdr_slackbot/bridge.py:1025`; `herdr_slackbot/slack_transport.py:97`, `herdr_slackbot/slack_transport.py:108`
- **Problem:** A connection failure/timeout is treated as a retryable post failure without distinguishing an unknown delivery outcome. Root bindings and result sequence markers are committed only after a successful response. If Slack accepted the request but the response was lost, retrying the same processing step issues another post; local state has no evidence of the accepted first post. The implementation's claim that retries never double-post is therefore too strong.
- **Concrete failure scenarios:** A fake transport records a successful result, then raises `SlackTransientError` once to simulate a lost response. `process_with_retry()` returns success after posting **two results** in the same thread. With the same fault on a send-confirmation root, worker retry creates **two roots**, retains only the second binding, and leaves the first root unbound. Both cases were reproduced. The added tests inject failures before accepting the post.
- **Suggested fix:** Distinguish definite rejection from ambiguous delivery. Give logical posts a stable identity and reconcile an unknown outcome before creating another root/result; retain enough operation state to recover the accepted root's binding. Add acceptance-then-response-loss tests for both roots and replies. Local sequence markers alone cannot deduplicate a remote side effect whose response was lost.

## Additional findings in the changed code

### N1. The new startup wait can adopt and prompt a replacement agent

- **Severity:** major
- **Location:** `herdr_slackbot/bridge.py:625`, `herdr_slackbot/bridge.py:629`, `herdr_slackbot/bridge.py:638`
- **Problem:** `_wait_settled()` repeatedly merges whatever agent currently occupies the pane into the original startup response, including session and terminal identity. `target_of(info)` is constructed only afterward. If the just-started agent exits and is replaced during the wait, the later admission/delivery checks verify the replacement against itself.
- **Concrete failure scenario:** Start agent A and return its known session with status unknown. During the first startup-wait sleep, replace A in that pane with idle B, using a different session and terminal. The wait adopts B, and the original `/new` prompt is sent to B. An injected replacement produced a thread keyed to **B** and a pending prompt `prompt intended for original`, with one submission to B's pane. No changed-agent notice was issued.
- **Suggested fix:** Capture the expected session/terminal immediately from `agent.start`. Preserve and verify that identity through startup/session waits and final admission; reject replacement instead of overwriting the expected identity. Add replacement-during-wait tests alongside the ordinary unknown-to-idle test.

### N2. Unified admission reuses a provisional entry belonging to another terminal

- **Severity:** major
- **Location:** `herdr_slackbot/bridge.py:365`, `herdr_slackbot/bridge.py:375`, `herdr_slackbot/bridge.py:379`, `herdr_slackbot/bridge.py:746`
- **Problem:** For a session-less agent, `_key_for()` accepts any provisional entry returned by pane lookup without comparing terminal IDs. The transition path now correctly requires terminal equality, so a task admitted into the wrong provisional entry cannot receive its subsequent notifications. The new-agent path now also uses this shared admission helper; previously it constructed its provisional key directly from the newly started terminal.
- **Concrete failure scenarios:**
  - Leave a persisted provisional entry for old terminal X at `w1:p1`, then send to a new session-less Codex Y occupying that pane. Admission posts the confirmation in X's old thread and stores Y's task under X. A valid working-to-idle transition for Y is ignored because its terminal differs. Reproduction: **zero results**, with pending work still under `pending:old-terminal`.
  - Seed an old provisional binding for a pane ID subsequently returned by `tab.create`, then run `/new ... kind=codex`. At this commit the first task is attached to the old terminal's thread. Running the same setup against the parent Bridge creates a separate root and a new-terminal key. Thus shared admission extends the bad reuse to `/new`. The ordinary-send pane-reuse problem predates this commit; the stricter transition check also makes its completion disappear.
- **Suggested fix:** Reuse a provisional entry only with positive terminal equality, including the session-less branch. A mismatched pane entry must not hide a matching terminal entry elsewhere or prevent creation of a new terminal key. Test both direct sends after terminal replacement and new-agent admission with a stale persisted pane binding. These reproductions involve terminal replacement, not the M1 move-reconciliation issues.

### N3. A resume retry loses its target after provisional migration

- **Severity:** major
- **Location:** `herdr_slackbot/bridge.py:955`, `herdr_slackbot/bridge.py:1057`, `herdr_slackbot/bridge.py:1068`
- **Problem:** `_Resume` retains the provisional key while `handle_resume()` rekeys state to the real session before posting. If that post raises a transient error, retry invokes `handle_resume()` with the now-deleted provisional key. It finds no pending task and returns successfully, leaving the real entry pending with no retry scheduled.
- **Concrete failure scenario:** Persist a pending task under `pending:term-X`; live Herdr reports matching terminal X, session REAL, status done. Process `_Resume('pending:term-X')` and fail the first result post once before acceptance. The first attempt migrates state and fails; the second reports success without posting. Reproduction: **zero posts**, only key REAL remains, its task is still pending, and `process_with_retry()` returns **True**. Slack has already recovered; no further event is needed or guaranteed from the settled agent.
- **Suggested fix:** Resolve retry work through a stable task/thread identity or update its canonical key when migration occurs. A missing old key must not count as successful delivery when the same pending task moved. Add a provisional-resume test that migrates, fails one post, then delivers exactly once on retry without restart or another agent transition.
