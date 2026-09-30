"""Herdr plugin runtime (D12): start/stop/restart/status of the bridge.

The plugin's startup hook and actions run `python -m herdr_slackbot <launch|restart|stop|status>`
(through `scripts/herdr-slackbot.ps1`). The bridge itself runs in a pane of the dedicated
workspace `herdr-slack` (BRIDGE_WORKSPACE), typed into that pane's shell with `herdr pane run`,
so its output and any startup error stay visible there and a failed start leaves a plain
shell behind instead of a crash loop.

Safety rules (docs/review/M3.md, M3-recheck.md):
- One mutex: `STATE_DIR/lifecycle.lock` (OS file lock) is held across the whole check -> act
  sequence of launch, stop and restart, *and* by the child bridge for its handoff (check its
  reservation -> take the instance lock -> write its runtime record -> acknowledge). Under the
  mutex the state is therefore always exactly one of: stopped, starting (active reservation in
  `launch.json`), running (instance lock held).
- A launch writes the reservation before typing. It is released by the child's ack, by a
  *definite* `pane run` refusal (Herdr error code in DEFINITE_REFUSALS, or the CLI could not be
  started), by a stop (cancel; any outstanding token, expired or not), or after RESERVATION_TTL.
  Any other outcome of a mutating CLI call (timeout, unrecognized nonzero exit such as a lost
  response) is uncertain and keeps it. The child is admitted only with the current, active,
  unexpired, uncancelled token.
- Launch never types into an existing pane: it always opens a fresh tab (`--no-focus`) in the
  bridge workspace. Once the new bridge has acknowledged, it closes the previous bridge tab only
  if that tab is verifiably ours and idle (same server, recorded tab/pane/terminal id, single
  pane, no agent, shell without child processes); otherwise the old tab is left alone.
- Stop never types into a terminal: it writes `stop.request` addressed to the verified bridge
  process (pid + creation time from `bridge.runtime.json`, lock holder), which the bridge polls
  and honours. A pid is terminated only if it is still that process.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Sequence

from . import procinfo
from .config import PLUGIN_ID, REPO_ROOT, Config, ConfigError, load_config
from .pairing import STATE_ACTIVATING, STATE_FAILED, read_pairing, read_pairing_record
from .state import InstanceLock, _lock_file, _unlock_file, atomic_write_text

BRIDGE_MODULE = "herdr_slackbot"
LOCK_WAIT = 15.0  # seconds the bridge may take to honour a stop request before it is terminated
LIFECYCLE_WAIT = 60.0  # seconds to wait for another launch/stop/restart to finish
RESERVATION_TTL = 60.0  # seconds a launch reservation blocks other launches without an ack
RUNTIME_FILE = "bridge.runtime.json"
LAUNCH_FILE = "launch.json"
LIFECYCLE_LOCK = "lifecycle.lock"
STOP_FILE = "stop.request"
READY_FILE = "bridge.ready.json"  # written once owner mode fully started (DM, notifier, subscriptions, Slack)

# Herdr error codes that prove a request was refused (nothing happened). Any other failure of a
# mutating CLI call is treated as uncertain.
DEFINITE_REFUSALS = frozenset({
    "pane_not_found", "tab_not_found", "workspace_not_found", "agent_not_found", "server_not_running",
    "invalid_params", "invalid_request", "invalid_argument", "unknown_method", "not_found",
})


class PluginError(Exception):
    pass


class PluginUncertain(PluginError):
    """The command may have been carried out (e.g. the CLI timed out after sending it)."""


def refusal_code(text: str) -> str | None:
    """Herdr's `{"error": {"code": ...}}` code from CLI output, if any."""
    for line in reversed((text or "").splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                err = (json.loads(line) or {}).get("error") or {}
            except ValueError:
                continue
            if isinstance(err, dict) and err.get("code"):
                return str(err["code"])
    return None


class HerdrCli:
    """Runs the `herdr` CLI (plugin commands get HERDR_BIN_PATH from Herdr). `socket` routes
    the commands to a specific Herdr server (the CLI honors HERDR_SOCKET_PATH)."""

    def __init__(self, herdr_bin: str | None = None, timeout: float = 20.0, socket: str | None = None):
        self.bin = herdr_bin or os.environ.get("HERDR_BIN_PATH") or "herdr"
        self.timeout = timeout
        self.socket = socket

    def for_socket(self, socket: str | None) -> "HerdrCli":
        return HerdrCli(self.bin, self.timeout, socket)

    def run(self, *args: str, mutating: bool = False) -> str:
        """Run the CLI. For `mutating` calls, a failure is a plain PluginError only when Herdr
        definitely refused the request; anything else (timeout, lost response...) is uncertain."""
        argv = [self.bin, *args]
        env = None
        if self.socket:
            env = dict(os.environ, HERDR_SOCKET_PATH=self.socket)
        try:
            proc = subprocess.run(argv, capture_output=True, timeout=self.timeout, env=env,
                                  creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except subprocess.TimeoutExpired as exc:
            raise PluginUncertain(f"{' '.join(argv[1:3])} timed out; outcome unknown") from exc
        except OSError as exc:  # the CLI could not even be started
            raise PluginError(f"{' '.join(argv[:3])} failed: {exc}") from exc
        out = proc.stdout.decode("utf-8", "replace").strip()
        if proc.returncode != 0:
            err = proc.stderr.decode("utf-8", "replace").strip() or out
            message = f"{' '.join(argv[1:3])}: {err or f'exit status {proc.returncode}'}"
            if mutating and refusal_code(err) not in DEFINITE_REFUSALS:
                raise PluginUncertain(message + " (outcome unknown)")
            raise PluginError(message)
        return out

    def json(self, *args: str) -> dict:
        out = self.run(*args)
        try:
            data = json.loads(out)
        except ValueError as exc:
            raise PluginError(f"{' '.join(args[:2])}: non-JSON output") from exc
        if isinstance(data, dict) and data.get("error"):
            err = data["error"]
            raise PluginError(f"{' '.join(args[:2])}: {err.get('code')}: {err.get('message', '')}")
        return data.get("result", data)


# --- config dir discovery ---------------------------------------------------------

def default_config_dir(env: dict | None = None) -> Path | None:
    """Herdr's per-plugin config dir layout: %APPDATA%\\herdr\\plugins\\config\\<id>."""
    env = os.environ if env is None else env
    appdata = env.get("APPDATA")
    return Path(appdata) / "herdr" / "plugins" / "config" / PLUGIN_ID if appdata else None


def strip_verbatim(path: str) -> str:
    """Herdr reports plugin paths as `\\\\?\\C:\\...`; drop the verbatim prefix."""
    return path[4:] if path.startswith("\\\\?\\") else path


def discover_config_dir(cli: HerdrCli | None = None, env: dict | None = None,
                        require_env_file: bool = False) -> Path | None:
    """HERDR_PLUGIN_CONFIG_DIR > `herdr plugin config-dir herdr-slackbot` > the default layout.

    With `require_env_file`, the last two only count when a `.env` already exists there
    (so a plain checkout keeps using the repo root).
    """
    env = os.environ if env is None else env
    if env.get("HERDR_PLUGIN_CONFIG_DIR"):
        return Path(env["HERDR_PLUGIN_CONFIG_DIR"])
    candidates: list[Path] = []
    try:
        out = (cli or HerdrCli(timeout=5)).run("plugin", "config-dir", PLUGIN_ID)
        line = out.splitlines()[-1].strip() if out else ""
        if line.startswith("{"):
            data = json.loads(line)
            line = (data.get("result") or data).get("config_dir") or (data.get("result") or data).get("path") or ""
        if line:
            candidates.append(Path(strip_verbatim(line)))
    except (PluginError, ValueError, AttributeError):
        pass
    fallback = default_config_dir(env)
    if fallback is not None:
        candidates.append(fallback)
    for path in candidates:
        if not require_env_file or (path / ".env").is_file():
            return path
    return None


# --- shell command building ------------------------------------------------------

def shell_kind(process_name: str) -> str:
    name = Path(process_name or "").name.lower()
    if name.endswith(".exe"):
        name = name[:-4]
    return name


def _ps_quote(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def _sh_quote(s: str) -> str:
    return "'" + s.replace("'", "'\\''") + "'"


def module_command(shell: str, python_exe: str, plugin_root: str, config_dir: str, subcommand: str,
                   extra: str = "") -> str:
    """A `python -m herdr_slackbot <subcommand> --config-dir ...` line for `shell`. Unknown shells
    get PowerShell syntax (Herdr's Windows default). `extra` must need no quoting."""
    extra = f" {extra}" if extra else ""
    if shell == "cmd":
        return (f'cd /d "{plugin_root}" && "{python_exe}" -m {BRIDGE_MODULE} {subcommand} '
                f'--config-dir "{config_dir}"{extra}')
    if shell in ("bash", "sh", "zsh", "fish"):
        fwd = lambda p: p.replace("\\", "/")  # noqa: E731
        return (f"cd {_sh_quote(fwd(plugin_root))} && {_sh_quote(fwd(python_exe))} -m {BRIDGE_MODULE} {subcommand} "
                f"--config-dir {_sh_quote(fwd(config_dir))}{extra}")
    return (f"Set-Location -LiteralPath {_ps_quote(plugin_root)}; & {_ps_quote(python_exe)} -m {BRIDGE_MODULE} "
            f"{subcommand} --config-dir {_ps_quote(config_dir)}{extra}")


def bridge_command(shell: str, python_exe: str, plugin_root: str, config_dir: str,
                   launch_id: str | None = None) -> str:
    """The line typed into the bridge pane's shell."""
    return module_command(shell, python_exe, plugin_root, config_dir, "run",
                          f"--launch-id {launch_id}" if launch_id else "")  # hex token: needs no quoting


SHELL_KINDS = {"powershell", "pwsh", "cmd", "bash", "sh", "zsh", "fish", "nu"}


def same_socket(a: str | None, b: str | None) -> bool:
    return bool(a) and bool(b) and os.path.normcase(os.path.normpath(a)) == os.path.normcase(os.path.normpath(b))


def state_dir_for(config_dir: Path) -> Path:
    """STATE_DIR as the launcher computes it (also when .env is broken)."""
    try:
        return load_config(config_dir=Path(config_dir)).state_dir
    except ConfigError:
        return Path(config_dir) / "state"


def _read_json(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, json.dumps(data, indent=1))


# --- written by the running bridge ---------------------------------------------------

def write_runtime_record(state_dir: Path, pane_info: dict | None, env: dict | None = None) -> dict:
    """Called by the bridge once it holds the instance lock: who and where it is."""
    env = os.environ if env is None else env
    record = {
        "pid": os.getpid(),
        "start": procinfo.current_start_time(),
        "socket": env.get("HERDR_SOCKET_PATH") or "",
        "pane_id": (pane_info or {}).get("pane_id") or env.get("HERDR_PANE_ID") or "",
        "terminal_id": (pane_info or {}).get("terminal_id") or "",
        "at": time.time(),
    }
    _write_json(Path(state_dir) / RUNTIME_FILE, record)
    return record


def read_runtime_record(state_dir: Path) -> dict | None:
    return _read_json(Path(state_dir) / RUNTIME_FILE)


def write_ready_marker(state_dir: Path, owner: str) -> None:
    """Bridge side: owner mode is really serving `owner` (not just configured)."""
    _write_json(Path(state_dir) / READY_FILE, {"pid": os.getpid(), "start": procinfo.current_start_time(),
                                               "owner": owner, "at": time.time()})


def clear_ready_marker(state_dir: Path, pid: int | None = None) -> None:
    path = Path(state_dir) / READY_FILE
    if pid is not None and (_read_json(path) or {}).get("pid") != pid:
        return
    try:
        path.unlink()
    except OSError:
        pass


def verified_ready(state_dir: Path, start_time: Callable[[int], int | None] = procinfo.process_start_time) -> dict | None:
    """The ready marker, if the process that wrote it still holds the bridge lock and is the same
    process (creation time). A marker left behind by a crashed bridge does not count."""
    data = _read_json(Path(state_dir) / READY_FILE)
    if not data or not data.get("owner"):
        return None
    lock = InstanceLock(state_dir)
    pid = data.get("pid")
    if not lock.is_held() or lock.read_pid() != pid:
        return None
    if data.get("start") is None or start_time(pid) != data.get("start"):
        return None
    return data


@contextmanager
def lifecycle_lock(state_dir: Path, timeout: float = LIFECYCLE_WAIT, clock: Callable[[], float] = time.time,
                   sleep: Callable[[float], None] = time.sleep):
    """The launch/stop/handoff mutex (cross-process OS file lock)."""
    state_dir = Path(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    fh = open(state_dir / LIFECYCLE_LOCK, "a+b")
    try:
        deadline = clock() + timeout
        while True:
            try:
                _lock_file(fh)
                break
            except OSError:
                if clock() >= deadline:
                    raise PluginError("another start/stop of the bridge is still running")
                sleep(0.2)
        try:
            yield
        finally:
            _unlock_file(fh)
    finally:
        fh.close()


def launch_admitted(state_dir: Path, launch_id: str, now: float | None = None) -> bool:
    """Child side, under the lifecycle lock: is `launch_id` the current start, still active,
    not cancelled and not expired? (An expired start may already have been superseded.)"""
    data = _read_json(Path(state_dir) / LAUNCH_FILE)
    if not data or data.get("launch_id") != launch_id or data.get("cancelled") or not data.get("active"):
        return False
    now = time.time() if now is None else now
    return now - float(data.get("at") or 0) <= RESERVATION_TTL


def close_previous_tab(state_dir: Path, cli: "HerdrCli", socket: str | None,
                       children: Callable[[int], list | None] = procinfo.busy_children) -> str:
    """Called by a newly admitted bridge: close the tab of the previous bridge if (and only if) it
    is verifiably ours and idle. Returns what happened (for the log)."""
    prev = (_read_json(Path(state_dir) / LAUNCH_FILE) or {}).get("previous") or {}
    tab_id, pane_id, terminal = prev.get("tab_id"), prev.get("pane_id"), prev.get("terminal_id")
    if not (tab_id and pane_id and terminal):
        return "no previous bridge tab recorded"
    if not same_socket(prev.get("socket"), socket):
        return "previous bridge tab is on another Herdr server; left alone"
    try:
        pane = cli.json("pane", "get", pane_id).get("pane") or {}
    except PluginError:
        return "previous bridge pane is gone"
    if pane.get("terminal_id") != terminal or pane.get("tab_id") != tab_id:
        return "previous bridge pane now hosts something else; left alone"
    if pane.get("agent"):
        return "previous bridge pane hosts an agent; left alone"
    try:
        panes = cli.json("pane", "list", "--workspace", pane.get("workspace_id") or "").get("panes") or []
        info = cli.json("pane", "process-info", "--pane", pane_id).get("process_info") or {}
    except PluginError:
        return "previous bridge tab could not be inspected; left alone"
    if [p.get("pane_id") for p in panes if p.get("tab_id") == tab_id] != [pane_id]:
        return "previous bridge tab has other panes; left alone"
    shell_pid = info.get("shell_pid")
    if not shell_pid or children(int(shell_pid)) != []:
        return "previous bridge shell is busy; left alone"
    try:
        cli.run("tab", "close", tab_id, mutating=True)
    except PluginError as exc:
        return f"closing the previous bridge tab failed: {exc}"
    return f"closed the previous bridge tab {tab_id}"


def request_stop(state_dir: Path, rec: dict) -> None:
    _write_json(Path(state_dir) / STOP_FILE, {"pid": rec.get("pid"), "start": rec.get("start"), "at": time.time()})


def stop_requested(state_dir: Path, pid: int, start: int | None) -> bool:
    """Bridge side: is there a stop request addressed to exactly this process?"""
    data = _read_json(Path(state_dir) / STOP_FILE)
    return bool(data) and data.get("pid") == pid and data.get("start") == start


def clear_stop_request(state_dir: Path, pid: int | None = None) -> None:
    path = Path(state_dir) / STOP_FILE
    if pid is not None and (_read_json(path) or {}).get("pid") != pid:
        return
    try:
        path.unlink()
    except OSError:
        pass


def ack_launch(state_dir: Path, launch_id: str | None) -> bool:
    """The child's side of the startup reservation: clear it (only if it is ours and active).
    Returns True if this call acknowledged our own active start."""
    if not launch_id:
        return False
    path = Path(state_dir) / LAUNCH_FILE
    data = _read_json(path)
    if data and data.get("launch_id") == launch_id and data.get("active"):
        data["active"] = False
        try:
            _write_json(path, data)
        except OSError:
            return False
        return True
    return False


# --- plugin operations -------------------------------------------------------------

class PluginOps:
    def __init__(self, cli: HerdrCli, config_dir: Path, plugin_root: Path | None = None,
                 python_exe: str | None = None, out: Callable[[str], None] = print,
                 sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.time,
                 kill: Callable[[int], None] | None = None, env: dict | None = None,
                 children: Callable[[int], list | None] = procinfo.busy_children,
                 start_time: Callable[[int], int | None] = procinfo.process_start_time):
        self.cli = cli
        self.config_dir = Path(config_dir)
        self.plugin_root = Path(plugin_root or REPO_ROOT)
        self.python_exe = python_exe or sys.executable
        self.out = out
        self.sleep = sleep
        self.clock = clock
        self.kill = kill or _terminate
        self.env = os.environ if env is None else env
        self.children = children
        self.start_time = start_time
        try:
            self.cfg: Config | None = load_config(config_dir=self.config_dir)
            self.cfg_error: str | None = None
        except ConfigError as exc:  # the bridge itself reports it in its pane
            self.cfg, self.cfg_error = None, str(exc)
        self.state_dir = self.cfg.state_dir if self.cfg else self.config_dir / "state"
        self.workspace_label = self.cfg.bridge_workspace if self.cfg else "herdr-slack"
        self.lock = InstanceLock(self.state_dir)
        self.socket = self.env.get("HERDR_SOCKET_PATH") or ""

    # -- cross-process lifecycle lock
    @contextmanager
    def lifecycle(self):
        with lifecycle_lock(self.state_dir, LIFECYCLE_WAIT, self.clock, self.sleep):
            yield

    # -- lookups
    def running_pid(self) -> int | None | bool:
        """pid of the running bridge, True if running with an unknown pid, None if not running."""
        if not self.lock.is_held():
            return None
        return self.lock.read_pid() or True

    def find_workspace(self) -> dict | None:
        for ws in self.cli.json("workspace", "list").get("workspaces", []):
            if ws.get("label") == self.workspace_label:
                return ws
        return None

    def pane_info(self, pane_id: str, cli: HerdrCli | None = None) -> dict | None:
        try:
            return (cli or self.cli).json("pane", "get", pane_id).get("pane")
        except PluginError:
            return None

    def process_info(self, pane_id: str) -> dict:
        try:
            return self.cli.json("pane", "process-info", "--pane", pane_id).get("process_info", {}) or {}
        except PluginError:
            return {}

    def pane_shell(self, pane_id: str) -> str:
        fg = (self.process_info(pane_id).get("foreground_processes") or [{}])[0]
        return shell_kind(fg.get("name") or "")

    def reservation(self) -> dict | None:
        """The active startup reservation, if it is still valid."""
        data = _read_json(self.state_dir / LAUNCH_FILE)
        if not data or not data.get("active"):
            return None
        if self.clock() - float(data.get("at") or 0) > RESERVATION_TTL:
            return None
        return data

    def verified_bridge(self) -> tuple[dict | None, str]:
        """(runtime record, problem). The record counts only if its pid holds the lock and is
        still the same process (creation time)."""
        rec = read_runtime_record(self.state_dir)
        if not rec:
            return None, "no runtime record"
        pid = rec.get("pid")
        if self.lock.read_pid() != pid:
            return None, "runtime record does not match the lock holder"
        if rec.get("start") is None or self.start_time(pid) != rec.get("start"):
            return None, f"pid {pid} is not the recorded bridge process"
        return rec, ""

    def verified_pane(self, rec: dict) -> tuple[HerdrCli, str] | None:
        """(cli routed to the bridge's server, pane id) if the recorded pane still hosts it."""
        if not rec.get("pane_id") or not rec.get("terminal_id") or not rec.get("socket"):
            return None
        owner = self.cli.for_socket(rec["socket"])
        pane = self.pane_info(rec["pane_id"], owner)
        if not pane or pane.get("terminal_id") != rec["terminal_id"]:
            return None
        return owner, rec["pane_id"]

    # -- operations
    def launch(self) -> int:
        with self.lifecycle():
            return self._launch()

    def stop(self) -> int:
        with self.lifecycle():
            return self._stop()

    def restart(self) -> int:
        with self.lifecycle():
            code = self._stop()
            return code if code != 0 else self._launch()

    def _launch(self) -> int:
        pid = self.running_pid()
        if pid:
            self.out(f"bridge already running (pid {pid if pid is not True else '?'})")
            return 0
        pending = self.reservation()
        if pending:
            self.out(f"a bridge start is already in progress (pane {pending.get('pane_id')}); not starting another")
            return 0
        previous = _read_json(self.state_dir / LAUNCH_FILE) or {}
        pane_id = self._fresh_pane()
        pane = self.pane_info(pane_id) or {}
        launch_id = uuid.uuid4().hex
        _write_json(self.state_dir / LAUNCH_FILE, {
            "launch_id": launch_id, "active": True, "at": self.clock(), "socket": self.socket,
            "pane_id": pane_id, "tab_id": pane.get("tab_id") or "", "terminal_id": pane.get("terminal_id") or "",
            # the bridge before this one: its tab may be closed once the new bridge has acked
            "previous": {k: previous.get(k) for k in ("socket", "tab_id", "pane_id", "terminal_id")},
        })
        command = bridge_command(self.pane_shell(pane_id), self.python_exe, str(self.plugin_root),
                                 str(self.config_dir), launch_id)
        try:
            self.cli.run("pane", "run", pane_id, command, mutating=True)
        except PluginUncertain:
            # The command may have been typed: keep the reservation until the child reports in
            # (ack / runtime lock), a stop cancels it, or it expires.
            self.out(f"starting in pane {pane_id}: outcome unknown; the start stays reserved for "
                     f"{int(RESERVATION_TTL)}s")
            raise
        except PluginError:
            ack_launch(self.state_dir, launch_id)  # Herdr refused it: nothing was typed
            raise
        self.out(f"bridge started in {self.workspace_label} pane {pane_id}")
        return 0

    def _fresh_pane(self) -> str:
        """A pane nobody has typed into: a new workspace's root pane or a new tab (never reused)."""
        ws = self.find_workspace()
        if ws is None:
            created = self.cli.json("workspace", "create", "--label", self.workspace_label,
                                    "--cwd", str(self.plugin_root), "--no-focus")
            ws = created.get("workspace") or {}
            pane_id = (created.get("root_pane") or {}).get("pane_id")
            self.out(f"created workspace {self.workspace_label} ({ws.get('workspace_id', '?')})")
            if pane_id:
                return pane_id
        ws_id = ws.get("workspace_id")
        if not ws_id:
            raise PluginError("could not determine the bridge workspace id")
        tab = self.cli.json("tab", "create", "--workspace", ws_id, "--cwd", str(self.plugin_root),
                            "--label", "bridge", "--no-focus")
        pane_id = (tab.get("root_pane") or {}).get("pane_id")
        if not pane_id:
            raise PluginError("could not create a pane for the bridge")
        return pane_id

    def _stop(self) -> int:
        if not self.running_pid():
            data = _read_json(self.state_dir / LAUNCH_FILE) or {}
            if data.get("active") and not data.get("cancelled"):
                # Starting (or a start that never reported in, expired or not): cancel under the
                # mutex; the child checks this before it takes the instance lock.
                _write_json(self.state_dir / LAUNCH_FILE, dict(data, active=False, cancelled=True))
                self.out(f"cancelled a pending bridge start (pane {data.get('pane_id')})")
                return 0
            self.out("bridge is not running")
            return 0
        rec, problem = self.verified_bridge()
        if rec is None:
            self.out(f"cannot verify the running bridge ({problem}); not stopping it. "
                     "Stop it with Ctrl+C in its pane.")
            return 1
        request_stop(self.state_dir, rec)  # addressed to this pid + creation time only
        if self._wait_released(LOCK_WAIT):
            clear_stop_request(self.state_dir, rec["pid"])
            self.out("bridge stopped")
            return 0
        rec, problem = self.verified_bridge()  # the process may have exited / been replaced meanwhile
        if rec is None:
            if not self.running_pid():
                self.out("bridge stopped")
                return 0
            self.out(f"not terminating: {problem}")
            return 1
        self.out(f"bridge did not stop within {int(LOCK_WAIT)}s; terminating pid {rec['pid']}")
        self.kill(rec["pid"])
        if self._wait_released(5.0):
            clear_stop_request(self.state_dir, rec["pid"])
            self.out("bridge stopped")
            return 0
        self.out("bridge did not stop")
        return 1

    def open_wizard(self) -> int:
        """The `setup` action. Plugin actions run headless (stdin redirected; checked on Herdr 0.8.2),
        so open a focused tab in the current workspace and type the wizard command into its shell."""
        tab = self.cli.json("tab", "create", "--cwd", str(self.plugin_root), "--label", "Slack setup", "--focus")
        pane_id = (tab.get("root_pane") or {}).get("pane_id")
        if not pane_id:
            raise PluginError("could not create a tab for the setup wizard")
        command = module_command(self.pane_shell(pane_id), self.python_exe, str(self.plugin_root),
                                 str(self.config_dir), "wizard")
        self.cli.run("pane", "run", pane_id, command, mutating=True)
        self.out(f"setup wizard opened in a new tab (pane {pane_id})")
        return 0

    def status_lines(self) -> list[str]:
        pid = self.running_pid()
        if pid:
            rec, _ = self.verified_bridge()
            where = ""
            if rec is not None:
                if not same_socket(rec.get("socket"), self.socket):
                    where = f", on another Herdr server ({rec.get('socket') or '?'})"
                elif self.verified_pane(rec) is not None:
                    where = f", pane {rec['pane_id']}"
                else:
                    where = ", pane unverified"
            state = f"running (pid {pid if pid is not True else '?'}{where})"
        elif self.reservation():
            state = "starting"
        else:
            state = "stopped"
        lines = [f"bridge: {state}"]
        lines.append(f"config: {self.config_dir / '.env'}"
                     + ("" if (self.config_dir / ".env").is_file() else " (missing — run setup)"))
        if self.cfg_error:
            lines.append(f"config error: {self.cfg_error}")
        elif self.cfg is not None:
            missing = self.cfg.missing_slack_settings()
            pairing_state = (read_pairing_record(self.state_dir) or {}).get("state") if pid else None
            if missing:
                slack = "missing " + ", ".join(missing)
            elif not self.cfg.needs_pairing or pairing_state in (STATE_ACTIVATING, STATE_FAILED):
                slack = self._owner_readiness(pid, pairing_state)
            elif pid and read_pairing(self.state_dir):
                slack = f"waiting for pairing (code in {self.workspace_label} pane)"
            elif pid:
                slack = "not paired yet (see the bridge pane)"
            else:
                slack = "tokens set, not paired yet (start the bridge to get a pairing code)"
            lines.append(f"slack: {slack}")
            lines.append(f"command: {self.cfg.slash_command}")
        lines.append(f"log: {self.state_dir / 'bridge.log'}")
        return lines

    def _owner_readiness(self, pid, pairing_state: str | None) -> str:
        """An owner is saved: say whether the bridge really serves them (ready marker of the verified
        running bridge), not just that the key is there."""
        ready = verified_ready(self.state_dir, self.start_time)
        if ready:
            return f"ready (owner {ready['owner']})"
        if pid and pairing_state == STATE_FAILED:
            return "paired, but owner mode failed to start (see the bridge pane); restart the bridge"
        if pid and pairing_state == STATE_ACTIVATING:
            return "paired; owner mode is starting"
        if pid:
            return "configured, but the bridge is not ready (still starting, or it failed: see the bridge pane)"
        return "configured; the bridge is not running"

    def status(self, notify: bool = False) -> int:
        lines = self.status_lines()
        for line in lines:
            self.out(line)
        if notify:
            body = " · ".join(line for line in lines if not line.startswith(("config:", "log:")))
            try:
                self.cli.run("notification", "show", "herdr-slackbot", "--body", body)
            except PluginError as exc:
                self.out(f"notification failed: {exc}")
        return 0

    def _wait_released(self, timeout: float) -> bool:
        deadline = self.clock() + timeout
        while self.clock() < deadline:
            if not self.lock.is_held():
                return True
            self.sleep(0.25)
        return not self.lock.is_held()


def _terminate(pid: int) -> None:
    import signal

    try:
        os.kill(pid, signal.SIGTERM)  # TerminateProcess on Windows
    except OSError:
        pass


def plugin_root_from_env(env: dict | None = None) -> Path:
    env = os.environ if env is None else env
    raw = env.get("HERDR_PLUGIN_ROOT")
    return Path(strip_verbatim(raw)) if raw else REPO_ROOT


def run_plugin_command(command: str, config_dir: Path | None, argv_rest: Sequence[str] = ()) -> int:
    cli = HerdrCli()
    config_dir = config_dir or discover_config_dir(cli) or REPO_ROOT
    ops = PluginOps(cli, config_dir, plugin_root_from_env())
    try:
        if command == "launch":
            return ops.launch()
        if command == "stop":
            return ops.stop()
        if command == "restart":
            return ops.restart()
        if command == "status":
            return ops.status(notify="--notify" in argv_rest)
        if command == "open-wizard":
            return ops.open_wizard()
    except PluginError as exc:
        print(f"herdr-slackbot {command}: {exc}", file=sys.stderr)
        return 1
    raise ValueError(command)
