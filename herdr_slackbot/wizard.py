"""`python -m herdr_slackbot wizard`: interactive first-time setup (release R1).

1. `.env` skeleton + Slack app manifest (`run_setup`); asks for the slash command / bot name only
   when `.env` has no value for them yet.
2. Opens Slack's app-creation page with the manifest prefilled (URL-encoded JSON in the link);
   too long for the browser -> the manifest file is pasted by hand.
3. Asks for the App-Level Token (xapp-) and the Bot token (xoxb-), verifies them with
   `apps.connections.open` / `auth.test` and re-asks on failure. Valid tokens already in `.env`
   are kept unless the user wants to replace them. Tokens are never echoed back in full.
4. Writes them into `.env` in place (`update_env_file`).
5. Starts or restarts the bridge (plugin lifecycle, `PluginOps.restart`).
6. Without SLACK_OWNER_USER_ID: shows the pairing code (pairing.py) and waits until the bridge
   has written the owner into `.env`.
7. Prints a summary.

Needs an interactive terminal: with redirected stdin (e.g. Herdr's build step or a plugin
action) it refuses and says how to run it.
"""

from __future__ import annotations

import json
import sys
import time
import urllib.parse
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .config import (
    ENV_FILE_NAME,
    PLUGIN_ID,
    ConfigError,
    current_username,
    default_bot_display_name,
    default_slash_command,
    load_config,
    load_env_file,
    normalize_slash_command,
    redact,
)
from .pairing import STATE_ACTIVATING, STATE_FAILED, read_pairing, read_pairing_record
from .setup_cmd import run_setup, update_env_file
from .slack_manifest import build_manifest

SLACK_NEW_APP_URL = "https://api.slack.com/apps?new_app=1&manifest_json="
# Windows hands URLs to the browser through ShellExecute; stay well below INTERNET_MAX_URL_LENGTH
# (2083). Typical manifests encode to ~1.4 KB.
MAX_BROWSER_URL = 2000
PAIRING_WAIT = 600.0  # seconds the wizard waits for pairing
POLL_INTERVAL = 2.0
NO_CODE_HINT_AFTER = 60.0  # seconds without a pairing code before pointing at the bridge pane
READY_WAIT = 60.0  # seconds the wizard waits for the bridge to report owner mode as running

NOT_INTERACTIVE_TEXT = f"""The setup wizard is interactive and needs a terminal (stdin is not a console here).
Run it in a terminal instead:
  herdr plugin action invoke setup --plugin {PLUGIN_ID}     (opens a new Herdr tab with the wizard)
or in PowerShell, from the plugin folder:
  powershell -NoProfile -ExecutionPolicy Bypass -File scripts\\setup.ps1 -Wizard"""

APP_TOKEN_HELP = """App-Level Token (xapp-...):
  Slack app page -> Basic Information -> App-Level Tokens -> Generate Token and Scopes
  -> any name, Add Scope: connections:write -> Generate -> copy the xapp-... token"""

BOT_TOKEN_HELP = """Bot token (xoxb-...):
  Slack app page -> Install App (left menu) -> Install to Workspace -> Allow
  -> copy the Bot User OAuth Token xoxb-..."""


class TokenError(Exception):
    """Slack rejected a token (message = Slack's error code or a short reason)."""


def manifest_url(manifest: dict, limit: int = MAX_BROWSER_URL) -> str | None:
    """Slack's prefilled app-creation link, or None when it would be too long for the browser."""
    compact = json.dumps(manifest, separators=(",", ":"), ensure_ascii=False)
    url = SLACK_NEW_APP_URL + urllib.parse.quote(compact, safe="")
    return url if len(url) <= limit else None


def mask_token(token: str) -> str:
    """`xoxb-…abcd`: enough to recognise a token, never the whole thing."""
    token = token or ""
    prefix = token.split("-", 1)[0] + "-" if "-" in token else ""
    return f"{prefix}…{token[-4:]}" if len(token) > len(prefix) + 8 else f"{prefix}…"


def stdin_is_interactive(stream=None) -> bool:
    """A real console on stdin. On Windows `isatty()` is also true for NUL (what Herdr gives plugin
    commands), so the console mode is checked as well."""
    stream = sys.stdin if stream is None else stream
    try:
        if stream is None or not stream.isatty():
            return False
        fd = stream.fileno()
    except (AttributeError, ValueError, OSError):
        return False
    if sys.platform != "win32":
        return True
    try:
        import ctypes
        import msvcrt

        mode = ctypes.c_uint32()
        return bool(ctypes.windll.kernel32.GetConsoleMode(msvcrt.get_osfhandle(fd), ctypes.byref(mode)))
    except (OSError, AttributeError, ValueError):
        return False


def _slack_error(exc: Exception, token: str) -> str:
    """A short, printable reason. Exception text (proxies, SDK errors) may quote the token: it is
    redacted, together with anything else that looks like a token."""
    from slack_sdk.errors import SlackApiError

    if isinstance(exc, SlackApiError):
        text = str(exc.response.get("error") if exc.response is not None else "slack_error")
    else:
        text = f"{type(exc).__name__}: {str(exc).splitlines()[0] if str(exc) else ''}".strip().rstrip(":")
    return redact(text, token)


def _default_client(token: str | None = None):
    from slack_sdk import WebClient

    return WebClient(token=token, timeout=15)


def verify_bot_token(token: str, client_factory: Callable = _default_client) -> str:
    """auth.test with the bot token; returns "workspace X, bot @Y". Raises TokenError."""
    try:
        resp = client_factory(token).auth_test()
    except Exception as exc:  # SlackApiError or network failure: show it and ask again
        raise TokenError(_slack_error(exc, token)) from None
    if not resp.get("bot_id"):
        raise TokenError("not a bot token (auth.test has no bot_id)")
    return f"workspace {resp.get('team') or '?'}, bot @{resp.get('user') or '?'}"


def verify_app_token(token: str, client_factory: Callable = _default_client) -> None:
    """apps.connections.open with the app-level token (proves connections:write + Socket Mode)."""
    try:
        client_factory(None).apps_connections_open(app_token=token)
    except Exception as exc:
        raise TokenError(_slack_error(exc, token)) from None


@dataclass
class TokenSpec:
    key: str
    prefix: str
    label: str
    help: str
    verify: Callable[[str], str | None]


def bridge_readiness(state_dir: Path) -> dict | None:
    """The running bridge's ready marker (owner mode started), verified against the bridge lock."""
    from .plugin import verified_ready

    return verified_ready(state_dir)


def restart_bridge(config_dir: Path, out: Callable[[str], None]) -> int:
    """Start the bridge, or restart it when it is running (plugin lifecycle code)."""
    from .plugin import HerdrCli, PluginError, PluginOps, plugin_root_from_env

    try:
        return PluginOps(HerdrCli(), config_dir, plugin_root_from_env(), out=out).restart()
    except PluginError as exc:
        out(f"could not start the bridge: {exc}")
        return 1


class Wizard:
    def __init__(self, config_dir: Path, *, username: str | None = None, slash_command: str | None = None,
                 display_name: str | None = None, ask: Callable[[str], str] = input,
                 out: Callable[[str], None] = print, open_browser: Callable[[str], bool] = webbrowser.open,
                 client_factory: Callable = _default_client, start_bridge: Callable[[Path, Callable], int] = restart_bridge,
                 interactive: Callable[[], bool] = stdin_is_interactive, clock: Callable[[], float] = time.time,
                 sleep: Callable[[float], None] = time.sleep, pairing_wait: float = PAIRING_WAIT,
                 readiness: Callable[[Path], dict | None] = bridge_readiness, ready_wait: float = READY_WAIT):
        self.config_dir = Path(config_dir)
        self.env_file = self.config_dir / ENV_FILE_NAME
        self.username = username or current_username()
        self.slash_default = slash_command
        self.name_default = display_name
        self.ask = ask
        self.out = out
        self.open_browser = open_browser
        self.client_factory = client_factory
        self.start_bridge = start_bridge
        self.interactive = interactive
        self.clock = clock
        self.sleep = sleep
        self.pairing_wait = pairing_wait
        self.readiness = readiness
        self.ready_wait = ready_wait

    # --- prompts ----------------------------------------------------------------------------
    def _prompt(self, question: str, default: str = "") -> str:
        shown = f" [{default}]" if default else ""
        answer = self.ask(f"{question}{shown}: ").strip()
        return answer or default

    def _confirm(self, question: str, default: bool) -> bool:
        while True:
            answer = self.ask(f"{question} [{'Y/n' if default else 'y/N'}]: ").strip().lower()
            if not answer:
                return default
            if answer in ("y", "yes"):
                return True
            if answer in ("n", "no"):
                return False
            self.out("  please answer y or n")

    def _section(self, title: str) -> None:
        self.out("")
        self.out(f"== {title} ==")

    # --- steps ---------------------------------------------------------------------------------
    def run(self) -> int:
        if not self.interactive():
            self.out(NOT_INTERACTIVE_TEXT)
            return 2
        self.out("herdr-slackbot setup wizard  (Ctrl+C to quit; your answers so far are kept in .env)")
        try:
            return self._run()
        except (KeyboardInterrupt, EOFError):
            self.out("")
            self.out("wizard stopped. Run it again any time; it picks up where you left off.")
            return 130

    def _run(self) -> int:
        self._section("1/5 Settings")
        try:
            result = self.step_settings()
        except ConfigError as exc:
            self.out(f"setup error: {exc}")
            return 2
        self.step_tokens(result)
        self._section("4/5 Bridge")
        self.out("starting the bridge in Herdr workspace herdr-slack ...")
        started_at = self.clock()
        code = self.start_bridge(self.config_dir, self.out)
        if code != 0:
            self.out("The bridge did not start. Fix the problem above, then:")
            self.out(f"  herdr plugin action invoke restart --plugin {PLUGIN_ID}")
            return 1
        paired = self.step_pairing(started_at)
        self.step_summary(paired)
        return 0 if paired else 1

    def step_settings(self):
        """Step 1: `.env` skeleton + manifest; ask for the identity only where `.env` has no value."""
        existing = load_env_file(self.env_file)  # before run_setup, which fills in the defaults
        answers: dict[str, str] = {}
        if not existing.get("SLASH_COMMAND"):
            default = self.slash_default or default_slash_command(self.username)
            self.out("Slash command: must be unique in your Slack workspace (e.g. /herdr-<your name>).")
            while True:
                try:
                    answers["SLASH_COMMAND"] = normalize_slash_command(self._prompt("  slash command", default))
                    break
                except ConfigError as exc:
                    self.out(f"  {exc}")
        if not existing.get("BOT_DISPLAY_NAME"):
            default = self.name_default or default_bot_display_name(self.username)
            answers["BOT_DISPLAY_NAME"] = self._prompt("  bot name", default)[:80]
        # Keys already in .env (blank) are set in place; missing keys come in with the skeleton.
        in_place = {k: v for k, v in answers.items() if k in existing}
        if in_place:
            update_env_file(self.env_file, in_place)
        result = run_setup(self.config_dir, username=self.username,
                           slash_command=None if "SLASH_COMMAND" in in_place else answers.get("SLASH_COMMAND"),
                           display_name=None if "BOT_DISPLAY_NAME" in in_place else answers.get("BOT_DISPLAY_NAME"))
        self.out(f"  .env:           {result.env_file}")
        self.out(f"  slash command:  {result.slash_command}")
        self.out(f"  bot name:       {result.display_name}")
        self.out(f"  manifest:       {result.manifest_file}")
        return result

    def _token_specs(self) -> list[TokenSpec]:
        return [
            TokenSpec("SLACK_APP_TOKEN", "xapp-", "App-Level Token", APP_TOKEN_HELP,
                      lambda t: verify_app_token(t, self.client_factory)),
            TokenSpec("SLACK_BOT_TOKEN", "xoxb-", "Bot token", BOT_TOKEN_HELP,
                      lambda t: verify_bot_token(t, self.client_factory)),
        ]

    def step_tokens(self, result) -> dict[str, str]:
        """Steps 2-3: create the Slack app (browser) and collect verified tokens; saves them."""
        values = load_env_file(self.env_file)
        keep: dict[str, str] = {}
        specs = self._token_specs()
        self._section("2/5 Slack app")
        for spec in specs:
            token = values.get(spec.key) or ""
            if not token:
                continue
            try:
                detail = spec.verify(token)
            except TokenError as exc:
                self.out(f"  {spec.key} in .env ({mask_token(token)}) does not work: {exc}")
                continue
            self.out(f"  {spec.key} in .env is valid ({mask_token(token)}{', ' + detail if detail else ''})")
            if not self._confirm(f"  replace {spec.key}?", default=False):
                keep[spec.key] = token
        needed = [spec for spec in specs if spec.key not in keep]
        if not needed:
            self.out("  both tokens are set and valid; skipping app creation.")
            return keep
        if self._confirm("Create the Slack app now? (opens your browser with the manifest prefilled)",
                         default=len(needed) == len(specs)):
            self.step_browser(result)
        self._section("3/5 Tokens")
        self.out("Open your app at https://api.slack.com/apps (pick it in the list) and copy:")
        new: dict[str, str] = {}
        for spec in needed:
            new[spec.key] = self._ask_token(spec)
        update_env_file(self.env_file, new)
        self.out(f"  saved {', '.join(new)} to {self.env_file}")
        return {**keep, **new}

    def step_browser(self, result) -> None:
        manifest = json.loads(Path(result.manifest_file).read_text(encoding="utf-8"))
        url = manifest_url(manifest)
        if url is None:
            self.out("The manifest is too long for a browser link. Create the app by hand:")
            self._manual_steps(result)
            return
        opened = False
        try:
            opened = bool(self.open_browser(url))
        except Exception:
            opened = False
        self.out("  " + ("opened your browser at:" if opened else "open this link in your browser:"))
        self.out(f"  {url}")
        self.out("  In Slack: pick the workspace -> Next -> check the manifest -> Create.")
        self.out(f"  If the form comes up empty, paste the manifest by hand: {result.manifest_file}")
        self._manual_steps(result, short=True)

    def _manual_steps(self, result, short: bool = False) -> None:
        if not short:
            self.out("  1. https://api.slack.com/apps -> Create New App -> From a manifest -> pick the workspace")
            self.out(f"  2. JSON tab: replace the content with {result.manifest_file} -> Next -> Create")
        self.out("  (If Slack asks to install the app, do it; the Bot token appears on the Install App page.)")

    def _ask_token(self, spec: TokenSpec) -> str:
        self.out("")
        self.out(spec.help)
        while True:
            token = self.ask(f"  paste the {spec.label} ({spec.prefix}...): ").strip().strip('"').strip("'")
            if not token:
                continue
            if not token.startswith(spec.prefix):
                self.out(f"  that is not a {spec.prefix} token (it starts with {token[:5]!r}); try again")
                continue
            try:
                detail = spec.verify(token)
            except TokenError as exc:
                self.out(f"  Slack rejected {mask_token(token)}: {exc}. Check the token and try again.")
                continue
            self.out(f"  ok {mask_token(token)}" + (f" ({detail})" if detail else ""))
            return token

    def step_pairing(self, started_at: float) -> bool:
        """Step 5: wait for pairing (the bridge writes SLACK_OWNER_USER_ID into .env)."""
        self._section("5/5 Pairing")
        cfg = load_config(config_dir=self.config_dir)
        if not cfg.needs_pairing:
            self.out(f"  already paired with Slack user {cfg.slack_owner_user_id}; checking that the bridge serves it ...")
            return self.wait_ready(cfg, started_at)
        cmd = cfg.slash_command
        self.out("The bridge now waits for you to prove which Slack account is yours:")
        self.out(f"  1. In Slack, open the DM with the bot ({cfg.bot_display_name}; left sidebar -> Apps).")
        self.out(f"  2. Send:  {cmd} pair <code>   with the code below (also shown in the herdr-slack pane).")
        self.out(f"  Waiting up to {int(self.pairing_wait // 60)} minutes (Ctrl+C stops waiting; the bridge keeps "
                 "waiting).")
        deadline = self.clock() + self.pairing_wait
        shown: str | None = None
        hinted = activating = False
        try:
            while True:
                if load_env_file(self.env_file).get("SLACK_OWNER_USER_ID"):
                    # The bridge saves the owner first, then starts owner mode (pairing.py).
                    state = (read_pairing_record(cfg.state_dir) or {}).get("state")
                    if state == STATE_FAILED:
                        self.out("  the code was accepted and saved, but the bridge could not start for you "
                                 "(see the herdr-slack pane). Restart it:")
                        self.out(f"    herdr plugin action invoke restart --plugin {PLUGIN_ID}")
                        return False
                    if state != STATE_ACTIVATING:
                        return self.wait_ready(cfg, started_at)
                    if not activating:
                        activating = True
                        self.out("  code accepted; the bridge is starting for you ...")
                rec = read_pairing(cfg.state_dir)
                # only codes of the bridge started just now (an old pairing.json may be left over)
                if rec and float(rec.get("created") or 0) >= started_at - 5 and rec["code"] != shown:
                    shown = rec["code"]
                    self.out("")
                    self.out(f"  pairing code:  {shown}      ->  in Slack: {cmd} pair {shown}")
                elif shown is None and not hinted and self.clock() - started_at > NO_CODE_HINT_AFTER:
                    hinted = True
                    self.out("  no pairing code yet; look at the bridge pane in workspace herdr-slack for errors.")
                if self.clock() >= deadline:
                    self.out("  still not paired. The bridge keeps waiting: run the pair command whenever you are "
                             "ready (restart the bridge for a new code).")
                    return False
                self.sleep(POLL_INTERVAL)
        except KeyboardInterrupt:
            self.out("")
            self.out(f"  stopped waiting. The bridge still accepts `{cmd} pair <code>` (code in the herdr-slack pane).")
            return False

    def wait_ready(self, cfg, started_at: float) -> bool:
        """Success only once the bridge started just now reports owner mode running for the owner in
        .env (ready marker of the verified lock holder), not merely because the owner key is set."""
        deadline = self.clock() + self.ready_wait
        try:
            while True:
                owner = load_env_file(self.env_file).get("SLACK_OWNER_USER_ID") or cfg.slack_owner_user_id
                rec = self.readiness(cfg.state_dir)
                if rec and rec.get("owner") == owner and float(rec.get("at") or 0) >= started_at - 5:
                    self.out(f"  paired ✅ the bridge is running for Slack user {owner}")
                    return True
                if (read_pairing_record(cfg.state_dir) or {}).get("state") == STATE_FAILED:
                    self.out("  the code was accepted and saved, but the bridge could not start for you "
                             "(see the herdr-slack pane). Restart it:")
                    self.out(f"    herdr plugin action invoke restart --plugin {PLUGIN_ID}")
                    return False
                if self.clock() >= deadline:
                    self.out(f"  the bridge did not report ready within {int(self.ready_wait)}s. Look at the bridge "
                             "pane in workspace herdr-slack, fix the problem, then:")
                    self.out(f"    herdr plugin action invoke restart --plugin {PLUGIN_ID}")
                    return False
                self.sleep(POLL_INTERVAL)
        except KeyboardInterrupt:
            self.out("")
            self.out(f"  stopped waiting; check with: herdr plugin action invoke status --plugin {PLUGIN_ID}")
            return False

    def step_summary(self, paired: bool) -> None:
        cfg = load_config(config_dir=self.config_dir)
        self._section("Done" if paired else "Not finished")
        self.out(f"  slash command:  {cfg.slash_command}   (e.g. {cfg.slash_command} list, {cfg.slash_command} new)")
        self.out(f"  bot DM:         Slack -> Apps -> {cfg.bot_display_name}; notifications arrive there")
        if not paired and cfg.needs_pairing:
            self.out(f"  pairing:        run `{cfg.slash_command} pair <code>` in that DM")
        elif not paired:
            self.out("  bridge:         not serving yet; see the bridge pane in workspace herdr-slack, then restart")
        self.out(f"  status:         herdr plugin action invoke status --plugin {PLUGIN_ID}")
        self.out(f"  restart:        herdr plugin action invoke restart --plugin {PLUGIN_ID}   (after editing .env)")
        self.out(f"  settings:       {self.env_file}")


def main_wizard(config_dir: Path, username: str | None = None, slash_command: str | None = None,
                display_name: str | None = None) -> int:
    return Wizard(config_dir, username=username, slash_command=slash_command, display_name=display_name).run()
