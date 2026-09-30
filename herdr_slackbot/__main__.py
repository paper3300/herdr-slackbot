"""Entry point.

    python -m herdr_slackbot [run]          run the bridge (normally inside the herdr-slack pane)
    python -m herdr_slackbot check          verify config and Herdr connectivity (`--check` also works)
    python -m herdr_slackbot setup          write the .env skeleton + Slack app manifest into the config dir
    python -m herdr_slackbot wizard         interactive setup: Slack app, tokens, bridge start, pairing
    python -m herdr_slackbot manifest       print the Slack app manifest for the current config
    python -m herdr_slackbot launch|stop|restart|status|open-wizard   plugin hook / action commands (see plugin.py)
"""

from __future__ import annotations

import argparse
import logging
import logging.handlers
import os
import signal
import sys
import threading
from pathlib import Path

from .config import SECRET_RE, Config, ConfigError, load_config, redact
from .herdr_client import HerdrClient, HerdrError
from .naming import agent_option_label

log = logging.getLogger("herdr_slackbot")

_SECRET_RE = SECRET_RE


NOT_CONFIGURED_HELP = """The bridge is not configured yet, so it did not start (this is not a crash).
  1. scripts\\setup.ps1 in the plugin folder (writes the .env skeleton + Slack app manifest)
  2. create the Slack app from the manifest and fill the tokens into .env (see README)
  3. herdr plugin action invoke restart --plugin herdr-slackbot"""

SLACK_FAILED_HELP = """Slack rejected the connection, so the bridge stopped (it will not retry on its own).
Check SLACK_BOT_TOKEN (xoxb-), SLACK_APP_TOKEN (xapp-, scope connections:write) and that
Socket Mode is enabled for the app, in {env}.
Then: herdr plugin action invoke restart --plugin herdr-slackbot"""


def slack_failure_reason(exc: BaseException) -> str | None:
    """A short reason for Slack auth/setup failures at startup, or None for other errors."""
    from slack_bolt.error import BoltError
    from slack_sdk.errors import SlackApiError, SlackClientError

    if isinstance(exc, SlackApiError):
        return redact(str(exc.response.get("error") if exc.response is not None else exc))
    if isinstance(exc, (BoltError, SlackClientError)):
        return redact(str(exc).splitlines()[0] if str(exc) else type(exc).__name__)
    return None


class RedactSecrets(logging.Filter):
    """Redacts the message itself (for handlers without `RedactingFormatter`)."""

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        if _SECRET_RE.search(msg):
            record.msg = redact(msg)
            record.args = None
        return True


class RedactingFormatter(logging.Formatter):
    """Redacts the final rendered record, including exception tracebacks and stack info."""

    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))

    def formatException(self, ei) -> str:  # noqa: N802 - logging API name
        return redact(super().formatException(ei))

    def formatStack(self, stack_info: str) -> str:  # noqa: N802 - logging API name
        return redact(super().formatStack(stack_info))


def setup_logging(cfg: Config) -> None:
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    fmt = RedactingFormatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    file_handler = logging.handlers.RotatingFileHandler(cfg.state_dir / "bridge.log", maxBytes=2_000_000,
                                                        backupCount=3, encoding="utf-8")
    stream_handler = logging.StreamHandler(sys.stdout)
    root = logging.getLogger()
    root.setLevel(cfg.log_level)
    for handler in (file_handler, stream_handler):
        handler.setFormatter(fmt)
        handler.addFilter(RedactSecrets())
        root.addHandler(handler)
    logging.getLogger("slack_sdk").setLevel(max(logging.INFO, root.level))
    logging.getLogger("slack_bolt").setLevel(max(logging.INFO, root.level))


def check() -> int:
    try:
        cfg = load_config()
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    print(f"config dir:   {cfg.config_dir}")
    print(f"state dir:    {cfg.state_dir}")
    print(f"slash cmd:    {cfg.slash_command}")
    print(f"bot name:     {cfg.bot_display_name}")
    missing = cfg.missing_slack_settings()
    print(f"slack config: {'missing ' + ', '.join(missing) if missing else 'ok'}")
    print(f"owner:        {cfg.slack_owner_user_id or 'not paired yet (the bridge starts in pairing mode)'}")
    client = HerdrClient.from_env(cfg.herdr_socket_path or None, cfg.herdr_bin)
    try:
        pong = client.ping()
        workspaces = {w["workspace_id"]: w["label"] for w in client.list_workspaces()}
        agents = client.list_agents()
    except HerdrError as exc:
        print(f"herdr: {exc}", file=sys.stderr)
        return 1
    print(f"herdr:        {pong.get('version', pong)} ({len(agents)} agents)")
    for agent in agents:
        print("  " + agent_option_label(agent, workspaces))
    return 0


def run(launch_id: str | None = None) -> int:
    try:
        cfg = load_config()
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    print(f"herdr-slackbot bridge — config {cfg.env_file}")
    missing = cfg.missing_slack_settings()
    if missing:
        print(f"missing settings in {cfg.env_file}: {', '.join(missing)}", file=sys.stderr)
        print(NOT_CONFIGURED_HELP, file=sys.stderr)
        return 2
    setup_logging(cfg)

    from slack_sdk import WebClient

    from .bridge import Bridge
    from .results import ResultStore
    from .slack_app import build_app, run_socket_mode
    from .slack_transport import WebClientTransport
    from .state import AlreadyRunning, InstanceLock, StateStore

    from . import procinfo
    from .plugin import (
        PluginError,
        ack_launch,
        clear_ready_marker,
        clear_stop_request,
        launch_admitted,
        lifecycle_lock,
        stop_requested,
        write_ready_marker,
        write_runtime_record,
    )

    # Handoff from the launcher, under the same lifecycle lock launch/stop hold for their whole
    # check -> act sequence: a launcher sees either "starting" (reservation) or "running" (lock),
    # never neither, and a stop issued while we were starting cancels us here.
    try:
        with lifecycle_lock(cfg.state_dir):
            if launch_id and not launch_admitted(cfg.state_dir, launch_id):
                print("this start was cancelled (stop) or superseded by a newer start; not starting",
                      file=sys.stderr)
                return 0
            try:
                lock = InstanceLock(cfg.state_dir).acquire()
            except AlreadyRunning as exc:
                log.error("%s", exc)
                return 3
            try:
                client = HerdrClient.from_env(cfg.herdr_socket_path or None, cfg.herdr_bin)
                client.ping()
                pane_id = os.environ.get("HERDR_PANE_ID")
                pane = None
                if pane_id:
                    try:
                        pane = client.get_pane(pane_id)
                    except HerdrError as exc:
                        log.warning("could not look up own pane %s: %s", pane_id, exc)
                clear_stop_request(cfg.state_dir)  # any request on disk predates this process
                write_runtime_record(cfg.state_dir, pane)
            except HerdrError as exc:
                lock.release()
                log.error("Herdr is not reachable: %s", exc)
                return 1
            except BaseException:
                lock.release()
                raise
            if ack_launch(cfg.state_dir, launch_id):
                _close_previous_tab(cfg.state_dir, cfg.herdr_bin)
    except PluginError as exc:
        print(f"could not coordinate with the plugin launcher: {exc}", file=sys.stderr)
        return 1
    my_start = procinfo.current_start_time()
    stop = threading.Event()

    def request_stop(*_):
        stop.set()

    for sig in (signal.SIGINT, signal.SIGTERM, getattr(signal, "SIGBREAK", None)):
        if sig is not None:
            signal.signal(sig, request_stop)

    handler = bridge = pairing = None
    try:
        web = WebClient(token=cfg.slack_bot_token)
        transport = WebClientTransport(web)
        bridge = Bridge(cfg, client, StateStore(cfg.state_dir / "state.json"), transport,
                        ResultStore(cfg.state_dir / "results"))
        if cfg.needs_pairing:
            pairing = make_pairing(cfg)
            def activate(user: str) -> None:  # the marker is written before pairing.json goes away
                bridge.activate_owner(user)
                write_ready_marker(cfg.state_dir, user)

            app = build_app(cfg, bridge, client=web, pairing=pairing,
                            on_paired=lambda user: pairing.complete(activate), transport=transport)
        else:
            app = build_app(cfg, bridge, client=web)
            bridge.start()
        handler = run_socket_mode(app, cfg.slack_app_token)
        if pairing is not None:
            log.info("bridge running: %s, waiting for pairing (no SLACK_OWNER_USER_ID yet)", cfg.slash_command)
            pairing.begin()  # only once Slack is connected, so the code shown can be used right away
        else:
            write_ready_marker(cfg.state_dir, cfg.slack_owner_user_id)
            log.info("bridge running: %s for owner %s", cfg.slash_command, cfg.slack_owner_user_id)
        while not stop.wait(1.0):
            if stop_requested(cfg.state_dir, os.getpid(), my_start):
                log.info("stop requested by the plugin")
                break
            if pairing is not None:
                pairing.tick()
        log.info("shutting down")
        return 0
    except KeyboardInterrupt:
        log.info("interrupted; shutting down")
        return 0
    except HerdrError as exc:
        log.error("Herdr is not reachable: %s", exc)
        return 1
    except Exception as exc:  # Slack auth / connection failures: one clear line, no crash loop
        reason = slack_failure_reason(exc)
        if reason is None:
            raise
        log.error("Slack connection failed: %s", reason)
        print(SLACK_FAILED_HELP.format(env=cfg.env_file), file=sys.stderr)
        return 2
    finally:
        if handler is not None:
            try:
                handler.close()
            except Exception:
                log.exception("closing Socket Mode failed")
        if bridge is not None:
            bridge.stop()
        if pairing is not None and pairing.owner is None:
            from .pairing import delete_pairing

            delete_pairing(cfg.state_dir)  # the code (or activation state) dies with this process
        clear_ready_marker(cfg.state_dir, os.getpid())
        clear_stop_request(cfg.state_dir, os.getpid())
        lock.release()


def make_pairing(cfg: Config):
    """Pairing mode (pairing.py): the owner is saved into `.env` in place; new codes are printed
    in this pane and shown as a Herdr notification (never logged)."""
    from .pairing import Pairing, code_banner
    from .plugin import HerdrCli, PluginError
    from .setup_cmd import update_env_file

    def persist(user: str) -> None:
        update_env_file(cfg.env_file, {"SLACK_OWNER_USER_ID": user})

    def announce(code: str, reason: str) -> None:
        print(code_banner(code, cfg.slash_command, reason), flush=True)
        body = (f"Slack pairing code: {code}" if code
                else "Slack pairing saved, but the bridge could not start; restart it")

        def show() -> None:  # off the Slack handler thread: the CLI may be slow
            try:
                HerdrCli(cfg.herdr_bin, timeout=10).run("notification", "show", "herdr-slackbot", "--body", body,
                                                        "--sound", "request")
            except PluginError as exc:
                log.warning("pairing notification failed: %s", exc)

        threading.Thread(target=show, name="pairing-notify", daemon=True).start()

    return Pairing(cfg.state_dir, persist, announce)


def _close_previous_tab(state_dir: Path, herdr_bin: str) -> None:
    """After acknowledging its start, a new bridge closes the previous bridge's tab if that tab is
    verifiably ours and idle (see plugin.close_previous_tab). Never fatal."""
    from .plugin import HerdrCli, close_previous_tab

    try:
        result = close_previous_tab(state_dir, HerdrCli(herdr_bin, timeout=10), os.environ.get("HERDR_SOCKET_PATH"))
        log.info("%s", result)
        print(f"herdr-slackbot: {result}")
    except Exception:
        log.exception("closing the previous bridge tab failed")


COMMANDS = ("run", "check", "setup", "wizard", "manifest", "launch", "stop", "restart", "status", "open-wizard",
            "marker-check")


def _apply_config_dir(config_dir: str | None, discover: bool) -> Path | None:
    """--config-dir wins; otherwise (for manual runs) look for the plugin config dir."""
    if config_dir:
        os.environ["HERDR_PLUGIN_CONFIG_DIR"] = config_dir
        return Path(config_dir)
    if os.environ.get("HERDR_PLUGIN_CONFIG_DIR"):
        return Path(os.environ["HERDR_PLUGIN_CONFIG_DIR"])
    if not discover:
        return None
    from .plugin import discover_config_dir

    found = discover_config_dir(require_env_file=True)
    if found is not None:
        os.environ["HERDR_PLUGIN_CONFIG_DIR"] = str(found)
    return found


def _safe_stdio() -> None:
    """Consoles use WriteConsoleW (Unicode); pipes (plugin command logs) get UTF-8. Never crash on
    an unencodable character (cp949 consoles vs. emoji status labels)."""
    for stream in (sys.stdout, sys.stderr):
        try:
            if stream.isatty():
                stream.reconfigure(errors="replace")
            else:
                stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass


def main(argv: list[str] | None = None) -> int:
    _safe_stdio()
    if os.environ.get("HERDR_PLUGIN_CONFIG_DIR"):
        from .plugin import strip_verbatim

        os.environ["HERDR_PLUGIN_CONFIG_DIR"] = strip_verbatim(os.environ["HERDR_PLUGIN_CONFIG_DIR"])
    parser = argparse.ArgumentParser(prog="herdr-slackbot")
    parser.add_argument("command", nargs="?", default="run", choices=COMMANDS)
    parser.add_argument("--check", action="store_true", help="same as the `check` command")
    parser.add_argument("--config-dir", help="plugin config dir holding .env (default: discovered)")
    parser.add_argument("--slash-command", help="setup/wizard: slash command when .env has none")
    parser.add_argument("--display-name", help="setup/wizard: bot display name when .env has none")
    parser.add_argument("--username", help="setup/wizard: username used for the defaults")
    parser.add_argument("--notify", action="store_true", help="status: also show a Herdr notification")
    parser.add_argument("--launch-id", help=argparse.SUPPRESS)  # startup reservation token (plugin launch)
    args = parser.parse_args(argv)
    command = "check" if args.check else args.command

    if command in ("setup", "wizard"):
        from .plugin import discover_config_dir

        config_dir = Path(args.config_dir) if args.config_dir else discover_config_dir()
        if config_dir is None:
            print(f"{command}: cannot determine the plugin config dir; pass --config-dir", file=sys.stderr)
            return 2
        if command == "wizard":
            from .wizard import main_wizard

            os.environ["HERDR_PLUGIN_CONFIG_DIR"] = str(config_dir)
            return main_wizard(config_dir, args.username, args.slash_command, args.display_name)
        from .setup_cmd import main_setup

        return main_setup(config_dir, args.username, args.slash_command, args.display_name)
    if command in ("launch", "stop", "restart", "status", "open-wizard"):
        from .plugin import run_plugin_command

        config_dir = _apply_config_dir(args.config_dir, discover=False)
        return run_plugin_command(command, config_dir, ["--notify"] if args.notify else [])

    _apply_config_dir(args.config_dir, discover=True)
    if command == "manifest":
        from .slack_manifest import manifest_for_config, manifest_json

        try:
            cfg = load_config()
        except ConfigError as exc:
            print(f"config error: {exc}", file=sys.stderr)
            return 2
        sys.stdout.write(manifest_json(manifest_for_config(cfg)))
        return 0
    if command == "marker-check":
        try:
            cfg = load_config()
        except ConfigError as exc:
            print(f"config error: {exc}", file=sys.stderr)
            return 2
        missing = cfg.missing_slack_settings() + (["SLACK_OWNER_USER_ID"] if cfg.needs_pairing else [])
        if missing:
            print(f"missing settings in {cfg.env_file}: {', '.join(missing)}", file=sys.stderr)
            return 2
        from slack_sdk import WebClient

        from .live_check import marker_check
        from .slack_transport import WebClientTransport

        return marker_check(WebClientTransport(WebClient(token=cfg.slack_bot_token)), cfg.slack_owner_user_id)
    if command == "check":
        return check()
    try:
        return run(args.launch_id)
    finally:
        if args.launch_id:  # exited (or failed) before/after acknowledging: never leave it reserved
            from .config import resolve_config_dir
            from .plugin import PluginError, ack_launch, lifecycle_lock, state_dir_for

            state_dir = state_dir_for(resolve_config_dir())
            try:
                with lifecycle_lock(state_dir):
                    if ack_launch(state_dir, args.launch_id):  # e.g. exited early: missing tokens
                        _close_previous_tab(state_dir, os.environ.get("HERDR_BIN") or "herdr")
            except PluginError:
                ack_launch(state_dir, args.launch_id)


if __name__ == "__main__":
    sys.exit(main())
