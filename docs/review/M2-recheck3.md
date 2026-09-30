# M2 third re-review

Reviewed `3a16f93e3ccaece5ea32d0756bba999a0e750587` (`M2 recheck2 fixes`) against **N1, R9a, R9b, N4 and N5** in `docs/review/M2-recheck2.md`, including the new `docs/LIVE_TEST.md`. All source locations refer to that commit. The concurrent M3 fixes and findings in `M3-recheck.md` are excluded.

**Status: 3 fixed, 1 partially fixed, 1 mitigated pending live validation. Findings: 0 blocker, 1 major; minor issues listed briefly below.**

Validation: **492 passed, 5 skipped** with `python -m pytest -q` in an isolated `git archive 3a16f93` extraction. Additional offline probes used the real Bridge/transport, fake API pages and agent observations, temporary persisted state, and injected write failures followed by a fresh Bridge/StateStore. The suite includes the real Slack SDK with only its HTTP operation stubbed. No source or repository tests were edited, and no live Slack requests or Herdr commands were sent. Only this review was added to the checkout.

## Status per requested item

| Item | Status | Verification |
|---|---|---|
| **N1: missing startup session discards pinned identity** | **Fixed** | `_observe()` preserves the known session/terminal, and `/new` constructs its admission target from `expected`. The original A → absent session → same-terminal B regression sends no prompt and creates no B task. Additional probes: an identity that stays absent at admission sends **0** prompts; the same pinned identity reappearing sends **1** prompt. Session-less startup coverage still passes. |
| **R9a: SDK retries an accepted post internally** | **Fixed** | Wrapping the actual SDK client clears `retry_handlers`. The response-loss regression makes **one HTTP attempt** and raises `SlackUncertainError`; its unwrapped control makes two attempts. A connection-refused error remains retryable without being classified as accepted/uncertain. Production wraps the client before Bolt uses it. |
| **R9b: marker retention not established** | **Mitigated; live validation outstanding** | Posts with blocks now carry both metadata and a `block_id` marker. Offline tests recover from block-only retention and defer when bot messages have no visible markers. The new diagnostic covers a root and a reply. No real-app round trip was performed, so the original integration question is not fully closed. This is a validation gap, not a newly demonstrated major platform failure. |
| **N4: incomplete history treated as absence** | **Partially fixed** | Ordinary two-page lookups, failed pages, the ten-page cap and early-miss deferral pass. The new loop still stops despite a continuation cursor if `has_more` is absent/false, and accepts `has_more=true` without a cursor as complete. The first case reproduced a duplicate result; see the major finding below. |
| **N5: intent removed before durable completion** | **Fixed** | Accepted timestamps remain in the intent until binding/result/task updates remove it in the same `upsert_thread`. Supplied root-binding and result-commit interruption regressions pass. Additional fresh-process probes interrupted **accepted-timestamp persistence itself** and **the final commit after successful reconciliation**. Both recovered with **one result total**, one reconciliation read total, no pending task and no remaining intent. Remote lookup correctness remains subject to N4/R9b; the local write-order defect is fixed. |

## Major finding

### N4 remains: the loop requires two pagination signals to agree before continuing

- **Severity:** major.
- **Location:** `herdr_slackbot/slack_transport.py:215`, `herdr_slackbot/slack_transport.py:216`; repost at `herdr_slackbot/bridge.py:291`.
- **Problem:** The loop breaks on `not resp.get("has_more") or not cursor`. A nonempty `response_metadata.next_cursor` is sufficient evidence that another cursor page exists, but a missing or false `has_more` overrides it. Conversely, a response explicitly reporting more results without a usable cursor falls through to `None`, rather than an incomplete-scan error. Slack documents cursor continuation separately from time-based pagination; its cursor contract directs callers to follow `next_cursor`. [Slack pagination](https://docs.slack.dev/apis/web-api/pagination/), [history pagination](https://docs.slack.dev/reference/methods/conversations.history/#pagination), [replies pagination](https://docs.slack.dev/reference/methods/conversations.replies/#pagination).
- **Realistic failure scenario:** An accepted root/result loses its HTTP response. Recovery scans a busy DM or agent thread, and the accepted marker lies on a later page. The first response supplies a continuation cursor without the additional `has_more` flag. Recovery treats that partial page as a complete miss; after the five-second age guard, it posts the message again and commits completion. This requires response loss and pagination, not marker stripping or an arbitrary agent replacement.
- **Offline reproduction:** Use the existing `PagedWeb`/`_msg` helpers with these pages:

  ```python
  [
      {"messages": [_msg("1.1", "other")],
       "response_metadata": {"next_cursor": "page2"}},
      {"messages": [_msg("1.2", "result:t")]},
  ]
  ```

  Both root and reply lookups returned **`None` after one request**, leaving page two unread. An end-to-end Bridge probe accepted a result and raised a connection-reset error, advanced the fake clock six seconds, then supplied this pagination shape. Recovery made **two result posts, one history/replies read, cleared pending state, and never requested page two**. A separate `has_more=true`/no-cursor response also returned `None`. These response shapes were simulated; no live Slack response was captured.
- **Fix direction:** Follow every nonempty continuation cursor independently of `has_more`. If the response says more results exist but offers no usable continuation, preserve the uncertain outcome (or implement the documented time continuation). Test both history and replies, plus the Bridge's no-repost behavior for an incomplete scan. Do not require a live duplicate to verify this control-flow correction.

## New-code review and minor issues

No additional blocker/major regression was demonstrated outside the remaining N4 defect. The new persistence paths were checked beyond their supplied tests, including failure before an accepted timestamp becomes durable and failure after lookup succeeds.

- **Minor — diagnostic failures escape the promised report.** `live_check.py:37`, `:40` and `:48` perform DM opening, posting and the initial history read outside the error handler around `find_message()`. A first-run token missing `im:history`, or a transient read failure, can produce a traceback instead of the `root`/`reply` and `PASS`/`FAIL`/`UNKNOWN` output described in `LIVE_TEST.md:40–43`. Injecting `SlackPermanentError("missing_scope")` at `recent_messages()` raised that exception with **zero diagnostic lines**. Catch/report failures across the diagnostic, including which step failed; do not label every failure as marker loss.
- **Minor — §3 overstates the guarantee.** `LIVE_TEST.md:43` says failed/unknown marker checks mean no duplicates. The diagnostic is not saved as a runtime capability flag, and the runtime can repost after a lookup returns `None` once the intent is five seconds old. N4 demonstrates a concrete exception. Describe deferral for an incomplete lookup and leave the no-duplicates claim conditional on reliable reconciliation; a failed diagnostic does not switch the bridge into a different mode.

## `docs/LIVE_TEST.md` sanity check

- **Commands/actions:** `check` and `marker-check` exist in `__main__.COMMANDS`; plugin actions are exactly `start`, `stop`, `restart`, `status`. `start` correctly maps to the Python `launch` command through the wrapper. Slash forms for bare usage, `list`, `status`, `new <workspace> <prompt>`, and `send <name> <prompt>` match the parser and handlers.
- **Paths:** The documented `.venv\Scripts\python` and `scripts\setup.ps1` are relative to the plugin root, as the introduction says. `.env` and `slack-app-manifest.json` belong in the plugin config directory. Logging uses the effective `STATE_DIR\bridge.log`; with blank/default `STATE_DIR`, this is `<config-dir>\state\bridge.log`.
- **Expected output/features:** `check` prints `slack config: ok` when required values are present; it does not authenticate to Slack. The five listed bot scopes match the generated manifest. Socket Mode, interactivity, `message.im`, modal actions, mute/full-result controls, outside-thread ephemeral usage, and the non-owner rejection text match the implementation. `marker-check` really posts a root and reply; its normal success output matches the checklist.
- **Limits:** The checklist is a test procedure, not evidence that the real app or marker round trip passed. Its §10 lifecycle behavior belongs to the ongoing M3 review and is not re-raised here. The two minor §3 corrections are listed above.

**Conclusion:** Close N1, R9a and N5 for the reviewed scenarios. Keep N4 open for incomplete-pagination handling. R9b has a practical fallback and an executable diagnostic, but still needs a recorded root/reply round trip with the actual app. No M3 finding is repeated.
