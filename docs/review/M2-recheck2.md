# M2 second re-review

Reviewed `b386d6e10da478cfcaa9b6cb77ba2ccdf94c2ed9` (`M2 recheck fixes`) against `docs/review/M2-recheck.md` items **R2, R5, R9, N1, N2, N3** and the implementation decisions in `docs/SPEC.md`. M3 findings and concurrent fixes are excluded. All code references below refer to this commit.

**Requested items: 4 fixed, 2 partially fixed.**

**Open findings: 0 blocker, 5 major, 0 minor.** Four findings have offline reproductions; R9b is a documented integration concern whose actual Slack behavior remains unverified.

Validation: **452 passed, 5 skipped** with `python -m pytest -q` in an isolated `git archive b386d6e` extraction. Additional probes exercised the real Bridge/transport, the real Slack SDK **3.44.0** with its HTTP operation stubbed, temporary persisted state, replacement-agent snapshots, paginated/metadata-free API responses, and injected state-write/process interruptions. No live Slack messages or Herdr mutations were sent. Source and repository tests were not edited.

## Status per requested item

| Item | Status | Verification / remaining issue |
|---|---|---|
| R2: admission/notifier create two roots | **Fixed** | Admission, transitions and resume now share `_admission_lock`, including root creation and migration. The barrier regression holds a send worker in its root post while a completion arrives: the notifier waits, one root is bound, and the result lands in that thread. The global lock can delay unrelated sends during slow I/O, an acknowledged tradeoff rather than another finding. |
| R5: resume clears a newer task from an old snapshot | **Fixed** | The admission lock spans resume's lookup and decision; the final task-ID check rejects replacement. The concurrent resume/admission regression posts A once and leaves working B pending. Different-task and unstarted-reservation checks also pass. |
| R9: accepted post is duplicated on retry | **Partially fixed** | Explicit uncertain outcomes, persisted intents and marker lookup pass the supplied accepted-response-loss tests. Failed lookups defer reposting, and ordinary definite failures still retry. However, SDK retries bypass reconciliation (R9a), marker retention is not established for the generated manifest (R9b), and the new lookup/persistence paths have additional defects (N4/N5). |
| N1: startup wait adopts a replacement | **Partially fixed** | Explicit session/terminal changes are rejected, including the supplied session-less Codex case; ordinary unknown-to-idle startup passes. A live snapshot with an absent session can still erase the known identity before admission and allow a replacement to receive the prompt; see N1 below. |
| N2: stale provisional entry reused after terminal replacement | **Fixed** | Admission and transitions now find provisional entries by positive terminal equality. Direct send and `/new` tests leave the old terminal's entry untouched and bind a new thread. A matching terminal elsewhere is found despite a stale same-pane entry. |
| N3: resume retry loses a migrated task | **Fixed** | Production startup queues `_Resume(key, task_id)`; retries use `find_task()` after migration. The original pre-acceptance-failure regression passes. An additional accepted-response-loss probe migrated to `REAL`, reconciled one result in the original thread, cleared the task, and left no provisional entry. This confirms migration recovery independently of R9's real API limitations. |

## Remaining findings

### N1. An absent session in a startup observation still discards the pinned identity

- **Severity:** major
- **Location:** `herdr_slackbot/bridge.py:684`, `herdr_slackbot/bridge.py:712`, `herdr_slackbot/bridge.py:736`, `herdr_slackbot/bridge.py:707`
- **Problem:** `_same_agent()` rejects different nonempty sessions, but accepts a missing session even when `expected` already contains one. `_observe()` then merges `agent_session=None` over the known session. The eventual `Target` is constructed from this merged `info`, not the pinned `expected`. Admission consequently falls back to terminal-only identity.
- **Concrete failure scenario:** `agent.start` returns known session A with status unknown. Its next live startup snapshot is idle with the same terminal ID and no session; the following lookup reports replacement B in that terminal. `/new` submitted the original prompt to B, created a thread keyed to **B**, and stored `prompt intended for A` as B's pending task. No replacement notice appeared. This is a same-terminal replacement; the supplied tests replace both session and terminal together.
- **Suggested fix:** Never downgrade a known session/terminal to missing data. Carry the pinned identity into admission/delivery, and treat a missing expected identity as unconfirmed until a bounded recheck proves continuity or rejects the target. Add the known-session → absent-session → replacement-session case.

### R9a. The Slack SDK can duplicate a post before the transport sees an uncertain outcome

- **Severity:** major
- **Location:** `herdr_slackbot/slack_transport.py:131`, `herdr_slackbot/slack_transport.py:151`; client construction in `herdr_slackbot/__main__.py:167`
- **Problem:** `_call()` classifies an error only after `WebClient.chat_postMessage()` returns or raises. The application constructs the client with default retry handlers. Slack documents one automatic connection-error retry; the installed SDK also retries `RemoteDisconnected` and connection resets. An accepted request can therefore be repeated inside the SDK, before `_post()` gets a chance to reconcile. An operation marker is data, not a server-enforced idempotency key. [Slack SDK retry behavior](https://docs.slack.dev/tools/python-slack-sdk/web/#retryhandler).
- **Reproduction:** Used the real SDK and `WebClientTransport`, replacing only `_perform_urllib_http_request_internal`. The first request was recorded as accepted and raised `RemoteDisconnected`; the second succeeded. One `post_message(op='result:task-A')` issued **two HTTP post attempts with the same marker** and returned the second timestamp successfully. No `SlackUncertainError` reached the Bridge. The existing fake-client tests raise above the SDK retry layer.
- **Suggested fix:** Disable automatic retries for ambiguous mutating requests, or supply a method-aware retry policy that only repeats definitively unsent requests. Let the Bridge reconcile unknown outcomes. Include the real SDK retry layer in regression coverage. The new at-most-once claim for ordinary posts also needs this protection.

### R9b. Marker retention is doubtful with the generated Slack manifest

- **Severity:** major — integration concern, not a live-confirmed Slack rejection
- **Location:** `herdr_slackbot/slack_transport.py:149`, `herdr_slackbot/slack_transport.py:155`, `herdr_slackbot/slack_manifest.py:51`
- **Embedding:** The marker exists only as `metadata={event_type: 'herdr_slackbot_post', event_payload: {op: ...}}`. It is absent from message text and blocks. Lookup requires both metadata fields; missing metadata is treated as no accepted message.
- **Scope/API check:** Slack lists bot-token `im:history` as compatible with both DM history and replies, and both methods expose `include_all_metadata`. The metadata guide documents retrieval through history with that option. These sources support the retrieval mechanism; they do **not** establish that this app's custom metadata survives posting. `metadata.message:read` is documented for metadata events, which this implementation does not subscribe to, so I am not asserting that adding that scope alone fixes the Web API path. [DM history scope](https://docs.slack.dev/reference/scopes/im.history/), [history](https://docs.slack.dev/reference/methods/conversations.history/), [replies](https://docs.slack.dev/reference/methods/conversations.replies/), [metadata event scope](https://docs.slack.dev/reference/scopes/metadata.message.read/).
- **Documented gap:** Slack's current metadata guide says schemas must be registered in the app manifest before sending metadata, and invalid metadata is ignored with a warning. This manifest declares no custom metadata schema. The guide and manifest reference also describe registration using different structures, so the accepted format should be verified rather than guessed. [Metadata configuration and retrieval](https://docs.slack.dev/messaging/message-metadata/), [manifest metadata events](https://docs.slack.dev/reference/app-manifest/#metadata-events).
- **Failure consequence:** With a real Bridge/transport and a simulated successful history response containing the accepted bot message but no metadata, the retry posted a **second result**, returned success and cleared the task. This demonstrates the consequence of marker loss; it does not prove Slack stripped a live message. The supplied fakes always retain and return `op` and cannot establish the platform contract.
- **Suggested fix:** Validate the custom event registration and explicitly check a bot root and threaded reply round trip with the generated manifest and actual granted scopes. Inspect metadata-related warnings. If reliable marker visibility cannot be established, retain an uncertain outcome instead of treating absence as permission to post again, or use a verified marker representation. R9 should remain open until this contract is demonstrated.

## Additional findings in the new reconciliation implementation

### N4. A partial history page is treated as proof that no accepted message exists

- **Severity:** major
- **Location:** `herdr_slackbot/slack_transport.py:153`, `herdr_slackbot/slack_transport.py:166`; `herdr_slackbot/bridge.py:277`
- **Problem:** `find_message()` examines one response and ignores `has_more` and `response_metadata.next_cursor`. A successful first-page miss returns `None`, which `_post()` immediately turns into another post. Both APIs are paginated and may return fewer messages than the requested limit even when more remain. [History pagination](https://docs.slack.dev/reference/methods/conversations.history/), [replies pagination](https://docs.slack.dev/reference/methods/conversations.replies/).
- **Concrete failure scenario:** After an accepted result loses its response, the first lookup page contains other messages plus `has_more=true` and a next cursor; the accepted marker is available on page two. The real Bridge/transport made **one lookup, posted two results, returned success and cleared pending state**. The page-two stub was never called. Root lookup has the same flaw. This is independent of eventual consistency or metadata permissions: the accepted message is already readable.
- **Suggested fix:** Follow pagination through the relevant time range before reporting not-found. If the scan is incomplete or exceeds a limit, keep the outcome uncertain. Test short first pages with cursors as well as full pages, for both roots and replies. A complete empty scan still needs the separately acknowledged eventual-consistency policy; one-second backoff does not prove nonacceptance.

### N5. Intent deletion precedes the durable thread/result commit

- **Severity:** major
- **Location:** `herdr_slackbot/bridge.py:280`, `herdr_slackbot/bridge.py:291`, `herdr_slackbot/bridge.py:438`, `herdr_slackbot/bridge.py:1147`
- **Problem:** `_post()` removes the persisted intent immediately after a successful post or lookup. Its caller commits the root binding, result sequence or pending-task completion afterward. An interruption between these writes leaves an accepted message with neither a completed local operation nor an intent that would trigger reconciliation. Keeping `root_op` or recomputing the same result op is insufficient: `_post()` only looks up an operation when an intent remains.
- **Reproductions:**
  - Inject one state-write failure when `_ensure_thread()` commits `thread_ts`, after posting and intent deletion. Retry root creation: **two roots**, only the second bound, and **zero reconciliation reads**.
  - Interrupt a completed result at the `last_result_seq` write, reload a fresh `StateStore` from disk and resume the same task in a new Bridge. Persisted state had `post_intents={}` and a pending task. Recovery posted a **second result**, again without looking up the first.
- **Suggested fix:** Persist the accepted timestamp and operation status, then atomically commit the binding/completion together with intent removal. Recovery should distinguish accepted-but-not-locally-committed work and finish that commit without reposting. Add interruption tests at each persistence boundary, including after a successful reconciliation lookup. The existing restart test stops while the uncertain intent is still present and misses this gap.

## Conclusion

R2, R5, N2 and N3 can be closed for the reviewed scenarios. N1 still permits a wrong-agent prompt after identity information disappears. R9 improves explicit error handling but does not yet provide reliable deduplication: SDK retries, incomplete scans and non-atomic completion remain reproducible, and the metadata round trip needs validation with the actual app configuration. No M3 finding is repeated here.
