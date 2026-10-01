# herdr-slackbot — Implementation Spec

Herdr (0.8.2, Windows) plugin that bridges a Slack bot DM to the local Herdr session.
Python 3.11 + `slack_bolt` Socket Mode. Owner-only, one Slack app per user/PC.

## Scope
Bot DM features:
1. Notifications about agent state (done / blocked).
2. Slash commands: list agents, start a new agent in a chosen workspace with a prompt, send a prompt to a live agent.

## Decisions

### Commands (D1/D2)
- Slash command name comes from `.env` `SLASH_COMMAND`, default `/herdr-<windows username>` (lowercased, sanitized). Referred to as `/herdr` below.
- `/herdr <cmd>` with no args opens a Block Kit modal (dropdowns + multiline input); with args executes directly.
  - `/herdr list` — list agents.
  - `/herdr new` — modal (or args) to start a new agent.
  - `/herdr send [<target> <text>]` — modal or direct.
  - `/herdr status` — bridge status.
  - `/herdr` (bare) / unknown → usage.
- Bot display name default `Herdr (<username>)`, configurable (`BOT_DISPLAY_NAME`).

### New agent (D3, D4, naming)
- New pane = new tab in the chosen Herdr workspace: `herdr tab create --workspace <ws> --label <label> --cwd <cwd> --no-focus`; agent starts in its root pane (`.result.root_pane.pane_id`).
- Modal fields: workspace (dropdown), cwd (prefilled from an existing pane's cwd in that workspace — `workspace list` has no cwd; editable), agent kind (claude | codex only), model, effort, permission mode, optional name, prompt (multiline).
- claude defaults: `--model opus --effort high --permission-mode auto`. Effort options `low|medium|high|xhigh|max`. Permission modes exposed: `manual`, `acceptEdits`, `auto`, `plan` only (never `dontAsk`/`bypassPermissions`). Default `auto`.
- codex: model via `-m <model>`, effort via `-c model_reasoning_effort=<e>`. Model/effort option lists swap per kind (modal `dispatch_action` on kind select → `views.update`).
- Naming: blank → agent name `slack-<N>` (monotonic counter persisted in STATE_DIR, never reused) and tab label `slack-<N> <prompt summary>`. Given → used for both; must match `[a-z][a-z0-9_-]{0,31}` and be unique among live agents.
- Start: `herdr agent start <name> --kind <kind> --pane <root> -- <args...>` then `herdr agent prompt <name> <text>` (don't block Slack handler; ack first).
- No auto-close of tabs (possible `/herdr close` later — not in scope now).

### Send (D5, D6)
- Targets agent panes only (`herdr agent list`); no shell panes, no `pane run`.
- Dropdown label: `<status emoji> · <name or pane_id> · <workspace label> · <status> · <terminal title>` (truncate to Slack's 75-char option text limit).
- Accept only `idle` / `done`. `working` → DM "busy". `blocked` (or Herdr `agent_blocked` error) → DM "waiting for an answer: use the buttons in its thread, or answer on PC" (`/send` command and modal). `unknown` → reject. A **thread reply** while the thread's dialog is open is not rejected: it is the dialog's free-text answer (see "Blocked dialogs" below).

### Threads (D7, D8, D9, D11)
- One DM thread per agent, bound to `agent_session` id (not pane).
- Slack-send confirmation message is the thread root; post "⏳ started (working)" reply; on completion (idle or done) post result as thread reply. Completion detected via event subscription, no timeout.
- Notify for ALL agents, including PC-originated ones; thread created on first notification. `[🔕 mute]` button per agent thread (persist muted sessions).
- PC-originated agents: notify on `working → done` only (unseen tab); skip `working → idle`. No min-duration filter. `blocked` → notify for all agents with the dialog and answer buttons (see "Blocked dialogs" below).
- Slack-originated tasks always get a completion reply regardless of mute.
- Thread reply = `/herdr send <thread's agent> <text>` with D6 rules (except while a dialog is open: then it answers the dialog's free-text option, or gets "This question needs one of the buttons above."). If agent exited or pane now hosts a different agent_session → reject with notice.
- Plain DM messages outside threads → ignore + show usage.
- Persist thread map (agent_session → channel, thread_ts, origin, muted) in STATE_DIR (JSON, atomic write).

### Result body (D10)
- Read `herdr agent read <target> --source recent-unwrapped --lines 200` (fresh sessions have blank padding at bottom → drop blanks).
- Claude parse: strip everything from the prompt box (`───` line / `❯` prompt line at the bottom) downward; the response sits between the echoed `❯ <prompt>` line and `✻ ... done`-style line; take the last `●` block through the `✻ …` line; if a `※ recap` line exists, put it on top as summary.
- Fallback (codex, parse failure): raw last N non-blank lines (e.g. 40).
- Over 3000 chars → truncate + `[View full]` button that uploads the full text as `.md` file into the thread (`files_upload_v2`).
- Header: `✅ <name> · <workspace label> · <duration>` plus small context line with cwd / terminal title. Blocked: `⚠️ <name> · <workspace label> is waiting for your answer` + the dialog.
- Parser must be a pure function with unit tests (use fixture transcripts; include long / multi-tool outputs).

### Runtime / hosting (D12, D14, D15)
- Bridge runs in dedicated Herdr workspace `herdr-slack`; plugin startup hook creates it (`--no-focus`) if absent and runs the bridge in its pane.
- Single-instance lock file in STATE_DIR.
- Herdr plugin actions (and keys if supported) for restart / status. `/herdr status` Slack command.
- Tokens + settings in `.env` under `HERDR_PLUGIN_CONFIG_DIR` (`herdr plugin config-dir <id>`). Provide `.env.example`.
- Start local-only via `herdr plugin link D:\Git\herdr-slackbot`, but structure for GitHub distribution (`herdr plugin install`): manifest, Windows build/setup script, `.env.example`, setup guide (README).
- Check installed plugin `herdr-file-viewer` (`herdr plugin config-dir herdr-file-viewer`, its install dir under `%APPDATA%\herdr\plugins`) for the real manifest format / hooks / actions API. Run `herdr plugin` / `herdr plugin action` help for syntax.

### Security / Slack app (D13)
- Per-user Slack app. Setup generates a Slack app manifest (YAML/JSON) with the user's slash command name, bot display name, Socket Mode on, scopes needed (chat:write, commands, im:history, im:write, files:write, users:read as needed), `message.im` event, interactivity.
- User creates the app from the manifest, puts `SLACK_BOT_TOKEN` (xoxb) and `SLACK_APP_TOKEN` (xapp) in `.env`; `SLACK_OWNER_USER_ID` comes from pairing (below) or by hand.
- Bridge accepts ONLY the owner's Slack user ID (commands, actions, views, messages). Everything else → ignore / ephemeral reject.
- Keep Slack transport isolated behind an interface so a shared-app relay hub can be added later.

## Herdr integration facts (from spikes, Herdr 0.8.2)
- Socket: Python `open(r"\\.\pipe\" + os.environ["HERDR_SOCKET_PATH"], "r+b", buffering=0)` works; NDJSON request/response. Verify request shape (method names) against the CLI / docs; CLI (`herdr ... ` JSON output) is an acceptable fallback for commands.
- `events.subscribe` streams NDJSON live. `pane.agent_status_changed` subscription REQUIRES `pane_id` → subscribe per pane; watch `pane.agent_detected` / `pane.created` / `pane.closed` to add/remove panes. Use a separate connection per subscription (sync pipe handle can't write while a read is pending).
- On subscribe the server REPLAYS retained history → reconcile with an `agent list` snapshot and ignore stale events (use `state_change_seq` / revision).
- Event names are inconsistent: `pane.agent_status_changed` vs `pane_agent_detected` / `pane_created` — normalize.
- Idle on an unseen tab arrives as `done`. Release → `unknown` + `released: true`.
- `tab create` without `--cwd` uses the workspace's cwd.
- Timings: tab create ~0.5s, agent start ~4.4s (returns idle, `agent_session` id), prompt --wait returns `done` + new terminal_title.
- `tab close` kills the agent.
- `agent list` item fields: agent, agent_session{value}, agent_status, cwd, focused, pane_id, revision, state_change_seq, tab_id, terminal_id, terminal_title, terminal_title_stripped, workspace_id (name when set).

## Engineering
- Layout suggestion: `herdr_slackbot/` package — `config.py`, `herdr_client.py` (pipe + CLI), `events.py` (subscription manager), `state.py`, `parser.py`, `slack_app.py` (bolt handlers, owner guard), `blocks.py` (Block Kit builders), `bridge.py` (orchestration), `__main__.py`; `tests/`; `scripts/` (setup/build, manifest generator); plugin manifest; README.
- `pytest` unit tests for parser, naming/counter, state, block builders, status/notification decision logic (pure functions). Herdr/Slack I/O mocked.
- Logging to STATE_DIR log file + stdout. No secrets in logs.

## Decisions added during implementation (2026-09-30, confirmed by user)
- **Result source (overrides D10 primary source):** for claude agents, read the final assistant message from `~/.claude/projects/<cwd-slug>/<agent_session.value>.jsonl` first; the D10 screen parser is the fallback. codex stays screen-based (raw tail / parser).
- **Mute vs blocked (clarifies D8/D9):** mute suppresses `blocked` alerts too for PC-originated work. A pending Slack-originated task always notifies (blocked and completion) regardless of mute.
- **blocked → done (extends D9):** for PC-originated agents, `blocked → done` counts as a completion notification, same as `working → done`.
- **Codex options (orchestrator default):** effort `low|medium|high|xhigh|max`, default `high`. Model dropdown = models with `visibility: "list"` from `~/.codex/models_cache.json` (fallback to a static list), default = `model` in `~/.codex/config.toml` (fallback first listed). No permission-mode field for codex (uses the user's codex config).

## Blocked dialogs (docs/progress/BLOCKED-ANSWER.md, 2026-09-30)
Replaces "confirm on PC": a blocked agent's dialog is answered from Slack.
- Covered: Claude permission prompts, AskUserQuestion (single / multi-select / several questions /
  free text), plan approval, Claude folder trust at startup (`agent start` → `agent_not_ready`), Codex
  command approval, Codex folder trust at startup (reported `idle`: detected on screen), and a keypad
  (`1`–`4`, `↑`, `↓`, Enter, Esc + the last 15 screen lines) when the screen cannot be parsed.
  "Always allow" / "don't ask again" options are shown like any other option.
- Parser: `dialog.py` (pure) → kind, title, body, question, options (number, label, description,
  checked, free_text, chat, cursor), tabs, plan file, fingerprint (cursor moves and footer hints
  excluded; checkbox states included). Keys: the option's digit; unnumbered menus `up`/`down` + Enter.
- Message: one button per option (`"<n>. <label>"`, ≤75 chars; the full label stays in the text),
  multi-select ☐/☑ + [Next →] (cursor to the Submit row + Enter), free-text options open a modal, always
  [Esc] and [Show screen].
  Plan text comes from the plan file named in the dialog footer (else the screen), with [View full].
  Button values carry only `{"t": token, "o": option index | key}`.
- Pending record in the thread entry (`dialog`): token, fingerprint, kind, options, message ts, agent
  session / terminal / pane. Before sending keys the live agent must be the same session, still
  blocked (or showing the idle startup dialog) with the same fingerprint; otherwise nothing is sent and
  the message is updated. The fingerprint covers the title and body too (a plan above the dialog's rule),
  and a plan approval also compares a hash of the plan file. Answers per agent are serialized with its
  transition handling (answer lock keyed by terminal id, taken before the admission lock). The message
  and record are built once per post op and persisted (`dialog_pending`) before the first attempt.
  A click whose token is not open leaves a live (re-rendered) or closed dialog message alone
  ("That button was out of date"); closed message ts are kept in `closed_dialogs`.
- After the keys the bridge polls (0.3 s, up to 5 s): a new dialog that reads the same twice → the
  same message is edited (next question, toggled box, review, re-plan); agent no longer blocked →
  `✅ <choice> — answered from Slack`; no change → buttons kept + "⚠️ Could not confirm the answer".
- Transitions reconcile the record with the live agent: no longer blocked or a different dialog →
  `✅ answered on PC`; session ended → `⏹ ended`; buttons removed, record dropped.
- New agent blocked at startup: its thread is opened with the dialog and the prompt is kept
  (`deferred_prompt`); it is sent (once) when the dialog is answered and the agent is idle.
- Free text (verified live): single-select / plan: digit, `pane.send_text` (newlines as they are: they break
  the line, they do not submit; CRLF → LF), Enter. Multi-select: a digit only toggles, so the cursor is
  moved onto the Type something row with up/down, then the text is typed (it checks the box) and no
  Enter is pressed (Enter would uncheck it). A newline typed there breaks the line too (verified live).
  After typing, the row shows the text instead of "Type something"; it is recognized only on evidence
  (the record it was answered from had its free-text row at that position; otherwise it stays a plain
  toggle) and its label is the typed text. More text is appended (no clearing key); the modal and the
  thread-reply notice say so and show the current text. Text is passed positionally to the CLI
  (no `--`). Restart / idle-recheck reconciles re-render a changed dialog that is still waiting (no
  transition would post it); a transition closes it (its `blocked` transition posts the new one).
- Recovery without a transition: at start the bridge reconciles every open dialog / deferred prompt;
  idle dialogs (Codex trust) are rechecked when the notifier has been idle for 15 s. A timeout re-renders
  the buttons under a new token, so a click queued behind the answer cannot press the key again.

## Release additions (docs/progress/RELEASE.md)
- **Owner pairing (R2):** tokens are required, the owner is not. Without `SLACK_OWNER_USER_ID` the bridge
  runs in pairing mode (`pairing.py`): random 6-digit code in `STATE_DIR/pairing.json`, printed in the
  bridge pane + `herdr notification show`; only `/<cmd> pair <code>` is accepted (any user), everything
  else gets a "not paired yet" notice. A correct code writes the owner into `.env` in place (every
  `.env` writer holds the `.env.lock` file lock next to it), then owner mode starts (DM, notifier,
  subscriptions, retried); only then does the owner guard serve that user ("still starting" before,
  "could not start → restart" if it gives up; `pairing.json` state activating/failed for the wizard).
  5 wrong attempts (all users) or 15 min rotate the code; each Slack user may make 5 wrong attempts per
  10 min, then only that user waits (no global lock). Re-pair = clear the key + restart.
- **Setup wizard (R1):** `python -m herdr_slackbot wizard` (`wizard.py`, interactive console only; NUL
  stdin counts as non-interactive). Plugin actions run headless (stdin redirected, checked on Herdr
  0.8.2), so the `setup` action (`open-wizard`) opens a focused tab in the current workspace and types
  the wizard command into its shell. The wizard opens `https://api.slack.com/apps?new_app=1&manifest_json=`
  with the URL-encoded manifest (manual paste above 2000 chars), verifies tokens with `auth.test` /
  `apps.connections.open`, writes them into `.env` in place, restarts the bridge and waits for pairing.
