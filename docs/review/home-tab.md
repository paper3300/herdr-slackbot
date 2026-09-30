# App Home tab review

Reviewed `8efe9030f3456fef7b988eff6a0bb84b714a7737` (`feat: App Home tab`). Locations below refer to that commit. Earlier M2/M3 findings and concurrent fixes are excluded.

**Result: 0 blockers, 1 major finding.** Owner isolation, acknowledgement order, trigger routing, Home block limits and manifest changes pass the checks below.

Validation: **544 passed, 5 skipped** with `python -m pytest -q` in an isolated `git archive 8efe903` extraction. Additional probes used actual Bolt dispatch, real callback threads, fake clocks/timers and fake Herdr/Slack transports. No live Slack calls or mutating Herdr commands were made. Only this review was added to the checkout; source and repository tests were not edited.

## Major finding

### H1 — Slow Home publishes accumulate callbacks that survive shutdown

- **Location:** `herdr_slackbot/bridge.py:231`–`238`, `:242`–`249`, `:1191`–`1200`.
- **Cause:** `HomeRefresher._fire()` clears `_timer` and records `_last` before calling `publish()`. A transition can therefore schedule another callback while a publish is still running. `Bridge.publish_home()` serializes these callbacks with `_home_lock`, but places no bound on the number waiting. `close()` cancels only the currently scheduled timer; callbacks that have already passed `_closed` subsequently publish without checking shutdown again.
- **Realistic failure:** Once the owner has opened Home, several agents keep transitioning while Slack requests time out or the local Herdr snapshot is slow. The production Slack client uses the SDK's 30-second default timeout. With refresh requests approximately every five seconds, callbacks arrive faster than serialized publishes complete, so waiting threads grow throughout the outage. Refresh/open handlers can also wait behind this work. On recovery, queued callbacks drain without the promised five-second publish spacing. Stopping the bridge leaves already queued callbacks eligible to make further Herdr reads and Slack publishes while the process remains alive.
- **Reproduction:** Construct the real bridge with `test_bridge.make_env(..., start=False)`, set its Home owner, and replace only the transport, timer factory and clock. Block the first `publish_view` on a `threading.Event`; request and fire six timers on separate real threads at valid virtual intervals of at least five seconds. All six callbacks enter `publish_home`, with five waiting behind its lock. Call `bridge.stop()`, then release the first publish and join the callback threads. Observed: **6 blocked callback threads; 6 total publishes; 5 publishes started after stop; the five queued calls drained within one millisecond**. All snapshots and API calls were fake.
- **Fix direction:** Keep refresh work marked in flight through completion and coalesce further requests into one pending refresh. Schedule that refresh from the actual publish timing, with at most one callback running or waiting. Check shutdown immediately before snapshot/publish work, and cancel the pending refresh on close. Cover slow publishes, transitions during a publish, recovery spacing and shutdown with an interleaving regression.

## Requested checks

| Area | Status / evidence |
|---|---|
| Owner guard and information disclosure | **Pass.** Global middleware rejects non-owners before `app_home_opened` and all four Home action handlers. Owner identity comes from the event/interaction user. Tests and real Bolt probes observed no bridge handler calls for a stranger; no agent snapshot or view is returned to that user. The Messages tab event is ignored. |
| Ack first | **Pass.** All four action handlers call `ack()` before bridge work. Bolt auto-acks the event in the production default processing mode. For the event and each action, dispatch returned HTTP 200 in under one second while the owner handler remained blocked on a test event. |
| Trigger IDs and modal actions | **Pass.** `home_new`, `home_send` and `home_send_agent` pass the interaction's `TRIG` through to the existing modal flow, which opens a loading view before Herdr reads. Home payloads without message/channel fields work. Per-agent selection works for ordinary lists; a departed target leaves the modal unselected. |
| Block Kit limits | **Pass for the requested limits.** Probed 0, 1, 95, 96, 150 and 500 agents, in one workspace and separate workspaces, including oversized names. Views stayed at or below 100 blocks; button values stayed below 2,000 characters (agent values are capped at 150). Header/section/context text is bounded. Repeated `home_send_agent` IDs occur in different section blocks; Slack requires action ID uniqueness within the containing block, where these IDs are unique. See [Home view limits](https://docs.slack.dev/surfaces/app-home/) and [button requirements](https://docs.slack.dev/reference/block-kit/block-elements/button-element/). |
| Debounce, thread safety and shutdown | **Fail: H1.** Burst coalescing and cancellation of an unfired timer pass. Running and waiting publishes are not bounded or cancelled. |
| Rate limits | **Normal path acceptable; recovery/backoff gaps remain.** Five-second automatic spacing is below the documented Tier 4 allowance of 100+ calls/minute. H1 breaks actual spacing after slow calls; minor backoff/manual-refresh gaps are listed below. See [views.publish rate limits and errors](https://docs.slack.dev/reference/methods/views.publish/). |
| Manifest and scopes | **Pass.** Home is enabled, `app_home_opened` is added to bot events, and Messages/interactivity/Socket Mode remain enabled. No extra OAuth scope is required for either [views.publish](https://docs.slack.dev/reference/methods/views.publish/) or [app_home_opened](https://docs.slack.dev/reference/events/app_home_opened/); the five existing scopes remain sufficient for this addition. README instructions cover updating the existing app's manifest. |

## Minor observations

- **Backoff/freshness:** `publish_home()` catches and logs `SlackTransientError`, discarding `retry_after`. Later transitions may publish during that cooldown; a failed final refresh is not retried until another transition, open or manual refresh. This affects the Home display, without stopping the separate notification retry flow (`bridge.py:1196`).
- **Manual/automatic spacing:** Open/Refresh publishes bypass the debounce schedule. `published()` changes `_last` but does not move an existing timer. A fake-clock probe published manually at `100.5` and automatically at `101.0`. Ordinary owner use alone is unlikely to exhaust the method's allowance (`bridge.py:226`, `:1163`–`1180`).
- **Large-list preselection:** With 101 agents ordered as one agent in workspace A, 99 in B, then another in A, Home grouping displays that last agent near the top. Its Send button opens a modal that excludes it because the existing selector takes the first 100 agents in original order. Reproduced: target visible on Home, absent from modal options, no initial selection (`blocks.py:230`, `:264`, `:384`; `bridge.py:893`).

Slack-side rendering and installation were checked against documentation and payloads, without a live app installation.
