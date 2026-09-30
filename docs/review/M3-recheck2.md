# M3 second re-review

Reviewed `915760b1b426da719a88804a0e01c4b4c2f67c56` (`M3 recheck fixes`) against **R1, R2, R3 and N1** in `docs/review/M3-recheck.md`. All source locations refer to that commit. M2-recheck3 findings and concurrent coder changes are excluded.

**Status: 2 fixed, 2 partially fixed. Findings: 0 blocker, 2 major; 1 minor listed briefly below.**

Validation: **501 passed, 5 skipped** with `python -m pytest -q` in an isolated `git archive 915760b` extraction. Additional probes used fake panes/CLI results, real temporary locks, and a separate Python process running the actual bridge entry point with Herdr/Slack I/O replaced. The installed Herdr CLI was also checked through read-only help/schema commands and a disposable fake named-pipe server. No source or repository tests were edited, no mutating Herdr commands were issued, and no live Slack calls were made. Only this review was added to the checkout.

## Status per requested item

| Item | Status | Verification / remaining issue |
|---|---|---|
| **R1: stop sends keys after the bridge exits** | **Fixed** | Stop now writes a PID/creation-time-addressed request and makes no terminal calls. The exit-after-verification regression passes without keys or termination. Address mismatch and fallback PID rechecks pass. A disposable child using the real `__main__.main()` startup/polling/cleanup code honored the request: **stop=0, child exit=0, zero stop CLI calls, runtime lock released, reservation inactive, stop request removed**. Herdr and Slack operations in that child were faked. |
| **R2: shell with no children is mistaken for an idle prompt** | **Partially fixed** | The original `Read-Host` screen is rejected and gets a fresh tab; ordinary bare prompts can be reused. Existing occupied-pane, custom-prompt and typed-text cases pass. However, the new PowerShell regex also accepts an unfinished command ending in `>`; see R2 below. |
| **R3: child acknowledgement races between launch checks** | **Fixed for the reviewed handoff race** | The child takes the same lifecycle lock for admission, runtime-lock acquisition, identity publication and acknowledgement. The supplied interleaving now blocks the child's acknowledgement until the second launcher has observed the reservation, yielding one `pane run`. Concurrent start/start and start/restart tests pass. Pending-stop cancellation and the actual cancelled-child entry-point test pass within the reservation lifetime. Expired-start cancellation has a smaller gap, listed under minors. |
| **N1: uncertain CLI outcome clears the reservation** | **Partially fixed** | Python's outer `subprocess.TimeoutExpired` now becomes `PluginUncertain`; an immediate retry remains a no-op. Missing executables/definite failures still release the reservation. But a CLI that exits nonzero after losing an RPC response is treated as a definite rejection and permits another launch; see N1 below. |

## Major findings

### R2 remains: the PowerShell prompt regex accepts unfinished redirection commands

- **Severity:** major.
- **Location:** `herdr_slackbot/plugin.py:54`, `herdr_slackbot/plugin.py:55`, `herdr_slackbot/plugin.py:395`.
- **Problem:** `^PS [^\n]*> ?$` allows additional `>` characters and arbitrary command text between the initial `PS ` and the final `>`. It therefore does not establish that nothing is typed after the prompt. The sh-family patterns likewise use an end-of-line suffix search rather than recognizing an empty command line.
- **Realistic failure scenario:** After the bridge exits, the user starts typing an output-redirection command in its PowerShell pane, such as `Get-Process >`, and invokes the plugin's start action before finishing the filename. The shell has no child process or agent and retains its original terminal identity. The launch command is appended to the user's unfinished input and Enter submits the combined line, potentially executing the pending command and misdirecting the bridge invocation. This does not require a program deliberately spoofing a prompt.
- **Reproduction:** Seed a previous-launch record for a matching fake pane with an idle PowerShell process and screen `PS D:\Work> Get-Process >`. `at_prompt()` returns **True**; `launch()` reuses that pane and creates **zero new tabs**. The control `PS D:\Work> ` also reuses it, while the original `review-probe: ` Read-Host screen correctly creates a new tab.
- **Fix direction:** Reject text after the actual prompt delimiter, including incomplete redirections. Tighten PowerShell matching and avoid treating any sh-family line ending in `$`, `#` or `%` as an empty prompt. When readiness cannot be established, use a fresh tab. Add these partial-command cases to the pane-selection regressions.

### N1 remains: a nonzero CLI exit does not prove the command was refused

- **Severity:** major.
- **Location:** `herdr_slackbot/plugin.py:96`, `herdr_slackbot/plugin.py:98`, `herdr_slackbot/plugin.py:467`.
- **Problem:** Only the Python subprocess timeout is classified as uncertain. Every nonzero CLI exit becomes ordinary `PluginError`, and `_launch()` clears the reservation with the assumption that Herdr refused the command. The CLI can instead fail while receiving/decoding a response after submitting the RPC.
- **Native read-only verification:** An isolated fake named-pipe server answered the installed CLI's preliminary `ping`, received its **`pane.get`** request, and closed that connection without replying. `herdr pane get w1:p1` exited **1** with:

  ```text
  Error: Custom { kind: Other, error: EmptyResponse }
  ```

  Thus the installed CLI does report response loss as a nonzero exit; it does not necessarily wait for Python's outer timeout. This experiment used a read-only request and never connected that command to the user's server.
- **Realistic failure scenario:** Herdr accepts `pane run`, but that request's connection closes before its reply reaches the launcher. The child is still starting and has not acquired its runtime lock. The launcher clears its reservation, so retrying start can create another tab and submit another bridge command. The eventual instance lock does not undo those launch side effects.
- **Launch reproduction:** Have the fake CLI accept the first `pane run`, mark the original shell as hosting a starting Python child, then feed the real `HerdrCli.run()` the native exit-1/`EmptyResponse` result above through a subprocess stub. It raises **`PluginError`**, leaves **`active: false`**, and an immediate retry produces **two `pane run` calls and one extra tab**. Acceptance of the mutating command was simulated; only the read-only CLI response-loss behavior was exercised natively.
- **Fix direction:** Release a reservation only on evidence of definite rejection or failure to start the CLI. Classify transport/decoding failures and unrecognized abnormal exits as uncertain for mutating calls, retaining the reservation until acknowledgement, cancellation or bounded recovery. Add a nonzero-exit-after-acceptance regression alongside the outer-timeout case.

## Minor issue

- **Expired starts survive stop:** `reservation()` ignores records older than 60 seconds (`plugin.py:402`), so `_stop()` returns success with “bridge is not running” without cancelling them. `launch_admitted()` checks neither expiration nor `active` (`plugin.py:267`). Probe: launch, advance the fake clock 61 seconds, stop → **0**, then `launch_admitted(old_id)` → **True**, with the record still active. A child delayed by suspension or slow startup can therefore begin after that stop. Cancel the outstanding token independently of whether its launch-blocking TTL has expired, or reject expired tokens at admission. The original handoff race itself is fixed.

## Conclusion

Close R1 and the R3 handoff race. Keep R2 and N1 open for the reproduced partial-command and nonzero-response-loss cases. No additional blocker/major regression was demonstrated outside these remaining gaps. The new expired-start cancellation edge is minor. M2-recheck3 issues are not repeated here.
