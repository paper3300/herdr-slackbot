# Release: public GitHub + easy setup

Goal: anyone with Herdr on Windows can run

```powershell
herdr plugin install <owner>/herdr-slackbot
```

and then finish Slack setup with **one interactive wizard**, without hand-editing `.env` or
hunting for their member ID. Repo will be PUBLIC on the user's personal GitHub.

## R1. Interactive setup wizard

New entry point `python -m herdr_slackbot wizard` (+ `scripts\setup.ps1 -Wizard`, and a plugin
action `setup` titled "Slack bridge: setup wizard"). The existing non-interactive `setup`
(also the `[[build]]` step) must stay non-interactive and unchanged in behaviour.

Steps:
1. Run the existing `run_setup` (venv already done by setup.ps1) → `.env` skeleton + manifest.
   Ask (with defaults shown, Enter = keep) for slash command and bot name only when `.env` has
   no value yet.
2. Open the browser to Slack's prefilled app-creation link:
   `https://api.slack.com/apps?new_app=1&manifest_json=<urlencoded manifest JSON>`.
   Also print the URL and the manifest file path (fallback: paste manifest manually). If the
   URL gets too long for the browser, fall back to the manual path — verify the length.
3. Tell the user exactly which pages to visit, then prompt for `xapp-` (App-Level Token,
   `connections:write`) and `xoxb-` tokens. Hidden input is not required, but never echo the
   token back in full. Validate prefix; call `auth.test` with the bot token (slack_sdk
   WebClient) and `apps.connections.open` with the app token to verify both; on failure show
   the Slack error and re-prompt. Skip any token already present and valid in `.env`
   (offer to replace).
4. Write the values into `.env` **in place** (replace the `KEY=` line, keep comments/other
   values; atomic write). Add a helper for that next to `merge_env`.
5. Start (or restart if running) the bridge via the existing plugin lifecycle code.
6. If `SLACK_OWNER_USER_ID` is empty → explain pairing (R2) and wait (poll `.env`, up to
   e.g. 10 min, Ctrl+C ok) printing the pairing code; report success when paired.
7. Final summary: slash command, where to DM the bot, how to restart/status.

Non-TTY stdin (e.g. run by Herdr build) → wizard refuses with a clear message.

## R2. Owner pairing

Currently missing `SLACK_OWNER_USER_ID` stops the bridge ("missing settings"). Change:
- Tokens are still required; owner ID becomes optional at startup.
- With no owner, the bridge runs in **pairing mode**: generate a random 6-digit code
  (`secrets`), store it in `STATE_DIR/pairing.json` (code + created time, 0600-ish, not in
  logs beyond the pane print), print it prominently in the bridge pane, and send a Herdr
  notification (`herdr notification`, check the CLI) saying "Slack pairing code: NNNNNN".
- In pairing mode the only accepted Slack input is `/<cmd> pair <code>` (any user).
  Everything else (other commands, buttons, modals, DM messages, Home tab) is rejected with
  an ephemeral "not paired yet — run `/<cmd> pair <code>` with the code shown on your PC".
- Correct code → write `SLACK_OWNER_USER_ID=<user_id>` into the config `.env` (in-place
  helper from R1), switch the running bridge to normal owner-only mode without a restart,
  delete pairing.json, reply "paired ✅", DM the new owner a welcome/usage message.
- Wrong code → ephemeral error; after 5 wrong attempts total, rotate the code (print +
  notify again). Codes expire after 15 min → rotate. Constant-time compare.
- Once an owner is set, `pair` is rejected like any other non-owner request; re-pairing =
  clear the key in `.env` and restart (document it).
- `/herdr status` / plugin `status` action show "waiting for pairing (code in herdr-slack pane)".

## R3. Public-repo hygiene

- Captured transcripts under `tests/fixtures/transcripts/` that contain private project content
  from the author's other work: replace them with synthetic ones (neutral example.com links and
  IDs); keep the fixture's structure so tests still pass.
- Use a generic example name (`kim`) in README.md / docs/LIVE_TEST.md examples. Tests may keep
  synthetic usernames but prefer generic ones.
- A final `git grep` over tracked files for private names must be empty (except git history).
- Add `LICENSE` (MIT, copyright holder: leave `<OWNER>` placeholder; orchestrator fills it).
- README references `tests/test_slack_manifest.py` which doesn't exist — fix (add the test or
  fix the text).
- `pyproject.toml`/`herdr-plugin.toml`: add `setup` action; keep version 0.1.0.

## R4. README rewrite (Korean, same tone)

Top section = quick start in ≤ 3 commands:
1. Prereqs (Windows, Herdr ≥ 0.8.2, Python ≥ 3.11 + winget line).
2. `herdr plugin install <OWNER>/herdr-slackbot`
3. `herdr plugin action invoke setup --plugin herdr-slackbot` — if plugin actions can't be
   interactive (check! actions probably run headless with output going to the plugin log),
   then the wizard command must be run in a terminal: give the exact PowerShell one-liner that
   locates the plugin root (`herdr plugin list --json`) and runs `scripts\setup.ps1 -Wizard`.
   Verify which one really works and document only that.
Then the pairing step, then the existing detailed sections (manual setup kept as
"수동 설정" fallback). Keep `<OWNER>` as a literal placeholder; orchestrator replaces it.

## Constraints

- Follow existing code style; keep all 527+ offline tests passing; add tests for: env in-place
  update, pairing state machine (code gen, wrong attempts, expiry, rotation, success switches
  owner live, rejection of everything else while unpaired), wizard with fake input/WebClient,
  manifest URL building.
- No token ever written to logs.
- Commit in logical commits on `master` (feat: wizard, feat: pairing, chore: public hygiene,
  docs: README). Do not push.
- Write `docs/progress/RELEASE-done.md` summarizing what was done + anything unverified live.
