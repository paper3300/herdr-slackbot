# Release review (`c7fcd3a..HEAD`)

Reviewed against `docs/progress/RELEASE.md` and `docs/progress/RELEASE-done.md`. Findings are ranked by severity. This review changed no source files.

## Critical

1. **Private project content remains in a tracked fixture.** `tests/fixtures/transcripts/claude_multiturn_sticky_recap.txt` still contains captured content from the author's other work. The hygiene commit changed a path, but kept the transcript body. **Failure scenario:** publishing the current `HEAD` exposes that content to anyone who clones the public repository, although the name scan finds nothing. Replace the entire fixture body with synthetic content of the same parser-relevant shape, and inspect the other captured transcripts before publishing.

2. **The proposed public branch retains the unsanitized fixture in Git history.** `docs/progress/RELEASE-done.md` acknowledges this; older commits contain the original captured transcripts. **Failure scenario:** pushing `master` publishes all reachable old blobs even after the current files are scrubbed; a reader can retrieve them with `git show`. Publish from a new sanitized root commit or filter the history, and verify all reachable objects before pushing. Cleaning only `HEAD` does not resolve this finding.

## High

3. **Concurrent `.env` updates can silently undo a successful pairing.** `herdr_slackbot/setup_cmd.py:112-125` reads the whole file, then replaces it without a cross-process lock or version check; both `herdr_slackbot/pairing.py:159-165` and `herdr_slackbot/wizard.py:302-305` call this helper. **Failure scenario:** while a bridge is waiting for pairing, the wizard reads an `.env` with an empty owner to save replacement tokens; the Slack pairing handler then writes the new owner; the wizard's delayed replace writes its older snapshot over that owner. The live process reports success, but a restart returns to pairing mode and permits a different account to claim ownership. Serialize writes across processes, or merge against the latest file under a shared lock and verify the owner was not lost.

4. **The owner gate opens before the bridge has activated the owner.** `herdr_slackbot/pairing.py:159-169` sets `pairing.owner` immediately after saving it; `herdr_slackbot/slack_app.py:77-78,89-93,119-125` then admits that user's requests while `on_paired` runs on a background thread. `herdr_slackbot/bridge.py:358-375` opens the DM and starts the notification pipeline only in that later callback. **Failure scenario:** a successful pair is acknowledged and the wizard sees the owner in `.env`, but a transient `open_dm` failure makes activation raise in the background. The owner can continue issuing commands, while the notifier remains unstarted and agent results never reach the DM; no retry or failed-pair state is established. Complete activation before enabling normal dispatch, and make activation failure recoverable or visible to the wizard.

5. **Wizard error text can print a full token.** `herdr_slackbot/wizard.py:109-114` returns a non-Slack exception's first line without redaction; `herdr_slackbot/wizard.py:125-139,343-346` then prints it next to a masked token. A read-only reproduction with `RuntimeError('proxy rejected xapp-1-A-SECRET')` returned that full fabricated token. **Failure scenario:** a proxy, transport, or SDK error includes the supplied token in its exception text; the wizard echoes it into the Herdr terminal output, which can be retained in pane logs. Redact token patterns and the exact submitted token before printing any verification error, including errors for tokens already stored in `.env`.

## Medium

6. **Any Slack user can lock pairing before the owner completes setup.** `herdr_slackbot/pairing.py:170-179` permanently locks the pairing instance after 50 wrong submissions, and `herdr_slackbot/slack_app.py:86-93` lets any Slack user submit them while unpaired. **Failure scenario:** someone in the app's workspace sends 50 guesses; the legitimate user can no longer pair with the correct code until they restart the bridge on the PC. The spec calls for rotation after five wrong attempts, not an unauthenticated permanent lock. Retain a brute-force bound with a time-based cooldown or per-user throttling that cannot indefinitely block the owner.

7. **The untracked release spec will reintroduce private names if staged for publication.** `docs/progress/RELEASE.md` contains the exact names that R3 requires absent. `git grep` on tracked `HEAD` is empty, but `git status --short` shows this spec as an untracked file. **Failure scenario:** a routine `git add -A` before the public push includes the spec and fails the release's own hygiene requirement. Redact or exclude that file, then rerun the named-term scan across the exact commit being published.

### Verified observations

- The tracked `HEAD` passes the three-term `git grep` requested by R3; this does not cover untracked files or Git history.
- The middleware rejects ordinary slash commands, actions, views, suggestions, DM events, and Home events while no owner is set; the findings above concern the transition after pairing and release exposure.
- No full pairing code is included in the pairing result text or the ordinary pairing log messages. The intended pane print and local notification do show the code.
- No full token is deliberately printed on the normal wizard success path; finding 5 concerns exception text.
- `git diff --check c7fcd3a..HEAD` passed. No live Slack or Herdr run was performed for this review.
