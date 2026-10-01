# herdr-slackbot

**English** | [한국어](README.ko.md)

A [Herdr](https://herdr.dev) plugin that **connects the Herdr agents on your PC (Claude Code / Codex) to a Slack bot DM**.

- Get a DM when an agent finishes (✅ done) or is waiting for an answer (⚠️ blocked), with the result text included.
- Answer dialogs such as permission prompts, questions (AskUserQuestion) and plan approvals **straight from Slack buttons**.
- Use a slash command in Slack to list agents, start new ones, and send prompts to running ones.
- Each agent gets its own DM thread; replying in that thread sends your reply to the agent as a prompt.

Every user (one PC) creates **their own Slack app**. The bot only handles requests from **the Slack account it is paired with**.

```
Slack DM ──(Socket Mode)──> herdr-slackbot bridge ──(named pipe)──> Herdr ──> claude / codex agents
          (no public URL)     (runs in a pane of the Herdr workspace herdr-slack)
```

## Screenshots

**Home tab:** Open the bot to see the agents on your PC, grouped by workspace. Each row shows status emoji · name/pane · kind · status · terminal title. The buttons at the top start a new agent or send a prompt, and idle/done agents have their own **[Send]** button on the row.

![Home tab: agents grouped by workspace, with New Agent / Send / Refresh buttons](docs/images/home-tab.png)

**New agent (`/herdr new`, [➕ New Agent]):** Opens a new tab in the chosen workspace and starts an agent there. You can pick the model, effort and permission mode; the defaults are Opus · high · auto. Leave the name blank to get `slack-<N>`.

![New agent modal: Model, Effort, Permission mode, Name and Prompt fields](docs/images/new-agent-modal.png)

**Send (`/herdr send`, [📤 Send]):** Pick a running agent and send it a prompt. Once you pick an agent, its last response is shown in the modal. Results come back in that agent's DM thread.

![Send modal: agent picker and Prompt field](docs/images/send-modal.png)

## Quick start

1. **Requirements:** Windows 10/11, **Herdr 0.8.2 or later**, **Python 3.11 or later**. Install Python first if you don't have it.

   ```powershell
   winget install Python.Python.3.12
   ```

   You also need permission to create and install apps in your Slack workspace. Workspaces managed by an admin may require approval to install apps.

2. **Install the plugin:** During install, Herdr runs `scripts/setup.ps1`, which creates the venv, installs dependencies and writes the config skeleton.

   ```powershell
   herdr plugin install paper3300/herdr-slackbot
   ```

3. **Setup wizard:** A **Slack setup** tab opens in the current workspace and runs the wizard.

   ```powershell
   herdr plugin action invoke setup --plugin herdr-slackbot
   ```

   Picking the *Slack bridge: setup wizard* action from the Herdr command palette does the same. Plugin actions cannot read input themselves (stdin is not attached), so the action opens a new tab and types the wizard command into that tab's shell.

What the wizard does:

1. Creates the `.env` skeleton and the Slack app manifest in the config folder. It asks for the slash command and bot name only when `.env` has no value for them. Press Enter to accept the default shown in brackets.
2. Opens Slack's create-app page in your browser with the manifest prefilled; just pick the workspace and click **Next → Create**. It also prints the link and the manifest file path, so if the browser doesn't open or the form is empty, paste the file contents yourself.
3. Tells you which page to copy from and asks for two tokens:
   - `xapp-...`: **Basic Information → App-Level Tokens → Generate Token and Scopes**, scope `connections:write`
   - `xoxb-...`: *Bot User OAuth Token* after **Install App → Install to Workspace**

   Tokens are verified against Slack directly (`apps.connections.open`, `auth.test`). If one is wrong, the wizard shows Slack's error and asks again. If `.env` already holds valid tokens, it skips this step and only asks whether to replace them. Tokens are never echoed back in full.
4. Writes the tokens into `.env` **in place**, leaving comments and other values untouched.
5. Starts the bridge, or restarts it if it is already running.
6. If you haven't paired yet, shows the pairing code and waits (up to 10 minutes) until pairing completes. See **Pairing** below.
7. Prints a summary: the slash command, where to find the bot DM, and how to restart and check status.

You can stop at any time with Ctrl+C. Whatever you entered so far stays in `.env`, and running the wizard again picks up where you left off.

## Pairing (linking your Slack account)

`SLACK_OWNER_USER_ID` in `.env` decides whose requests the bot accepts. You don't need to look up your member ID: when the value is empty, the bridge starts in **pairing mode**.

1. The bridge generates a 6-digit code. It is shown in large type in the bridge pane of the `herdr-slack` workspace and as a Herdr notification ("Slack pairing code: NNNNNN"). If you are running the wizard, it appears there too.
2. Open the bot from the **Apps** list in Slack's sidebar and send this in the DM:

   ```
   /herdr-kim pair 123456
   ```

3. When "paired ✅" comes back, the bridge writes `SLACK_OWNER_USER_ID` to `.env` and switches to owner-only mode **without a restart**. When it is ready, the bot DMs you a usage guide (👋 Paired!). Commands sent before that get a "still starting" notice. If startup fails (after a few retries), the pane, a Herdr notification and the wizard tell you to run `restart`, which starts normally with the saved account.

- In pairing mode only the `pair` command is accepted. Other commands, buttons, modals, DM messages and the Home tab only get a "not paired yet" notice. Agent notifications also start only after pairing.
- The code is replaced after 15 minutes or after 5 wrong attempts in total (the new code is shown in the pane and a notification). In addition, a Slack user who gets it wrong 5 times within 10 minutes is locked out for 10 minutes — **only that user**. Someone who hasn't guessed wrong (you) is never blocked, so nobody can stop you from pairing by guessing codes.
- Once paired, `pair` is rejected like any other request from a non-owner. To **re-pair with a different account**, clear the value of `SLACK_OWNER_USER_ID=` in `.env` and run the `restart` action.
- While waiting for pairing, the `status` action shows `slack: waiting for pairing (code in herdr-slack pane)`. After pairing, it shows `slack: ready (owner U...)` only when the bridge is actually running in owner-only mode. If `.env` has a member ID but the bridge isn't ready, it shows `not ready`; if startup failed, `owner mode failed to start`. The wizard also waits (up to 60 seconds) for the bridge to report ready before declaring success.

## Manual setup

For when you don't use the wizard: do by hand what the wizard does.

### 1. Install

To install from GitHub, use `herdr plugin install` as above. You can also clone the repo and link it locally.

```powershell
git clone https://github.com/paper3300/herdr-slackbot D:\Git\herdr-slackbot
powershell -NoProfile -ExecutionPolicy Bypass -File D:\Git\herdr-slackbot\scripts\setup.ps1
herdr plugin link D:\Git\herdr-slackbot
herdr plugin list                                # check: herdr-slackbot ... enabled
```

If you installed with `link` and there is no venv, the plugin creates one on first start. The `setup` action (wizard) works in this case too.

### What `setup.ps1` does

1. Creates `.venv` in the plugin folder and installs the dependencies (`slack_bolt`, `slack_sdk`).
2. Picks defaults:
   - Slash command: `/herdr-<windows username>` (lowercase, at most 32 characters)
   - Bot name: `Herdr (<username>)`
3. Writes two files to the plugin config folder (`herdr plugin config-dir herdr-slackbot`, usually `%APPDATA%\herdr\plugins\config\herdr-slackbot`):
   - `.env`: the config skeleton. If the file already exists, it **never overwrites existing values** and only appends missing keys.
   - `slack-app-manifest.json`: the manifest to paste when creating the Slack app.
4. Prints the next steps.

`setup.ps1` does not read input (it also runs as Herdr's install step). With `-Wizard` it runs the wizard instead of the setup step; in that case run it from a terminal.

The slash command and bot name can be changed with options. If `.env` already has the key, even with an empty value, it is left as is and you are told the option was not applied. The manifest is always generated from the settings that will actually apply (defaults for empty values).

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\setup.ps1 -SlashCommand /herdr-kim -DisplayName "Herdr (Kim)"
```

> Slash command names **must be unique across the whole Slack workspace.** If two apps use the same name, the most recently installed app takes the command. That's why each user picks a different name, such as `/herdr-<name>`.

### 2. Create the Slack app (from the manifest)

1. Go to <https://api.slack.com/apps> → **Create New App** → **From a manifest**.
2. Pick your workspace.
3. On the **JSON** tab, replace the existing content with the contents of `slack-app-manifest.json`, then click **Next** → **Create**.
4. **Basic Information** → **App-Level Tokens** → **Generate Token and Scopes**:
   - Enter any name, add the `connections:write` scope and click Generate.
   - The resulting `xapp-...` token is `SLACK_APP_TOKEN`.
5. **Install App** → **Install to Workspace** → Allow.
   - The **Bot User OAuth Token** `xoxb-...` is `SLACK_BOT_TOKEN`.
6. Open the new bot from the **Apps** list in Slack's sidebar to get a DM window. If the messages tab is disabled, turn on **App Home** → *Allow users to send Slash commands and messages from the messages tab* in the app settings.

The manifest contains the settings below. They are derived from the Slack APIs the code actually calls, and `tests/test_slack_manifest.py` checks that the two stay in sync.

| Setting | Value |
|---|---|
| Socket Mode | On (no public URL or open port needed) |
| Interactivity | On (modals, buttons) |
| Events | `message.im` (DM thread replies), `app_home_opened` (Home tab refresh) |
| App Home | Home tab on, messages tab on (input allowed) |
| Slash command | `SLASH_COMMAND` from `.env` |
| Bot scopes | `chat:write` (post, update, ephemeral messages), `commands`, `im:write` (open DMs), `im:history` (receive DM messages), `files:write` ([View full] file upload) |

#### Updating the manifest of an existing app (e.g. to add the Home tab)

When a new version changes the manifest, apply it to the Slack app you already created.

1. Run `powershell -NoProfile -ExecutionPolicy Bypass -File scripts\setup.ps1`. It regenerates `slack-app-manifest.json` in the config folder without touching the values in `.env`.
2. At <https://api.slack.com/apps>, open your app → **App Manifest**. Replace the JSON with the new file contents and click **Save Changes**.
3. If Slack asks you to reinstall (**Install App → Reinstall to Workspace**), do so. If the tokens changed, update `.env` too.
4. Restart the bridge with the `restart` action.

### 3. Fill in `.env`

`.env` lives in the plugin config folder. Find it with:

```powershell
herdr plugin config-dir herdr-slackbot
notepad "$(herdr plugin config-dir herdr-slackbot)\.env"
```

| Key | Required | Description |
|---|---|---|
| `SLACK_BOT_TOKEN` | ✔ | `xoxb-...` (Install App page) |
| `SLACK_APP_TOKEN` | ✔ | `xapp-...` (App-Level Token, `connections:write`) |
| `SLACK_OWNER_USER_ID` | | **Your** Slack member ID (starts with `U`). Leave it empty and pairing fills it in. Only this user can use the bot. |
| `SLASH_COMMAND` | | Default `/herdr-<username>`. **Must match the command in the manifest.** |
| `BOT_DISPLAY_NAME` | | Default `Herdr (<username>)` |
| `STATE_DIR`, `LOG_LEVEL`, `RESULT_MAX_CHARS`, `FALLBACK_LINES`, `READ_LINES`, `BRIDGE_WORKSPACE`, `START_TIMEOUT_MS`, `CODEX_PROMPT_DELAY`, `STALL_WAIT`, `HERDR_BIN`, `HERDR_SOCKET_PATH` | | Optional settings, documented in `.env.example`. |

After filling in the tokens, start the bridge with the `restart` action and do the **Pairing** above. You can also enter your member ID yourself: in Slack, click your profile picture → **Profile** → **⋮** (More) at the top right → **Copy member ID**. It looks like `U0123ABCD`.

If you changed `SLASH_COMMAND`, regenerate the manifest and apply it to the Slack app. Running `setup.ps1` again (with the same `powershell -NoProfile -ExecutionPolicy Bypass -File ...` form as above) regenerates `slack-app-manifest.json`; paste its contents into the app's **App Manifest** and save.

## Start / restart

- **Automatic start:** The plugin's startup hook runs when the Herdr server starts.
  1. Creates the workspace `herdr-slack` if it doesn't exist, without taking focus.
  2. Runs the bridge (`python -m herdr_slackbot run`) in a pane of that workspace.
  3. The bridge holds an instance lock (`STATE_DIR\bridge.lock`), so two bridges never run at once.
  4. Start, stop and restart are serialized through a single `STATE_DIR\lifecycle.lock`. The bridge takes its own lock inside the same lock at startup, so the state is always one of stopped / starting / running. Other start requests during a start (up to 60 seconds) are ignored, and a `stop` at that point cancels the start.
  5. Each start creates a new tab in `herdr-slack` (without taking focus) and types the command only there — never into an existing pane. Once the new bridge confirms it started, it closes the previous bridge tab, but only if all of the following hold: it is on the same Herdr server, the recorded tab/pane/terminal IDs are unchanged, the tab has a single pane, and no agent or program is running in it. If anything doesn't match, the tab is left alone.
- The startup hook only runs when the Herdr server starts, so it does not run right after `herdr plugin link`/`install`. The setup wizard starts the bridge for you; if you didn't use the wizard, start it right away with the `start` action.
- **Plugin actions:**

  ```powershell
  herdr plugin action invoke setup   --plugin herdr-slackbot   # setup wizard (runs in a new tab)
  herdr plugin action invoke start   --plugin herdr-slackbot   # no-op if already running
  herdr plugin action invoke restart --plugin herdr-slackbot   # after editing .env
  herdr plugin action invoke stop    --plugin herdr-slackbot
  herdr plugin action invoke status  --plugin herdr-slackbot   # also shown as a Herdr notification
  ```

  `stop`/`restart` don't type anything into a terminal. They identify the running bridge from the `bridge.runtime.json` it leaves behind (pid and process creation time), then leave a `stop.request` addressed to that process. The bridge checks for the request every second and shuts down cleanly on its own. If it hasn't exited within 15 seconds, the process is killed — but only if the pid and creation time still match. If the bridge can't be verified, nothing is done and you are told so. Action output is available via `herdr plugin log list --plugin herdr-slackbot`.
- **Key bindings:** The plugin manifest can't declare keys. Add them to your Herdr `config.toml` and run `herdr server reload-config`.

  ```toml
  [[keys.command]]
  key = "prefix+shift+s"
  type = "plugin_action"
  command = "herdr-slackbot.restart"
  description = "restart Slack bridge"

  [[keys.command]]
  key = "prefix+s"
  type = "plugin_action"
  command = "herdr-slackbot.status"
  description = "Slack bridge status"
  ```

- **Running manually:** For development, run it directly:

  ```powershell
  .venv\Scripts\python -m herdr_slackbot check     # check settings and the Herdr connection
  .venv\Scripts\python -m herdr_slackbot status
  .venv\Scripts\python -m herdr_slackbot manifest  # print the manifest for the current settings
  .venv\Scripts\python -m herdr_slackbot marker-check  # round-trip the duplicate-prevention marker through real Slack
  ```

If tokens are missing or wrong, the bridge doesn't crash or restart in a loop. It prints the reason and what to do next in the `herdr-slack` pane once and exits, leaving the shell open. Fix the settings and run the `restart` action.

For your first run against real Slack, follow the checklist in `docs/LIVE_TEST.md` in order.

## Using it in Slack

Below, `/herdr` stands for your own `SLASH_COMMAND` (e.g. `/herdr-kim`). Use it in the bot DM.

| Command | What it does |
|---|---|
| `/herdr` | Usage |
| `/herdr list` | List agents (by workspace, with status emoji) |
| `/herdr new` | Start a new agent from a modal: workspace, cwd, kind (claude/codex), model, effort, permission mode, name, prompt |
| `/herdr new <workspace> [name=..] [kind=claude\|codex] [model=..] [effort=..] [mode=..] [cwd=..] <prompt>` | Start directly, without the modal |
| `/herdr send` | Send a prompt to a running agent from a modal. Once you pick an agent, its **last response** (with time and duration) is shown between Agent and Prompt; long responses show only the last ~2500 characters. The Send button on the Home tab opens the same modal. |
| `/herdr send <agent name\|pane id> <prompt>` | Send directly |
| `/herdr status` | Bridge status |
| `/herdr pair <code>` | Link your account in pairing mode (see **Pairing** above). Rejected once paired. |

- **New agent:** Creates a new tab in the chosen workspace and starts the agent there. Defaults are claude `--model opus --effort high --permission-mode auto`. A blank name gets `slack-<N>`.
- **Send rules:** Prompts are sent only when the target is `idle`/`done`.
  - `working` → replies "busy".
  - `blocked` → tells you to answer with the buttons in the thread (`/herdr send`, modal).
- **Threads:** Each agent gets one DM thread.
  - For work sent from Slack, "⏳ started" and the final result arrive as thread replies.
  - Replying in the thread sends the reply to that agent as a prompt. If the agent is waiting for an answer, the reply becomes the typed answer to that question (see **Answering dialogs** below).
  - If the agent has exited or a different session now runs in the same pane, the reply is not forwarded and you get a notice instead.
- **Notifications:** Agents started directly on the PC are covered too.
  - Work that finishes (`done`) in a tab you aren't looking at is notified.
  - When an agent becomes `blocked`, the dialog is posted with buttons.
  - The 🔕 button in a thread mutes that agent. Results of work sent from Slack arrive regardless of mute.
- **Result text:**
  - For claude, the last answer is read from the session JSONL, falling back to parsing the screen. For codex, the end of the screen is sent.
  - Over 3000 characters, the text is truncated and a **[View full]** button uploads the full content as a `.md` file.

### Answering dialogs

When an agent becomes `blocked`, the thread gets a **"⚠️ <name> · <workspace> is waiting for your answer"** message. The bridge reads the dialog from the screen and adds a button for each option.

- **Supported:** Claude permission prompts (Bash etc.), AskUserQuestion (single choice, multiple choice, multiple questions, free text), plan approval (ExitPlanMode), the folder trust prompt at startup (Claude, Codex), and Codex command approval.
- **Buttons:** Each option gets a button like `1. Yes`. "Always allow / don't ask again" options show up as buttons too. Multiple choice uses ☐/☑ toggle buttons, and **[Next →]** moves the cursor to the question's Submit row and presses Enter (the next question, or the review screen). **[Esc]** and **[Show screen]** (posts the last 40 lines of the screen to the thread) are always there.
- **Free text:** The "Type something." / "Tell Claude what to change" button opens an input modal. Replying in the thread gives the same answer. Several lines are sent as they are (a line break only breaks the line in the agent's input; it does not submit). In a multiple-choice question the typed text also ticks the "Type something" box; press **[Next →]** to submit the question. Replying to a question without a free-text option gets "This question needs one of the buttons above."
- **Plan approval:** The plan file (`~\.claude\plans\…md`) is read and its text shown; long plans get **[View full]** to upload the whole thing.
- **Safeguards:** Pressing a button first re-checks the agent and the screen. If you already answered on the PC or the question changed in the meantime (including a different plan behind the same approval question), no keys are sent and only the message is updated. Pressing twice still sends the keys once; a button from an older version of the message only gets "That button was out of date".
- **After answering:** If another question follows, the same message is replaced with the new question. When the agent moves on, the message becomes `✅ <choice> — answered from Slack` and the buttons disappear. If you answer on the PC, it becomes `✅ answered on PC` (also after a bridge restart). If the screen doesn't change within 5 seconds, the buttons stay and you get "Could not confirm the answer".
- **When the screen can't be read:** A keypad (`1`–`4`, `↑`, `↓`, Enter, Esc) and the last 15 lines of the screen are shown.
- **If an agent started from Slack asks for folder trust right away:** A thread is created with that question, and the original prompt is sent after you answer.
- **Codex:** If `~/.codex/config.toml` delegates approvals with `approvals_reviewer = "auto_review"`, Codex never shows an approval screen, so nothing reaches Slack either. That's configuration, not a bug.

### Home tab

Open the bot from the **Apps** list in Slack's sidebar and click the **Home** tab for a dashboard.

- **Top:** Bridge status (uptime, agent count, slash command, last update) and three buttons:
  - **[➕ New Agent]**: opens the `/herdr new` modal.
  - **[📤 Send]**: opens the `/herdr send` modal.
  - **[🔄 Refresh]**: redraws immediately.
- **Below:** Agents by workspace (status emoji · name/pane · kind · status · terminal title). The **[Send]** button on an idle/done agent's row opens the send modal with that agent preselected.
- **Auto refresh:** Redrawn every time you open Home. After you've opened it once, it also refreshes when agent status changes, batched to at most once every 5 seconds.
- **Display limit:** Up to 100 blocks per view; the rest are collapsed into "외 N개" (N more).
- **Owner only:** If another user opens the Home tab, nothing is published, so they never see agent information.

## Troubleshooting

| Symptom | What to check |
|---|---|
| Slash command doesn't respond | 1. Look at the output in the `herdr-slack` workspace pane.<br>2. Run the `status` action.<br>3. Check `STATE_DIR\bridge.log`. |
| `missing settings in ...` in the pane | Fill in both tokens (`SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN`) in `.env` and run the `restart` action. You can also use the wizard (`setup` action). |
| "🔗 This Herdr bridge is not paired yet" | Pairing mode. Send `/herdr pair <code>` with the code from the `herdr-slack` pane or the Herdr notification. |
| "❌ Wrong pairing code" / "⌛ That code expired" | Use the **most recent** code shown in the pane. The code changes after 5 wrong attempts or 15 minutes. |
| "⏳ Too many wrong codes from you" | You got it wrong 5 times within 10 minutes. Wait the indicated time, then try again with the **most recent** code in the pane. |
| "You are paired, but the bridge could not start" | Your account was saved, but owner-only mode failed to start. Check the error in the pane and run the `restart` action. |
| `Slack connection failed: invalid_auth` | Check that the `xoxb`/`xapp` tokens are correct, the app is installed to the workspace, and the App-Level Token has `connections:write`. |
| `/herdr-...` gives "dispatch_failed" or another app answers | Check that the bridge is running. Also check whether another app uses a command with the same name (names must be unique across the workspace). |
| "⛔ This Herdr bridge only accepts requests from its owner." | It is paired with a different account. Clear `SLACK_OWNER_USER_ID` in `.env`, `restart`, and pair again. |
| Can't type in the DM window | Check the App Home messages tab setting (Manual setup, step 2.6). |
| `another bridge is running (pid N)` | It's already running. Check with the `status` action and run the `restart` action if needed. |
| Wizard stops with "needs a terminal" | It was run somewhere that can't take input (plugin log, pipe). Use the `setup` action or run it from a terminal. |
| A plugin action seems to do nothing | Look at stdout/stderr in `herdr plugin log list --plugin herdr-slackbot`. |
| Broken venv | Delete `.venv` in the plugin folder and run `powershell -NoProfile -ExecutionPolicy Bypass -File scripts\setup.ps1` again. |
| `cannot verify the running bridge` | The running bridge couldn't be verified, so it wasn't stopped. Stop it yourself with Ctrl+C in the bridge pane of the `herdr-slack` workspace. |

## Security

- **Owner only:** Slash commands, buttons, modals and DM messages are handled for the single user in `SLACK_OWNER_USER_ID`. Other users who try a command get a rejection only they can see.
- **Pairing:** While the value is empty, only `/herdr pair <code>` is accepted. The code is shown only on the PC (bridge pane, Herdr notification) and never written to logs. It is a 6-digit random number compared in constant time. It is replaced after 5 wrong attempts in total or 15 minutes, and no single user can try more than 5 times in 10 minutes. Before pairing, anyone in the same workspace who knows the slash command name can attempt `pair`, so pair right after installing.
- **Code output goes to Slack.** Agent answers, the end of the screen, working folder paths and terminal titles are posted to your Slack DM. For sensitive repositories, use 🔕 mute or turn the bridge off (`stop` action).
- When starting a new agent from Slack, the only permission modes available are `manual`/`acceptEdits`/`auto`/`plan`. `bypassPermissions` is not offered.
- Tokens live only in `.env` in the plugin config folder. That file is outside the git repository, and the repo's `.gitignore` also lists `.env`. `xoxb-`/`xapp-` tokens are masked in logs.
- Socket Mode only uses outbound connections from your PC to Slack. There are no inbound ports or public URLs.

## Development

```powershell
.venv\Scripts\python -m pip install pytest
.venv\Scripts\python -m pytest -q                       # offline tests
$env:HERDR_LIVE_TESTS=1; .venv\Scripts\python -m pytest tests/test_live.py   # against a running Herdr (read-only)
```

Design and decisions are in `docs/SPEC.md`, and per-milestone progress notes are in `docs/progress/`.
