# Release recheck (`8c07187..19a4daa`)

Checked the six fixes claimed by `19a4daa` against `docs/review/release.md`, reviewed the full new diff, and audited every path returned by `git ls-files` at `HEAD`. No source files were changed.

## Prior findings

| Finding | Status | Evidence and failure scenario |
| --- | --- | --- |
| 1. Private content in the sticky-recap fixture | **Fixed** | `tests/fixtures/transcripts/claude_multiturn_sticky_recap.txt` is now a synthetic transcript with a synthetic diff; the captured content is absent. The second captured recap (`claude_recap_multiline.txt`) was also replaced with a synthetic one. Publishing the current tree no longer publishes the captured content. |
| 2. Unsanitized Git history | **Not fixed in this branch** | `docs/progress/RELEASE-done.md:148-150` still says old commits contain the original fixtures, and older commits still contain the original captured transcripts. **Failure scenario:** pushing this branch with its existing ancestry publishes those objects. The stated plan to publish `HEAD` as a fresh public root commit resolves this only when the new public branch actually excludes the old ancestry and all other refs containing it. Verify the exact objects to be pushed before publishing. |
| 3. Concurrent `.env` writers lose the owner | **Fixed** | `herdr_slackbot/setup_cmd.py:123-160,190-201` holds the same cross-process `.env.lock` across each read/replace, including the setup merge. Pairing and wizard still use `update_env_file`, so the delayed wizard write reads the owner's latest value after acquiring the lock. The concurrency regressions in `tests/test_setup_cmd.py:201-267` pass. |
| 4. Owner admitted before activation | **Fixed for the original race** | `herdr_slackbot/pairing.py:188-198,208-231` saves a pending owner, runs activation with retries, and sets `owner` only after activation returns. `herdr_slackbot/slack_app.py:120-135` rejects normal commands during activation or after failure; `herdr_slackbot/bridge.py:369-393` resumes startup after a failed step. The wizard waits on the activation marker (`herdr_slackbot/wizard.py:373-387`). A failed DM open no longer opens the owner gate. See the new readiness finding below. |
| 5. Wizard prints tokens from exceptions | **Fixed** | `herdr_slackbot/wizard.py:110-120,130-144` passes the supplied token to `config.redact`, which removes the exact token before applying a generic token pattern (`herdr_slackbot/config.py:40-50`). This covers pasted and stored tokens, including unusual characters. The fabricated proxy-error regression in `tests/test_wizard.py:384-412` passes. |
| 6. Global pairing lockout | **Fixed** | `herdr_slackbot/pairing.py:182-205,241-251` replaces the 50-attempt permanent lock with a sliding, per-user wrong-attempt limit; global wrong attempts still rotate the code after five. One user's flood no longer prevents a different, unthrottled Slack user from pairing. The throttle tests in `tests/test_pairing.py:211-255` pass. |
| 7. Untracked spec with private names | **Fixed** | `docs/progress/RELEASE.md` now uses generic descriptions and is tracked. `git status --short` was empty before this review file was added; the requested name scan over tracked `HEAD` returned no matches. |

## New finding

### Medium: readiness checks report success while owner mode has failed

`herdr_slackbot/plugin.py:613-625` reports `slack: configured` as soon as `.env` has an owner, without consulting the new activation marker or checking that the notifier/subscriptions started. `herdr_slackbot/wizard.py:358-362` likewise returns success immediately when rerun with an existing owner, before checking the bridge's actual startup. **Failure scenario:** activation fails all retries after saving the owner (`herdr_slackbot/pairing.py:227-231`); the user follows the restart advice, but the bridge fails again during startup. The status action still says `configured`, and a rerun of the wizard prints `already paired` and completes successfully although the bridge cannot serve commands or send notifications. Report activation failure and actual runtime readiness separately from the presence of the owner key; make the wizard wait for a verified running bridge in its already-paired path.

## Public-tree audit and validation

- Audited all tracked paths from `git ls-files`, including documentation, tests, source, manifests, and every transcript. Searched tracked `HEAD` for the private names, identifiers and hosts from the earlier findings, URLs, drive and home paths, and people references. Inspected the matching fixtures and documentation. I found no private project content, identifiers, paths, URLs, or people in the current tracked tree. Remaining matching paths/hosts are this repository, public services, or clearly synthetic examples.
- `git diff --check 8c07187..HEAD` passed. `python -m pytest -q -p no:cacheprovider` with bytecode writes disabled: **636 passed, 5 skipped**. No live Slack or Herdr operations were run.
- This tree audit does not sanitize the current branch's Git history; finding 2 remains until the fresh-root publication is verified.
