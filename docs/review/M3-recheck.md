# M3 re-review

Reviewed `cc0cc5fbc3ab289667144ccbf396039bbfe9c2f8` (`M3 fixes`) against `docs/SPEC.md` and findings **#1–#4** in `docs/review/M3.md`. Read `docs/review/M2-recheck2.md`; its findings and the coder's concurrent changes are excluded. All source locations below refer to the reviewed commit.

**Requested items: 1 fixed, 3 partially fixed. Open findings: 0 blocker, 4 major.**

Validation: **471 passed, 5 skipped** with `python -m pytest -q` in an isolated `git archive cc0cc5f` extraction. Additional probes used the existing fake CLI, real temporary instance/lifecycle locks, four separate launcher processes, injected interleavings/errors, and a disposable real Windows PowerShell process. No source or repository tests were edited, no live Slack requests were sent, and no Herdr commands were executed. Only this review was added to the checkout.

## Status per requested item

| Item | Status | Verification / remaining issue |
|---|---|---|
| #1: stop/restart ownership | **Partially fixed** | The original server-A/server-B collision regression passes: keys route to A and B is untouched. Runtime PID/creation-time checks, terminal replacement rejection, status location checks, and rechecking before fallback termination also pass. However, the bridge can exit during the pane lookups and stop still sends Ctrl+C to its former terminal; see R1. |
| #2: launch into an occupied pane | **Partially fixed** | Saved panes with child processes, agents, another foreground executable, changed terminals or another server get a fresh tab. Unrecorded existing panes are also avoided. A shell running its own interactive command still passes the new idle check; see R2. |
| #3: concurrent launch idempotence | **Partially fixed** | Supplied start/start and start/restart regressions pass. Four separate simultaneous launcher processes sharing one state directory produced **one workspace and one `pane run`**. The reservation-to-runtime handoff is not synchronized with launch's two checks (R3), and uncertain CLI failures incorrectly clear the reservation (N1). |
| #4: setup blank identity values | **Fixed** | Repeated the original case with blank `SLASH_COMMAND` / `BOT_DISPLAY_NAME`, username `me`, and overrides `/herdr-custom` / `Custom`. Blanks remain preserved; the manifest, returned setup values and runtime all use `/herdr-me` / `Herdr (me)`. Notes explain that both overrides were not applied. The nonblank-preservation regression also passes. |

## Remaining findings

### R1. Stop checks terminal identity again, but not whether the bridge still owns it

- **Severity:** major; remaining part of #1.
- **Location:** `herdr_slackbot/plugin.py:350`, `herdr_slackbot/plugin.py:437`, `herdr_slackbot/plugin.py:440`.
- **Problem:** `verified_bridge()` runs before the CLI pane queries. Both subsequent `verified_pane()` calls check only socket/pane/terminal identity. A terminal survives the bridge's exit and can return to its shell or run another program without changing its terminal ID. The lifecycle lock does not serialize the bridge's exit or user activity. The next runtime-lock check happens only after keys have already been sent.
- **Reproduction:** Start with a real held instance lock and a matching fake runtime/pane record. On the first routed `pane get`, release the bridge lock and make that shell host `editor.exe`, retaining its terminal ID. Both pane lookups succeed. `stop()` returns **0** and records **`pane send-keys wE:p1 ctrl+c`**, although the bridge lock is already free. This exercises an exit during a CLI round trip, not an assumed pane-ID reuse.
- **Fix direction:** Recheck the pinned process and held lock immediately before input, and establish that the current terminal process belongs to that bridge. For a strict ownership guarantee, use control addressed to the verified bridge process rather than terminal-wide input when ownership cannot be established atomically. Add an exit-during-pane-lookup regression; the existing replacement-during-stop-wait test covers a later interval.

### R2. No child processes does not establish that PowerShell is at its prompt

- **Severity:** major; remaining part of #2.
- **Location:** `herdr_slackbot/plugin.py:313`, `herdr_slackbot/plugin.py:325`, `herdr_slackbot/plugin.py:418`.
- **Problem:** `idle_shell()` treats a known foreground shell with no agent and no non-console child processes as available for a command. PowerShell runs cmdlets and scripts inside that same process. In particular, `Read-Host` consumes terminal input while the foreground executable/PID and terminal identity remain unchanged and the shell has no children.
- **Reproduction:** Spawn a disposable real `powershell.exe -NoProfile -Command` executing:

  ```powershell
  $answer=Read-Host 'review-probe'; [Console]::WriteLine('consumed=' + $answer)
  ```

  Point the fake saved pane's shell PID at this process and use the real `procinfo.busy_children`. It returns **`[]`**; `idle_shell()` returns **`powershell`**. Launch reuses this pane, creates **no new tab**, and emits its Python command. Feeding that exact command to the waiting test shell prints **`consumed=<entire launch command>`** and exits successfully: the command was accepted as an answer, not executed. No live Herdr pane was involved.
- **Fix direction:** Require positive shell-prompt readiness from an integration that can establish it, or create a fresh dedicated tab when readiness is unknown. Child enumeration remains useful for rejecting busy panes but cannot prove that a shell is idle. Add an in-process shell-command case alongside the existing external-child case.

### R3. Acknowledgement can fall between launch's lock and reservation checks

- **Severity:** major; remaining part of #3.
- **Location:** `herdr_slackbot/plugin.py:375`, `herdr_slackbot/plugin.py:379`; `herdr_slackbot/__main__.py:147`, `herdr_slackbot/__main__.py:175`.
- **Problem:** The lifecycle lock serializes launchers, but the child acquires its runtime lock and clears the reservation without participating in that synchronization. A second launcher can observe no runtime lock, then observe an acknowledged reservation after the first child starts. It proceeds with another launch while the first bridge is running.
- **Reproduction:** Launch once and keep its reservation active. During the second launch, immediately after the real `running_pid()` check returns `None`, acquire the first child's real instance lock, record its identity, mark its shell as having a Python child, and acknowledge its launch ID. Resume the second launcher. Result: **runtime lock held, two `pane run` calls, one extra tab**. The second launcher even recognizes the original pane as busy, then launches another child in the new tab. The instance lock can reject that child later; it does not undo these side effects.
- **Fix direction:** Coordinate reservation acknowledgement and launch admission, or order/recheck the observations so the reservation-to-runtime handoff cannot appear as neither starting nor running. Add this child/launcher interleaving; concurrent launcher-only tests do not cover it.

## Additional finding in the new reservation code

### N1. A CLI timeout is treated as proof that no launch was accepted

- **Severity:** major; another gap in #3's new reservation handling.
- **Location:** `herdr_slackbot/plugin.py:65`, `herdr_slackbot/plugin.py:392`.
- **Problem:** `HerdrCli.run()` wraps `subprocess.TimeoutExpired` as `PluginError`. `_launch()` clears its reservation for every `PluginError`, with the comment “nothing was started.” A CLI timeout or lost response can occur after Herdr has accepted the terminal input. Killing/timing out the CLI does not establish that the server never submitted the command.
- **Reproduction:** Have the fake CLI record the first accepted `pane run`, then raise `PluginError` representing a lost response before the child acquires its runtime lock. The reservation becomes **`active: false`**. An immediate retry submits **a second `pane run` to the same pane**. This differs from the supplied failed-call regression, which assumes failure before acceptance. Acceptance followed by a timeout was simulated, not induced on a live server.
- **Fix direction:** Distinguish definite rejection from uncertain delivery. Retain the reservation on uncertain failures and reconcile through the child acknowledgement/runtime identity before permitting another launch. Add an accepted-command/response-loss case. This is a defect in the newly added guard; duplicate launches following CLI failure were not prevented by the old implementation either.

## Other lifecycle observation and limits

- A stop during the new `starting` state returns success with “bridge is not running,” leaves the reservation active, and allows the pending child to start afterward. A fake/real-lock probe reproduced `starting → stop(0) → running`. The pre-lock stop window already existed, so it is not counted as an additional introduced regression here. Cancellation or waiting for the pending start would make stop's result reliable.
- Actual multi-server routing, live terminal input, server restart and Slack startup were not exercised. The existing fake routing tests and native process-helper tests pass; they do not prove all live integration behavior.

**Conclusion:** Close #4. Keep #1–#3 open for the reproduced ownership, shell-readiness and startup-handoff gaps. The new reservation code also needs to preserve uncertain launch outcomes. M2-recheck2 findings are not repeated here.
