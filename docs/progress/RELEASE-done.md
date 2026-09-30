# Release (R1–R4): done

Spec: `docs/progress/RELEASE.md`. All on `master`, not pushed. Offline suite: **623 passed, 5 skipped**
(was 572 + 5 skipped) — run before every commit.

| Commit | What |
|---|---|
| `feat: owner pairing …` | R2 + the in-place `.env` helper from R1 step 4 (pairing needs it) |
| `feat: interactive setup wizard …` | R1 |
| `chore: public-repo hygiene …` | R3 |
| `docs: README …` | R4, LIVE_TEST, this file |

The pairing commit comes first because the wizard waits on pairing (reads `pairing.json`).

## Herdr facts verified on the installed binary (0.8.2)

- **Plugin actions are headless.** A probe plugin (temporary, linked from the scratchpad, then unlinked)
  showed `[Console]::IsInputRedirected = True` inside an action. The action returns immediately
  (`status: running`) and its output only goes to `herdr plugin log list`.
- **Windows gotcha:** in that action, Python's `sys.stdin.isatty()` is **True** (stdin is NUL, a
  character device). The wizard therefore also checks `GetConsoleMode` on stdin; NUL counts as
  non-interactive.
- **An action can open an interactive terminal:** `tab create` (no `--workspace` → the invoker's
  current workspace) + `pane run` gave a prompt that took typed input (`Read-Host` probe). That is how
  the `setup` action works (`open-wizard`).
- The wizard itself was run this way in a real Herdr pane against a **scratch config dir** (not the
  real one): console detection OK, prompts and prefix validation OK, Ctrl+C exits cleanly and keeps
  the answers in `.env`. The run stopped at the token prompt, so it made no Slack calls. For that run
  `--focus` was swapped for `--no-focus` so your focus wasn't taken; the tab was then closed.
- Herdr re-read the edited `herdr-plugin.toml` without re-linking: `herdr plugin action list` showed
  `setup` right away.
- Notification syntax: `herdr notification show <TITLE> [--body TEXT] [--sound none|done|request]`.
  Pairing uses title `herdr-slackbot`, body `Slack pairing code: NNNNNN`, `--sound request`.
- `herdr plugin pane open --entrypoint` / `[[panes]]` exist too, but the herdr-file-viewer plugin notes
  that Herdr can't spawn a relative pane command on Windows, so I didn't use them.
- Slack's prefilled link format `https://api.slack.com/apps?new_app=1&manifest_json=<urlencoded>` is
  from Slack's manifest docs (docs.slack.dev, "Configuring apps with app manifests"). Typical manifests
  encode to ~1.35–1.45 KB (Hangul names included); the limit is 2000 chars because Windows opens
  links through ShellExecute (INTERNET_MAX_URL_LENGTH 2083). Longer → manual paste.

The running bridge in workspace `herdr-slack` and the real config dir were not touched: I only read
which `.env` keys were set (values hidden).

## R1 wizard (`herdr_slackbot/wizard.py`)

- Entry points: `python -m herdr_slackbot wizard`, `scripts\setup.ps1 -Wizard`, plugin action `setup`
  ("Slack bridge: setup wizard") → `herdr-slackbot.ps1 open-wizard` → `PluginOps.open_wizard()`.
  `setup` / `[[build]]` stay non-interactive; their only change is new next-step text (pairing /
  wizard hint), and the owner no longer counts as "missing".
- Steps: `run_setup` (asks slash command / bot name only when `.env` has no value; Enter = default
  shown), prefilled browser link + printed URL + manifest path, token prompts (xapp first, then xoxb)
  that say which page to open, prefix check, `apps.connections.open` / `auth.test` (must return a
  `bot_id`), Slack's error shown before asking again, valid stored tokens kept unless you choose to
  replace them, masked echo (`xoxb-…abcd`), in-place `.env` write, `PluginOps.restart()` (starts
  the bridge if it isn't running), pairing wait (polls `.env` every 2 s for up to 10 min; shows only
  codes created after this start, so a stale `pairing.json` is ignored; hint after 60 s with no code;
  Ctrl+C → "bridge keeps waiting"), then a summary.
- `setup_cmd.update_env_text/update_env_file`: replaces active `KEY=` lines in place (keeps line
  endings, comments, order; rewrites duplicates; appends missing keys); atomic.
- Tests: `tests/test_wizard.py` (fake input, fake WebClient, fake bridge start, URL building/limit,
  NUL stdin, manual fallback, stale code, timeout, Ctrl+C, open_wizard command line). Env helper
  tests are in `tests/test_setup_cmd.py`.
- `tests/test_slack_manifest.py` bans direct Web API calls outside the transport. I added one
  exception: `apps_connections_open` in wizard.py. It runs on the app-level token, so it needs no bot
  scope. (`auth_test` doesn't match the banned prefixes.)

## R2 pairing (`herdr_slackbot/pairing.py`)

- Tokens are still required; with no owner, `run` starts in pairing mode. `Bridge.start()` (DM open,
  notifier, event subscriptions) is deferred until pairing succeeds, so nothing is sent to nobody.
- Code: `secrets.randbelow` → 6 digits, in `STATE_DIR/pairing.json` {code, created, attempts},
  `chmod 0600` best effort (on Windows this only sets read-only; the folder is per-user anyway).
  The code is issued only after Socket Mode connects. It is printed as a banner in the pane (stdout,
  not the logger) and sent as a Herdr notification from a background thread. It is never logged
  (a test checks this). `hmac.compare_digest`.
- Rotation: 5 wrong attempts in total (all users) → new code; expiry 15 min, checked by `tick()`
  once a second in the main loop and again on every attempt. Per-user throttle (review fix 6): a
  Slack user with 5 wrong attempts in 10 min is not checked until the window passes ("try again in N
  min"); throttled attempts don't count. There is no global lock, so a user who hasn't guessed wrong
  (the owner) can always pair. `pair` with no code only shows usage and doesn't count as an attempt.
- While unpaired, the Slack gate (`slack_app.build_app(pairing=…)`) behaves like this:
  - `/cmd pair <code>` → ephemeral result.
  - Other commands (including `status`) → ephemeral "not paired yet … (waiting for pairing, code in
    the herdr-slack pane) — run `/cmd pair <code>`".
  - Buttons → `response_url` notice (or Home view / ephemeral).
  - View submissions → a notice modal.
  - Block suggestions → no options.
  - DM messages → ephemeral.
  - Home tab → a "not paired" Home view.
- Correct code (two-phase since review fix 4): the owner is saved to `.env` in place *before*
  success is reported (a failed save → "saving the owner failed", still pairing), and
  `pairing.json` says `activating`. `Pairing.complete(Bridge.activate_owner)` then opens the DM and
  starts the notifier and subscriptions, retrying after 1/3/10 s; each retry resumes at the failed
  step. Only on success does the owner guard serve the user (before that: "still starting") and
  the welcome DM go out. If it gives up, `pairing.json` says `failed`: the pane, a notification, the
  paired user's Slack notices and the wizard all say to restart the bridge, which then starts
  normally with the saved owner. `pairing.json` is deleted on success, and on exit while unpaired.
- After pairing: non-owners, `pair` included, get the normal owner rejection; the owner's
  `/cmd pair` says "Already paired … clear SLACK_OWNER_USER_ID and restart" (re-pairing is in the
  README). Plugin `status`: `slack: waiting for pairing (code in herdr-slack pane)` when running with
  a code on disk.
- Tests: `tests/test_pairing.py` (state machine, gate for every entry type, live owner switch through
  the real Bridge + bolt dispatch, `run()` in pairing mode with Herdr CLI/Slack stubbed, plugin status).

## R3 hygiene

- The final `git grep` for private names is empty on tracked files. `docs/progress/RELEASE.md` is
  worded neutrally and committed (review fix 7).
- Captured transcripts with private project content from the author's other work were replaced with
  synthetic ones about herdr-slackbot itself (`claude_long_table`, `claude_multiturn_sticky_recap`,
  `claude_recap_multiline`; links and IDs are example.com / `C000…` placeholders). They keep the
  parser-relevant shape (header, multi-line prompt, progress `●` blocks, box table, sticky prompt copy,
  recap, update notice, prompt box, the same `✻` lines), and the parser tests check the new anchors.
- Also generic now: example paths, the cwd-slug test, workspace labels and terminal titles in tests,
  usernames in `test_config.py` (`kim2024`); README/LIVE_TEST examples use `/herdr-kim`.
- `LICENSE`: MIT, `Copyright (c) 2026 <OWNER>`. `pyproject.toml`: `license = {text = "MIT"}`; version
  stays 0.1.0 (the editable install still builds). pyproject has no plugin-action concept, so the
  `setup` action exists only in `herdr-plugin.toml`.
- The README line about `tests/test_slack_manifest.py` was **correct**: the file exists and has been
  tracked since `ed7f610` (M3). Left as is.

## R4 README

Quick start with 3 steps (prerequisites + winget line / `herdr plugin install <OWNER>/herdr-slackbot` /
`herdr plugin action invoke setup --plugin herdr-slackbot`). It covers only the action route; the
`setup.ps1 -Wizard` route is mentioned under 수동 설정. Then 페어링, then 수동 설정 (install/link,
setup.ps1, Slack app, `.env`; the owner is now optional), and the existing sections updated for
pairing (actions list, `pair` command, troubleshooting, security). `<OWNER>` is left as a literal.
LIVE_TEST gained a "2a. 시작과 페어링" checklist.

## Not verified live / for the orchestrator

- **Real Slack was not used at all.** Still to check on first real use:
  - that `?new_app=1&manifest_json=` prefills the form;
  - `auth.test` / `apps.connections.open` answers for real tokens;
  - the ephemeral `pair` replies, Home "not paired" view, `respond` notices and notice modal;
  - the welcome DM, and that Socket Mode keeps working across the live owner switch.

  LIVE_TEST §2a lists these checks.
- **I did not invoke the real `setup` action,** because it would run the wizard against the real
  config and real Slack. I tested the mechanism with the same code path and a scratch config, and
  confirmed the action is registered.
- **`herdr plugin install <OWNER>/herdr-slackbot` from GitHub is untested** (there is no repo yet).
  Assumed: `[[build]]` runs `setup.ps1` headless, and the startup hook doesn't run until the next Herdr
  server start, so the wizard starts the bridge.
- **Git history still contains the replaced content** (the original captured transcripts and
  names in older commits). Pushing `master` as is publishes it; the orchestrator publishes a fresh
  root commit instead. I did not rewrite history.

## Review fixes (`docs/review/release.md`)

| # | Fix |
|---|---|
| 1 | Audited every file under `tests/fixtures`, all `tests/*.py` string literals and `docs/**`. Two more captured transcripts (`claude_multiturn_sticky_recap`, `claude_recap_multiline`) held private project content from the author's other work; they are now synthetic ones about herdr-slackbot. They keep the parser-relevant shape: viewport start, `✻` lines, a sticky `❯` prompt copy inserted mid-diff, wrapped diff lines, a multi-line `※ recap` plus the "Update installed" notice, the prompt box and status lines. Also made generic: a workspace label and a terminal title in `test_blocks.py` / `test_naming.py`. The other fixtures (this project's own test sessions, the Codex screens, `claude_session.jsonl`) contain only herdr-slackbot content. |
| 2 | History: not changed here. The orchestrator publishes a fresh orphan root commit. |
| 3 | `setup_cmd.env_lock`: an OS file lock (`<config dir>/.env.lock`) around every `.env` read-modify-write: `update_env_file` (wizard tokens, paired owner) and all of `run_setup`. The latest file is read inside the lock, so a stale snapshot can't drop the owner. Waits up to 10 s, then fails loudly. |
| 4 | Two-phase pairing (see R2 above): the guard opens only after owner mode really started. Activation retries and resumes; a final failure is visible in the pane, in a notification, in Slack and in the wizard (which also waits through `activating`). |
| 5 | Wizard verification errors go through `config.redact(text, token)`: the exact token first, then anything token-shaped. Stored and pasted tokens are both covered. `redact` now lives in `config.py` (shared with the log redaction). |
| 6 | Removed the permanent 50-attempt lock; added the per-user throttle (5 wrong / 10 min). The code still rotates after 5 wrong attempts from all users together. |
| 7 | `docs/progress/RELEASE.md` is worded neutrally and committed. |

Tests added: two-phase activation (retry timing, failure state, gate texts, a real Bridge resuming
after a failed DM open and a failed manager start), throttling (a guessing user is released after the
window, 40 guessing accounts can't lock out the owner, owner typos), token redaction (stored + pasted,
odd characters), `.env` lock (waiting writer merges the latest file, 12 concurrent writers keep every
key, timeout, `run_setup` holds the lock), wizard waiting through `activating` / reporting `failed`.
Suite: **636 passed, 5 skipped**.

## Recheck fix (`docs/review/release-recheck.md`)

Medium: readiness was inferred from the owner key. Now the bridge writes `STATE_DIR/bridge.ready.json`
{pid, process start time, owner, at} only once owner mode fully started. In owner mode that is after
the DM, notifier, subscriptions and Socket Mode connection are up. In pairing mode it is inside
activation, before `pairing.json` is removed. The marker is cleared on exit. `plugin.verified_ready`
trusts it only while that pid holds the bridge lock and has the same creation time, so a crashed
bridge's leftover doesn't count.

- Plugin `status` now reports one of:
  - `slack: ready (owner U…)`;
  - `paired; owner mode is starting`;
  - `paired, but owner mode failed to start … restart`;
  - `configured, but the bridge is not ready …` (running, no verified marker);
  - `configured; the bridge is not running`.
- Wizard: both the already-paired path and the just-paired path now wait (up to 60 s) for a verified
  marker for the `.env` owner, written after this run restarted the bridge. Otherwise the wizard
  reports failure (exit 1, "not serving yet"); a failed activation is reported as before.
- Tests: `tests/test_readiness.py` covers:
  - marker verification against the real lock (stale, reused pid, foreign writer);
  - every status line;
  - `run()` writing the marker in owner mode, writing it in pairing mode only after activation, never
    writing it after a failed activation, and clearing it on exit.

  `tests/test_wizard.py` adds cases for: already paired + never serving, stale marker from before the
  restart, and a marker for another owner.

Suite: **651 passed, 5 skipped**.
