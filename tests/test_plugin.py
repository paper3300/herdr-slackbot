"""Plugin packaging: manifest, launcher (launch/stop/restart/status), config dir discovery."""

import itertools
import json
import os
import threading
import time
import tomllib
from pathlib import Path

import pytest

from herdr_slackbot import __main__ as main_mod
from herdr_slackbot import plugin as P
from herdr_slackbot.config import PLUGIN_ID, REPO_ROOT
from herdr_slackbot.state import InstanceLock

PS = "powershell.exe"
MY_START = 111  # fake creation time of this (test) process, which plays the bridge


class Server:
    """One fake Herdr server (identified by its socket)."""

    _pids = itertools.count(5000)

    def __init__(self, socket):
        self.socket = socket
        self.workspaces = []
        self.panes = {}  # pane_id -> {workspace_id, terminal_id, shell_pid, fg, agent}
        self._n = itertools.count(1)
        self.children = {}  # shell pid -> busy child list

    def add_workspace(self, ws_id, label="herdr-slack"):
        self.workspaces.append({"workspace_id": ws_id, "label": label})

    def add_pane(self, ws_id, pane_id=None, fg=PS, agent=None, busy=False, terminal=None, screen=None):
        pane_id = pane_id or f"{ws_id}:p{next(self._n)}"
        pid = next(self._pids)
        if screen is None:
            screen = {"powershell.exe": "output\nPS D:\\Plug Root> ", "cmd.exe": "output\nD:\\Plug Root>"}.get(fg, "")
        self.panes[pane_id] = {"workspace_id": ws_id, "terminal_id": terminal or f"term-{self.socket}-{pane_id}",
                               "shell_pid": pid, "fg": fg, "agent": agent, "screen": screen,
                               "tab_id": f"{ws_id}:t{pane_id.rsplit(':p', 1)[-1]}"}
        self.children[pid] = [(pid + 1, "claude.exe")] if busy else []
        return pane_id


class FakeCli:
    """Answers the herdr CLI calls PluginOps makes; records (socket, *args) for every call."""

    def __init__(self, servers=None, socket="A", fail=(), _shared=None):
        self.servers = servers if servers is not None else {"A": Server("A")}
        self.socket = socket
        self.fail = set(fail)
        self.shared = _shared if _shared is not None else {"calls": [], "on_send_keys": None, "on_run": None}

    @property
    def calls(self):
        return self.shared["calls"]

    def for_socket(self, socket):
        return FakeCli(self.servers, socket, self.fail, self.shared)

    def _server(self):
        if self.socket not in self.servers:
            raise P.PluginError("server_not_running")
        return self.servers[self.socket]

    def run(self, *args, mutating=False):
        self.calls.append((self.socket, *args))
        if args[:2] in self.fail:
            raise P.PluginError(f"{args[:2]} failed")
        srv = self._server()
        if args[:2] == ("pane", "send-keys") and self.shared["on_send_keys"]:
            self.shared["on_send_keys"]()
        if args[:2] == ("pane", "run") and self.shared["on_run"]:
            self.shared["on_run"](args)
        if args[:2] == ("tab", "close"):
            for pid in [k for k, v in srv.panes.items() if v.get("tab_id") == args[2]]:
                srv.panes.pop(pid)
        if args[:2] == ("pane", "read"):
            pane = srv.panes.get(args[2])
            if pane is None:
                raise P.PluginError("pane_not_found")
            return pane["screen"]
        return ""

    def json(self, *args):
        self.calls.append((self.socket, *args))
        if args[:2] in self.fail:
            raise P.PluginError(f"{args[:2]} failed")
        srv = self._server()
        if args[:2] == ("workspace", "list"):
            return {"workspaces": list(srv.workspaces)}
        if args[:2] == ("workspace", "create"):
            ws_id = f"wN{len(srv.workspaces)}"
            srv.add_workspace(ws_id, args[args.index("--label") + 1])
            pane = srv.add_pane(ws_id)
            return {"type": "workspace_created", "workspace": {"workspace_id": ws_id}, "root_pane": {"pane_id": pane}}
        if args[:2] == ("pane", "process-info"):
            pane = srv.panes.get(args[args.index("--pane") + 1])
            if pane is None:
                raise P.PluginError("pane_not_found")
            fg_pid = pane["shell_pid"] if P.shell_kind(pane["fg"]) in P.SHELL_KINDS else pane["shell_pid"] + 7
            return {"process_info": {"shell_pid": pane["shell_pid"],
                                     "foreground_processes": [{"name": pane["fg"], "pid": fg_pid}]}}
        if args[:2] == ("pane", "get"):
            pane = srv.panes.get(args[2])
            if pane is None:
                raise P.PluginError("pane_not_found")
            return {"pane": {"pane_id": args[2], **{k: v for k, v in pane.items() if v is not None and k != "screen"}}}
        if args[:2] == ("tab", "create"):
            pane = srv.add_pane(args[args.index("--workspace") + 1])
            return {"root_pane": {"pane_id": pane}}
        if args[:2] == ("pane", "list"):
            ws = args[args.index("--workspace") + 1]
            return {"panes": [{"pane_id": k, "tab_id": v.get("tab_id")} for k, v in srv.panes.items()
                              if v["workspace_id"] == ws]}
        raise AssertionError(f"unexpected call {args}")

    def called(self, *prefix, socket=None):
        return [c[1:] for c in self.calls if c[1:1 + len(prefix)] == prefix and (socket is None or c[0] == socket)]


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


@pytest.fixture
def cfg_dir(tmp_path):
    d = tmp_path / "cfg"
    d.mkdir()
    return d


def make_ops(cli, cfg_dir, socket="A", real_time=False, starts=None, **kw):
    clock = Clock()
    out = []
    starts = starts if starts is not None else {os.getpid(): MY_START}

    def children(pid):
        for srv in cli.servers.values():
            if pid in srv.children:
                return srv.children[pid]
        return None

    kw.setdefault("kill", lambda pid: None)
    ops = P.PluginOps(cli, cfg_dir, plugin_root=Path(r"D:\Plug Root"), python_exe=r"D:\Plug Root\.venv\python.exe",
                      out=out.append, sleep=time.sleep if real_time else clock.sleep,
                      clock=time.time if real_time else clock, env={"HERDR_SOCKET_PATH": socket},
                      children=children, start_time=lambda pid: starts.get(pid), **kw)
    return ops, out


def hold_bridge(ops, cli, pane_id, socket="A", start=MY_START):
    """This process plays a running bridge on `socket` in `pane_id`: lock + runtime record."""
    lock = InstanceLock(ops.state_dir).acquire()
    terminal = cli.servers[socket].panes[pane_id]["terminal_id"] if pane_id in cli.servers[socket].panes else "x"
    P._write_json(ops.state_dir / P.RUNTIME_FILE, {"pid": os.getpid(), "start": start, "socket": socket,
                                                    "pane_id": pane_id, "terminal_id": terminal, "at": 0})
    return Held(lock)


class Held:
    """An acquired InstanceLock usable in `with` (releases on exit, never re-acquires)."""

    def __init__(self, lock):
        self.lock = lock

    def release(self):
        self.lock.release()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.lock.release()


def honor_stop(ops, held, start=MY_START):
    """The (test-process) bridge honours a stop request addressed to it, like the real main loop."""
    orig = ops.sleep

    def sleep(seconds):
        if P.stop_requested(ops.state_dir, os.getpid(), start):
            held.release()
        orig(seconds)
    ops.sleep = sleep


def last_launch(ops):
    return json.loads((ops.state_dir / P.LAUNCH_FILE).read_text(encoding="utf-8"))


def bridge_ws(socket="A", servers=None):
    servers = servers or {}
    srv = servers.setdefault(socket, Server(socket))
    srv.add_workspace("wE")
    return servers, srv


# --- manifest ---------------------------------------------------------------------

MANIFEST = tomllib.loads((REPO_ROOT / "herdr-plugin.toml").read_text(encoding="utf-8"))


def test_manifest_identity_and_required_fields():
    assert MANIFEST["id"] == MANIFEST["name"] == PLUGIN_ID
    for key in ("version", "description", "min_herdr_version", "platforms"):
        assert MANIFEST[key]
    assert MANIFEST["platforms"] == ["windows"]


def test_manifest_hooks_and_actions_call_the_wrapper():
    ids = [a["id"] for a in MANIFEST["actions"]]
    assert len(ids) == len(set(ids))
    assert set(ids) == {"setup", "start", "restart", "stop", "status"}
    expected = {"setup": "open-wizard", "start": "launch", "restart": "restart", "stop": "stop",
                "status": "status -Notify"}
    for action in MANIFEST["actions"]:
        script = action["command"][-1]
        assert "herdr-slackbot.ps1') " + expected[action["id"]] + ";" in script
        assert "'herdr-slackbot'" in script  # plugin_list fallback looks up our own id
        assert action["title"] and action["platforms"] == ["windows"]
    (startup,) = MANIFEST["startup"]
    assert "herdr-slackbot.ps1') launch;" in startup["command"][-1]
    (build,) = MANIFEST["build"]
    assert build["command"][-1] == "scripts/setup.ps1"
    assert (REPO_ROOT / "scripts" / "setup.ps1").is_file()
    wrapper = (REPO_ROOT / "scripts" / "herdr-slackbot.ps1").read_text(encoding="utf-8")
    assert 'ValidateSet("launch", "restart", "stop", "status", "open-wizard")' in wrapper
    assert "[switch]$Wizard" in (REPO_ROOT / "scripts" / "setup.ps1").read_text(encoding="utf-8")


# --- helpers ------------------------------------------------------------------------

def test_strip_verbatim_and_shell_kind():
    assert P.strip_verbatim("\\\\?\\D:\\Git\\x") == "D:\\Git\\x"
    assert P.strip_verbatim("D:\\Git\\x") == "D:\\Git\\x"
    assert P.shell_kind(r"C:\WINDOWS\System32\WindowsPowerShell\v1.0\powershell.exe") == "powershell"
    assert P.shell_kind("pwsh.exe") == "pwsh"
    assert P.shell_kind("") == ""


def test_bridge_command_quoting_per_shell():
    root, py, cfg = r"D:\it's here", r"D:\it's here\.venv\Scripts\python.exe", r"C:\cfg dir"
    ps = P.bridge_command("powershell", py, root, cfg)
    assert ps == ("Set-Location -LiteralPath 'D:\\it''s here'; & 'D:\\it''s here\\.venv\\Scripts\\python.exe' "
                  "-m herdr_slackbot run --config-dir 'C:\\cfg dir'")
    assert P.bridge_command("", py, root, cfg) == ps  # unknown shell -> PowerShell (Herdr's default)
    cmd = P.bridge_command("cmd", py, root, cfg)
    assert cmd.startswith('cd /d "D:\\it\'s here" && "D:\\it\'s here\\.venv\\Scripts\\python.exe" -m herdr_slackbot run')
    sh = P.bridge_command("bash", py, root, cfg)
    assert "'D:/it'\\''s here/.venv/Scripts/python.exe' -m herdr_slackbot run --config-dir 'C:/cfg dir'" in sh


def test_discover_config_dir(tmp_path):
    class Cli:
        def __init__(self, out=None):
            self.out = out

        def run(self, *args):
            assert args == ("plugin", "config-dir", PLUGIN_ID)
            if self.out is None:
                raise P.PluginError("plugin_not_found")
            return self.out

    env = {"APPDATA": str(tmp_path)}
    default = tmp_path / "herdr" / "plugins" / "config" / PLUGIN_ID
    assert P.discover_config_dir(Cli(), {"HERDR_PLUGIN_CONFIG_DIR": "X:\\c", **env}) == Path("X:\\c")
    assert P.discover_config_dir(Cli("\\\\?\\Y:\\conf\n"), env) == Path("Y:\\conf")
    assert P.discover_config_dir(Cli(), env) == default
    assert P.discover_config_dir(Cli(), env, require_env_file=True) is None
    default.mkdir(parents=True)
    (default / ".env").write_text("", encoding="utf-8")
    assert P.discover_config_dir(Cli(), env, require_env_file=True) == default
    assert P.discover_config_dir(Cli(), {}) is None


# --- launch: pane selection (M3 review #2) ------------------------------------------------

def test_launch_creates_workspace_without_focus_and_runs_bridge(cfg_dir):
    cli = FakeCli()
    ops, out = make_ops(cli, cfg_dir)
    assert ops.launch() == 0
    (create,) = cli.called("workspace", "create")
    assert "--no-focus" in create and create[create.index("--label") + 1] == "herdr-slack"
    (run,) = cli.called("pane", "run")
    pane = run[2]
    launch = last_launch(ops)
    assert launch["active"] and launch["pane_id"] == pane and launch["socket"] == "A"
    assert launch["terminal_id"] == cli.servers["A"].panes[pane]["terminal_id"]
    assert run[3] == P.bridge_command("powershell", ops.python_exe, str(ops.plugin_root), str(cfg_dir),
                                      launch["launch_id"])
    assert any(line.startswith("created workspace herdr-slack") for line in out)


def _previous_launch(ops, pane_id, terminal, socket="A", tab_id=None):
    P._write_json(ops.state_dir / P.LAUNCH_FILE, {"launch_id": "old", "active": False, "at": 0, "socket": socket,
                                                  "pane_id": pane_id, "terminal_id": terminal, "tab_id": tab_id})


@pytest.mark.parametrize("case", ["idle_prompt", "unfinished_redirect", "read_host", "busy_child", "no_record"])
def test_r2_launch_always_uses_a_fresh_tab(cfg_dir, case):
    servers, srv = bridge_ws()
    screen = {"idle_prompt": "PS D:\\Work> ", "unfinished_redirect": "PS D:\\Work> Get-Process >",
              "read_host": "review-probe: "}.get(case)
    pane = srv.add_pane("wE", busy=case == "busy_child", screen=screen)
    cli = FakeCli(servers)
    ops, _ = make_ops(cli, cfg_dir)
    if case != "no_record":
        _previous_launch(ops, pane, srv.panes[pane]["terminal_id"], tab_id=srv.panes[pane]["tab_id"])
    assert ops.launch() == 0
    (tab,) = cli.called("tab", "create")
    assert "--no-focus" in tab
    (run,) = cli.called("pane", "run")
    assert run[2] != pane  # never typed into an existing pane, even a perfectly idle one
    assert cli.called("pane", "read") == []  # no prompt guessing at all
    launch = last_launch(ops)
    assert launch["pane_id"] == run[2] and launch["tab_id"] == srv.panes[run[2]]["tab_id"]
    if case != "no_record":
        assert launch["previous"]["pane_id"] == pane


def _with_previous(case):
    """Server with a previous bridge tab in `case` condition; returns (servers, srv, pane, state_setup)."""
    servers, srv = bridge_ws()
    pane = srv.add_pane("wE", busy=case == "busy", agent="claude" if case == "agent" else None)
    if case == "split_tab":
        extra = srv.add_pane("wE")
        srv.panes[extra]["tab_id"] = srv.panes[pane]["tab_id"]  # the user split the old tab
    return servers, srv, pane


def _record_previous(ops, srv, pane, socket="A", terminal=None, tab=None):
    P._write_json(ops.state_dir / P.LAUNCH_FILE, {
        "launch_id": "new", "active": False, "at": 0, "socket": socket, "pane_id": "wE:p99",
        "previous": {"socket": socket, "pane_id": pane, "tab_id": tab or srv.panes[pane]["tab_id"],
                     "terminal_id": terminal or srv.panes[pane]["terminal_id"]}})


def test_r2_previous_bridge_tab_is_closed_when_verifiably_ours_and_idle(cfg_dir):
    servers, srv, pane = _with_previous("idle")
    cli = FakeCli(servers)
    ops, _ = make_ops(cli, cfg_dir)
    tab = srv.panes[pane]["tab_id"]
    _record_previous(ops, srv, pane)
    result = P.close_previous_tab(ops.state_dir, cli, "A", children=lambda pid: srv.children.get(pid))
    assert result == f"closed the previous bridge tab {tab}"
    assert cli.called("tab", "close") == [("tab", "close", tab)]


@pytest.mark.parametrize("case", ["busy", "agent", "split_tab", "replaced_terminal", "moved_tab", "other_server",
                                  "gone", "unknown_children"])
def test_r2_previous_bridge_tab_is_left_alone_unless_verified(cfg_dir, case):
    servers, srv, pane = _with_previous(case)
    cli = FakeCli(servers)
    ops, _ = make_ops(cli, cfg_dir)
    _record_previous(ops, srv, pane, socket="B" if case == "other_server" else "A",
                     terminal="term-old" if case == "replaced_terminal" else None,
                     tab="wE:tOTHER" if case == "moved_tab" else None)
    if case == "gone":
        srv.panes.pop(pane)
    children = (lambda pid: None) if case == "unknown_children" else (lambda pid: srv.children.get(pid))
    result = P.close_previous_tab(ops.state_dir, cli, "A", children=children)
    assert cli.called("tab", "close") == [], result


def test_r2_nothing_recorded_means_nothing_closed(cfg_dir):
    cli = FakeCli()
    ops, _ = make_ops(cli, cfg_dir)
    assert P.close_previous_tab(ops.state_dir, cli, "A") == "no previous bridge tab recorded"
    assert cli.calls == []


def test_launch_is_a_noop_while_the_bridge_holds_the_lock(cfg_dir):
    servers, srv = bridge_ws()
    pane = srv.add_pane("wE")
    cli = FakeCli(servers)
    ops, out = make_ops(cli, cfg_dir)
    with hold_bridge(ops, cli, pane):
        assert ops.launch() == 0
    assert not cli.called("pane", "run") and not cli.called("workspace", "create")
    assert out == [f"bridge already running (pid {os.getpid()})"]


def test_launch_honours_bridge_workspace_setting(cfg_dir):
    (cfg_dir / ".env").write_text("BRIDGE_WORKSPACE=slack-bridge\n", encoding="utf-8")
    servers, _ = bridge_ws()
    cli = FakeCli(servers)
    ops, _ = make_ops(cli, cfg_dir)
    ops.launch()
    (create,) = cli.called("workspace", "create")
    assert create[create.index("--label") + 1] == "slack-bridge"


def test_launch_with_broken_config_still_starts_bridge_to_show_the_error(cfg_dir):
    (cfg_dir / ".env").write_text("SLASH_COMMAND=bad name!\n", encoding="utf-8")
    cli = FakeCli()
    ops, _ = make_ops(cli, cfg_dir)
    assert ops.cfg is None and "SLASH_COMMAND" in ops.cfg_error
    assert ops.launch() == 0 and cli.called("pane", "run")


# --- launch: idempotence (M3 review #3) -----------------------------------------------------

def test_launch_reservation_blocks_a_second_start_until_the_child_acks(cfg_dir):
    cli = FakeCli()
    ops, out = make_ops(cli, cfg_dir)
    ops.launch()
    ops.launch()  # the child has not taken the instance lock yet
    assert len(cli.called("pane", "run")) == 1 and len(cli.called("workspace", "create")) == 1
    assert "already in progress" in out[-1]
    assert ops.status_lines()[0] == "bridge: starting"
    P.ack_launch(ops.state_dir, "someone-else")  # only the matching child may release it
    ops.launch()
    assert len(cli.called("pane", "run")) == 1
    P.ack_launch(ops.state_dir, last_launch(ops)["launch_id"])  # child exited (e.g. missing tokens)
    ops.launch()
    runs = cli.called("pane", "run")
    assert len(runs) == 2 and runs[1][2] != runs[0][2]  # always a fresh tab
    assert last_launch(ops)["previous"]["pane_id"] == runs[0][2]


def test_launch_reservation_expires(cfg_dir):
    cli = FakeCli()
    ops, _ = make_ops(cli, cfg_dir)
    ops.launch()
    ops.clock.t += P.RESERVATION_TTL + 1
    ops.launch()
    assert len(cli.called("pane", "run")) == 2


def test_failed_pane_run_releases_the_reservation(cfg_dir):
    cli = FakeCli(fail={("pane", "run")})
    ops, _ = make_ops(cli, cfg_dir)
    with pytest.raises(P.PluginError):
        ops.launch()
    assert ops.reservation() is None


def test_uncertain_pane_run_keeps_the_reservation(cfg_dir):
    cli = FakeCli()

    def accepted_then_lost(args):
        raise P.PluginUncertain("pane run timed out; outcome unknown")
    cli.shared["on_run"] = accepted_then_lost  # recorded (accepted), then the response is lost
    ops, out = make_ops(cli, cfg_dir)
    with pytest.raises(P.PluginUncertain):
        ops.launch()
    assert ops.reservation() is not None and "stays reserved" in out[-1]
    cli.shared["on_run"] = None
    assert ops.launch() == 0  # immediate retry
    assert len(cli.called("pane", "run")) == 1
    ops.clock.t += P.RESERVATION_TTL + 1  # never reported in: the reservation expires
    ops.launch()
    assert len(cli.called("pane", "run")) == 2


class _Proc:
    def __init__(self, code, out=b"", err=b""):
        self.returncode, self.stdout, self.stderr = code, out, err


@pytest.mark.parametrize("err,mutating,uncertain", [
    (b"Error: Custom { kind: Other, error: EmptyResponse }", True, True),  # native lost-response exit 1
    (b"", True, True),
    (b'{"error":{"code":"internal_error","message":"x"},"id":"cli:request"}', True, True),
    (b'{"error":{"code":"pane_not_found","message":"pane wX:p99 not found"},"id":"cli:request"}', True, False),
    (b'{"error":{"code":"workspace_not_found","message":"w"},"id":"cli:tab:create"}', True, False),
    (b"Error: Custom { kind: Other, error: EmptyResponse }", False, False),  # read-only: plain error
])
def test_n1_nonzero_exit_is_uncertain_unless_a_known_refusal(monkeypatch, err, mutating, uncertain):
    import subprocess

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Proc(1, b"", err))
    with pytest.raises(P.PluginError) as info:
        P.HerdrCli("herdr").run("pane", "run", "wE:p1", "x", mutating=mutating)
    assert isinstance(info.value, P.PluginUncertain) is uncertain


def test_n1_lost_response_after_accepted_pane_run_keeps_the_reservation(monkeypatch, cfg_dir):
    import subprocess

    cli = FakeCli()
    real = P.HerdrCli("herdr")

    def accepted_then_lost(args):  # Herdr typed the command, then the reply was lost (exit 1)
        monkeypatch.setattr(subprocess, "run",
                            lambda *a, **k: _Proc(1, b"", b"Error: Custom { kind: Other, error: EmptyResponse }"))
        real.run(*args, mutating=True)
    cli.shared["on_run"] = accepted_then_lost
    ops, _ = make_ops(cli, cfg_dir)
    with pytest.raises(P.PluginUncertain):
        ops.launch()
    assert ops.reservation() is not None
    cli.shared["on_run"] = None
    ops.launch()  # retry: still reserved
    assert len(cli.called("pane", "run")) == 1 and len(cli.called("tab", "create")) == 0


def test_minor_stop_cancels_an_expired_start_and_admission_rejects_it(cfg_dir):
    cli = FakeCli()
    ops, out = make_ops(cli, cfg_dir)
    ops.launch()
    old = last_launch(ops)
    ops.clock.t += P.RESERVATION_TTL + 1
    assert ops.reservation() is None  # no longer blocks launches...
    assert not P.launch_admitted(ops.state_dir, old["launch_id"], now=ops.clock())  # ...nor admits its child
    assert ops.stop() == 0 and out[-1].startswith("cancelled a pending bridge start")
    assert last_launch(ops)["cancelled"] is True


def test_minor_admission_requires_current_active_unexpired_token(cfg_dir):
    cli = FakeCli()
    ops, _ = make_ops(cli, cfg_dir)
    ops.launch()
    rec = last_launch(ops)
    now = rec["at"] + 1
    assert P.launch_admitted(ops.state_dir, rec["launch_id"], now=now)
    assert not P.launch_admitted(ops.state_dir, "someone-else", now=now)
    P.ack_launch(ops.state_dir, rec["launch_id"])  # inactive
    assert not P.launch_admitted(ops.state_dir, rec["launch_id"], now=now)


def test_cli_timeout_is_uncertain_but_a_missing_cli_is_not(monkeypatch):
    import subprocess

    def timeout(*a, **k):
        raise subprocess.TimeoutExpired("herdr", 20)
    monkeypatch.setattr(subprocess, "run", timeout)
    with pytest.raises(P.PluginUncertain):
        P.HerdrCli("herdr").run("pane", "run", "wE:p1", "x")

    def missing(*a, **k):
        raise FileNotFoundError("herdr")
    monkeypatch.setattr(subprocess, "run", missing)
    with pytest.raises(P.PluginError) as info:
        P.HerdrCli("herdr").run("pane", "run", "wE:p1", "x")
    assert not isinstance(info.value, P.PluginUncertain)


def _concurrently(*fns):
    errors = []

    def wrap(fn):
        try:
            fn()
        except Exception as exc:  # pragma: no cover - surfaced below
            errors.append(exc)
    threads = [threading.Thread(target=wrap, args=(fn,)) for fn in fns]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert not errors, errors


def test_concurrent_starts_create_one_workspace_and_type_once(cfg_dir):
    cli = FakeCli()
    barrier = threading.Barrier(2, timeout=0.5)
    orig_json = cli.json

    def slow_json(*args):
        if args[:2] == ("workspace", "list"):
            try:
                barrier.wait()  # both launchers would observe "no workspace" without the lifecycle lock
            except threading.BrokenBarrierError:
                pass
        return orig_json(*args)
    cli.json = slow_json
    ops1, _ = make_ops(cli, cfg_dir, real_time=True)
    ops2, _ = make_ops(cli, cfg_dir, real_time=True)
    _concurrently(ops1.launch, ops2.launch)
    assert len(cli.called("workspace", "create")) == 1
    assert len(cli.called("pane", "run")) == 1


def test_concurrent_start_and_restart_type_once(cfg_dir):
    servers, srv = bridge_ws()
    pane = srv.add_pane("wE")
    cli = FakeCli(servers)
    ops1, _ = make_ops(cli, cfg_dir, real_time=True)
    ops2, _ = make_ops(cli, cfg_dir, real_time=True)
    held = hold_bridge(ops1, cli, pane)
    _previous_launch(ops1, pane, srv.panes[pane]["terminal_id"])
    honor_stop(ops1, held)
    _concurrently(ops1.restart, ops2.launch)
    assert len(cli.called("pane", "run")) == 1


def _child_handoff(ops, launch_id, done):
    """What `python -m herdr_slackbot run --launch-id` does at startup (see __main__.run)."""
    with P.lifecycle_lock(ops.state_dir):
        if not P.launch_admitted(ops.state_dir, launch_id):
            done["result"] = "cancelled"
            return
        done["lock"] = InstanceLock(ops.state_dir).acquire()
        P.ack_launch(ops.state_dir, launch_id)
        done["result"] = "running"


def test_r3_child_ack_cannot_fall_between_launch_checks(cfg_dir):
    cli = FakeCli()
    ops1, _ = make_ops(cli, cfg_dir, real_time=True)
    ops1.launch()  # reservation active; its child has not reported in yet
    launch_id = last_launch(ops1)["launch_id"]
    ops2, out2 = make_ops(cli, cfg_dir, real_time=True)
    done = {}
    orig_running = ops2.running_pid
    child = threading.Thread(target=_child_handoff, args=(ops1, launch_id, done))

    def running_then_child_acks():
        result = orig_running()
        if not child.is_alive() and "result" not in done:
            child.start()  # the review's interleaving: the child acks right after this check...
            time.sleep(0.3)  # ...but it must wait for the launcher's mutex
        return result
    ops2.running_pid = running_then_child_acks
    ops2.launch()
    child.join(5)
    try:
        assert done["result"] == "running"
        assert len(cli.called("pane", "run")) == 1  # the second launcher saw "starting", not "stopped"
        assert "already in progress" in out2[-1]
    finally:
        if done.get("lock"):
            done["lock"].release()


def test_stop_while_starting_cancels_the_start(cfg_dir):
    cli = FakeCli()
    ops, out = make_ops(cli, cfg_dir)
    ops.launch()
    launch_id = last_launch(ops)["launch_id"]
    assert ops.stop() == 0 and out[-1].startswith("cancelled a pending bridge start")
    done = {}
    _child_handoff(ops, launch_id, done)
    assert done["result"] == "cancelled" and not ops.lock.is_held()
    assert ops.status_lines()[0] == "bridge: stopped"
    ops.launch()  # a new start is allowed and is admitted
    assert P.launch_admitted(ops.state_dir, last_launch(ops)["launch_id"], now=ops.clock())


# --- stop / restart: addressed stop request (M3 review #1, recheck R1) ---------------------------

def test_stop_sends_an_addressed_request_and_never_types(cfg_dir):
    servers, srv = bridge_ws()
    pane = srv.add_pane("wE")
    cli = FakeCli(servers)
    killed = []
    ops, out = make_ops(cli, cfg_dir, kill=killed.append)
    held = hold_bridge(ops, cli, pane)
    requests = []
    orig_sleep = ops.sleep

    def sleep(seconds):
        requests.append(P._read_json(ops.state_dir / P.STOP_FILE))
        orig_sleep(seconds)
    ops.sleep = sleep
    honor_stop(ops, held)
    assert ops.stop() == 0
    assert requests[0]["pid"] == os.getpid() and requests[0]["start"] == MY_START
    assert cli.calls == []  # no pane lookups, no send-keys: nothing reaches any terminal
    assert killed == [] and out == ["bridge stopped"]
    assert not (ops.state_dir / P.STOP_FILE).exists()


def test_r1_bridge_exit_during_stop_puts_nothing_into_its_old_terminal(cfg_dir):
    servers, srv = bridge_ws()
    pane = srv.add_pane("wE")
    cli = FakeCli(servers)
    killed = []
    ops, out = make_ops(cli, cfg_dir, kill=killed.append)
    held = hold_bridge(ops, cli, pane)
    orig_verify = ops.verified_bridge

    def verify_then_exit():
        result = orig_verify()
        held.release()  # the bridge exits right after verification...
        srv.panes[pane]["fg"] = "editor.exe"  # ...and its terminal now runs something else
        return result
    ops.verified_bridge = verify_then_exit
    assert ops.stop() == 0
    assert cli.called("pane", "send-keys") == [] and killed == []
    assert P.stop_requested(ops.state_dir, os.getpid(), MY_START) is False  # request cleared


def test_stop_request_is_only_honoured_by_its_addressee(tmp_path):
    P.request_stop(tmp_path, {"pid": 10, "start": 1})
    assert P.stop_requested(tmp_path, 10, 1)
    assert not P.stop_requested(tmp_path, 10, 2)  # same pid, different process (reused pid)
    assert not P.stop_requested(tmp_path, 11, 1)
    P.clear_stop_request(tmp_path, pid=11)  # someone else's clear leaves it
    assert P.stop_requested(tmp_path, 10, 1)
    P.clear_stop_request(tmp_path)
    assert not P.stop_requested(tmp_path, 10, 1)


def test_stop_refuses_when_the_pid_is_not_the_recorded_process(cfg_dir):
    servers, srv = bridge_ws()
    pane = srv.add_pane("wE")
    cli = FakeCli(servers)
    killed = []
    ops, out = make_ops(cli, cfg_dir, kill=killed.append, starts={os.getpid(): 999})  # pid reused
    with hold_bridge(ops, cli, pane):
        assert ops.stop() == 1
    assert killed == [] and not (ops.state_dir / P.STOP_FILE).exists()
    assert "cannot verify" in out[-1]


def test_stop_refuses_when_the_record_is_from_another_process(cfg_dir):
    servers, srv = bridge_ws()
    pane = srv.add_pane("wE")
    cli = FakeCli(servers)
    killed = []
    ops, out = make_ops(cli, cfg_dir, kill=killed.append)
    with hold_bridge(ops, cli, pane):
        rec = P.read_runtime_record(ops.state_dir)
        P._write_json(ops.state_dir / P.RUNTIME_FILE, dict(rec, pid=424242))  # stale record of an old bridge
        assert ops.stop() == 1
    assert killed == []


def test_stop_rechecks_the_process_before_terminating(cfg_dir):
    servers, srv = bridge_ws()
    pane = srv.add_pane("wE")
    cli = FakeCli(servers)
    killed = []
    starts = {os.getpid(): MY_START}
    ops, out = make_ops(cli, cfg_dir, kill=killed.append, starts=starts)
    orig_sleep = ops.sleep

    def sleep(seconds):
        starts[os.getpid()] = 555  # replaced during the wait
        orig_sleep(seconds)
    ops.sleep = sleep
    with hold_bridge(ops, cli, pane):
        assert ops.stop() == 1
    assert killed == []
    assert out[-1].startswith("not terminating")


def test_stop_terminates_a_verified_bridge_that_ignores_the_request(cfg_dir):
    servers, srv = bridge_ws()
    pane = srv.add_pane("wE")
    cli = FakeCli(servers)
    ops, out = make_ops(cli, cfg_dir)
    held = hold_bridge(ops, cli, pane)
    killed = []
    ops.kill = lambda pid: (killed.append(pid), held.release())
    assert ops.stop() == 0
    assert killed == [os.getpid()] and "terminating pid" in out[0]


def test_stop_reports_failure_when_the_bridge_survives(cfg_dir):
    servers, srv = bridge_ws()
    pane = srv.add_pane("wE")
    cli = FakeCli(servers)
    ops, out = make_ops(cli, cfg_dir)
    with hold_bridge(ops, cli, pane):
        assert ops.stop() == 1
    assert out[-1] == "bridge did not stop"


def test_stop_when_not_running_ignores_a_stale_record(cfg_dir):
    servers, srv = bridge_ws()
    pane = srv.add_pane("wE")
    cli = FakeCli(servers)
    ops, out = make_ops(cli, cfg_dir)
    hold_bridge(ops, cli, pane).release()  # the bridge exited; its record stays behind
    assert ops.stop() == 0 and out == ["bridge is not running"]
    assert cli.calls == []


def test_restart_stops_then_launches_in_a_fresh_tab(cfg_dir):
    servers, srv = bridge_ws()
    pane = srv.add_pane("wE")
    cli = FakeCli(servers)
    ops, _ = make_ops(cli, cfg_dir)
    held = hold_bridge(ops, cli, pane)
    _previous_launch(ops, pane, srv.panes[pane]["terminal_id"])
    honor_stop(ops, held)
    assert ops.restart() == 0
    assert cli.called("pane", "send-keys") == []
    (run,) = cli.called("pane", "run")
    assert run[2] != pane and last_launch(ops)["previous"]["pane_id"] == pane


# --- status ----------------------------------------------------------------------------

def test_status_lines_and_notification(cfg_dir):
    (cfg_dir / ".env").write_text("SLACK_BOT_TOKEN=xoxb-1\n", encoding="utf-8")
    cli = FakeCli()
    ops, out = make_ops(cli, cfg_dir)
    assert ops.status(notify=True) == 0
    assert out[0] == "bridge: stopped"
    assert "slack: missing SLACK_APP_TOKEN" in out
    (note,) = cli.called("notification", "show")
    assert note[2] == "herdr-slackbot"
    body = note[note.index("--body") + 1]
    assert "bridge: stopped" in body and "missing SLACK_APP_TOKEN" in body and "xoxb" not in body


@pytest.mark.parametrize("case,expected", [
    ("verified", ", pane {pane})"),
    ("other_server", ", on another Herdr server (B))"),
    ("replaced", ", pane unverified)"),
])
def test_status_running_shows_verified_location(cfg_dir, case, expected):
    servers, srv = bridge_ws("B" if case == "other_server" else "A")
    pane = srv.add_pane("wE")
    cli = FakeCli(servers, socket="A")
    ops, out = make_ops(cli, cfg_dir)
    with hold_bridge(ops, cli, pane, socket=srv.socket):
        if case == "replaced":
            srv.panes[pane]["terminal_id"] = "other"
        ops.status()
    assert out[0] == f"bridge: running (pid {os.getpid()}" + expected.format(pane=pane)
    assert any("(missing — run setup)" in line for line in out)


def test_notification_failure_is_reported_not_raised(cfg_dir):
    cli = FakeCli(fail={("notification", "show")})
    ops, out = make_ops(cli, cfg_dir)
    assert ops.status(notify=True) == 0
    assert out[-1].startswith("notification failed")


def test_is_held_does_not_create_or_write(tmp_path):
    lock = InstanceLock(tmp_path / "state")
    assert lock.is_held() is False
    assert not (tmp_path / "state").exists()
    with InstanceLock(tmp_path / "state"):
        assert lock.is_held() is True
        assert lock.read_pid() == os.getpid()
    assert lock.is_held() is False


def test_runtime_record_and_ack(tmp_path):
    rec = P.write_runtime_record(tmp_path, {"pane_id": "wE:p3", "terminal_id": "term-9"},
                                 {"HERDR_SOCKET_PATH": "S"})
    assert rec == P.read_runtime_record(tmp_path)
    assert (rec["pid"], rec["socket"], rec["pane_id"], rec["terminal_id"]) == (os.getpid(), "S", "wE:p3", "term-9")
    assert rec["start"] == P.procinfo.current_start_time()
    P.ack_launch(tmp_path, "nothing-to-ack")  # no launch file: no error


def test_real_process_helpers():
    from herdr_slackbot import procinfo

    me = procinfo.current_start_time()
    if me is None:
        pytest.skip("process times unavailable on this platform")
    assert procinfo.process_start_time(os.getpid()) == me
    assert procinfo.busy_children(os.getpid()) is not None


# --- entry point --------------------------------------------------------------------------

def test_plugin_command_errors_are_one_line(monkeypatch, cfg_dir, capsys):
    class Boom(FakeCli):
        def json(self, *args):
            raise P.PluginError("server_unavailable")

    monkeypatch.setattr(P, "HerdrCli", lambda *a, **k: Boom())
    monkeypatch.setenv("HERDR_PLUGIN_CONFIG_DIR", str(cfg_dir))
    assert main_mod.main(["launch"]) == 1
    assert capsys.readouterr().err.strip() == "herdr-slackbot launch: server_unavailable"


def test_config_dir_env_verbatim_prefix_is_stripped(monkeypatch, cfg_dir, capsys):
    monkeypatch.setattr(P, "HerdrCli", lambda *a, **k: FakeCli())
    monkeypatch.setenv("HERDR_PLUGIN_CONFIG_DIR", "\\\\?\\" + str(cfg_dir))
    assert main_mod.main(["status"]) == 0
    assert os.environ["HERDR_PLUGIN_CONFIG_DIR"] == str(cfg_dir)
    assert str(cfg_dir / ".env") in capsys.readouterr().out


def test_run_without_tokens_explains_instead_of_crashing(monkeypatch, cfg_dir, capsys):
    for key in ("SLACK_BOT_TOKEN", "SLACK_APP_TOKEN", "SLACK_OWNER_USER_ID"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("HERDR_PLUGIN_CONFIG_DIR", "unused")
    assert main_mod.main(["run", "--config-dir", str(cfg_dir)]) == 2
    err = capsys.readouterr().err
    assert "not configured yet" in err and "restart --plugin herdr-slackbot" in err


def test_slack_failure_reason_classification():
    from slack_bolt.error import BoltError
    from slack_sdk.errors import SlackApiError

    class Resp(dict):
        pass

    assert main_mod.slack_failure_reason(SlackApiError("x", Resp(error="invalid_auth"))) == "invalid_auth"
    assert main_mod.slack_failure_reason(BoltError("token xoxb-123-abc is invalid\nmore")) == \
        "token [redacted] is invalid"
    assert main_mod.slack_failure_reason(ValueError("bug")) is None


def test_run_reports_slack_auth_failure_cleanly(monkeypatch, cfg_dir, capsys):
    from slack_bolt.error import BoltError

    (cfg_dir / ".env").write_text("SLACK_BOT_TOKEN=xoxb-1\nSLACK_APP_TOKEN=xapp-1\nSLACK_OWNER_USER_ID=U1\n",
                                  encoding="utf-8")
    monkeypatch.setenv("HERDR_PLUGIN_CONFIG_DIR", "unused")

    class Client:
        def ping(self):
            return {}

        def get_pane(self, pane_id):
            return {"pane_id": pane_id, "terminal_id": "term-own"}

    monkeypatch.setattr(main_mod.HerdrClient, "from_env", classmethod(lambda cls, *a, **k: Client()))

    def bad_app(*a, **k):
        raise BoltError("`token` is invalid (auth.test result: {'ok': False, 'error': 'invalid_auth'})")

    import herdr_slackbot.slack_app as slack_app
    monkeypatch.setattr(slack_app, "build_app", bad_app)
    monkeypatch.setenv("HERDR_PANE_ID", "wE:p7")
    import logging
    root = logging.getLogger()
    before = list(root.handlers)
    try:
        assert main_mod.main(["run", "--config-dir", str(cfg_dir)]) == 2
    finally:
        for h in root.handlers[:]:
            if h not in before:
                root.removeHandler(h)
                h.close()
    err = capsys.readouterr().err
    assert "Slack rejected the connection" in err
    assert not InstanceLock(cfg_dir / "state").is_held()
    rec = P.read_runtime_record(cfg_dir / "state")
    assert (rec["pid"], rec["pane_id"], rec["terminal_id"]) == (os.getpid(), "wE:p7", "term-own")
    assert rec["start"] == P.procinfo.current_start_time()


def test_run_acks_its_launch_reservation_even_when_it_exits_early(monkeypatch, cfg_dir, capsys):
    for key in ("SLACK_BOT_TOKEN", "SLACK_APP_TOKEN", "SLACK_OWNER_USER_ID"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("HERDR_PLUGIN_CONFIG_DIR", "unused")
    state = cfg_dir / "state"
    P._write_json(state / P.LAUNCH_FILE, {"launch_id": "L1", "active": True, "at": time.time(),
                                          "socket": "A", "pane_id": "wE:p1", "terminal_id": "t"})
    assert main_mod.main(["run", "--config-dir", str(cfg_dir), "--launch-id", "L1"]) == 2
    assert json.loads((state / P.LAUNCH_FILE).read_text(encoding="utf-8"))["active"] is False


def test_run_with_a_cancelled_launch_id_does_not_start(monkeypatch, cfg_dir, capsys):
    (cfg_dir / ".env").write_text("SLACK_BOT_TOKEN=xoxb-1\nSLACK_APP_TOKEN=xapp-1\nSLACK_OWNER_USER_ID=U1\n",
                                  encoding="utf-8")
    monkeypatch.setenv("HERDR_PLUGIN_CONFIG_DIR", "unused")
    state = cfg_dir / "state"
    P._write_json(state / P.LAUNCH_FILE, {"launch_id": "L1", "active": False, "cancelled": True, "at": time.time()})
    monkeypatch.setattr(main_mod.HerdrClient, "from_env",
                        classmethod(lambda cls, *a, **k: pytest.fail("must not touch Herdr")))
    import logging
    root = logging.getLogger()
    before = list(root.handlers)
    try:
        assert main_mod.main(["run", "--config-dir", str(cfg_dir), "--launch-id", "L1"]) == 0
    finally:
        for h in root.handlers[:]:
            if h not in before:
                root.removeHandler(h)
                h.close()
    assert "cancelled" in capsys.readouterr().err
    assert not InstanceLock(state).is_held() and not (state / P.RUNTIME_FILE).exists()


@pytest.mark.parametrize("token,closes", [({"active": True}, True), ({"active": False, "cancelled": True}, False)])
def test_r2_early_exit_ack_closes_previous_tab_only_for_our_own_active_start(monkeypatch, cfg_dir, token, closes):
    for key in ("SLACK_BOT_TOKEN", "SLACK_APP_TOKEN", "SLACK_OWNER_USER_ID"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("HERDR_PLUGIN_CONFIG_DIR", "unused")
    state = cfg_dir / "state"
    P._write_json(state / P.LAUNCH_FILE, {"launch_id": "L1", "at": time.time(), **token})
    calls = []
    monkeypatch.setattr(P, "close_previous_tab", lambda *a, **k: calls.append(a) or "closed")
    assert main_mod.main(["run", "--config-dir", str(cfg_dir), "--launch-id", "L1"]) == 2
    assert bool(calls) is closes
