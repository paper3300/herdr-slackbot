"""Readiness reporting (release recheck): an owner key in .env is not proof that the bridge serves it.
The bridge writes `bridge.ready.json` once owner mode fully started; status and the wizard trust it
only while the writing process still holds the bridge lock and is the same process."""

import json
import logging
import os

import pytest

from herdr_slackbot import __main__ as main_mod
from herdr_slackbot import plugin as P
from herdr_slackbot.pairing import pairing_path
from herdr_slackbot.state import InstanceLock
from test_plugin import FakeCli

ENV = "SLACK_BOT_TOKEN=xoxb-1\nSLACK_APP_TOKEN=xapp-1\nSLACK_OWNER_USER_ID={owner}\n"


@pytest.fixture
def state(tmp_path):
    (tmp_path / ".env").write_text(ENV.format(owner="UKIM"), encoding="utf-8")
    d = tmp_path / "state"
    d.mkdir()
    return tmp_path, d


def slack_line(ops):
    return next(line for line in ops.status_lines() if line.startswith("slack: "))


# --- the marker ------------------------------------------------------------------------------------

def test_marker_counts_only_for_the_live_lock_holder(state):
    _, d = state
    P.write_ready_marker(d, "UKIM")
    assert P.verified_ready(d) is None  # nobody holds the bridge lock: a leftover from a crash
    lock = InstanceLock(d).acquire()  # this process is the "bridge"
    try:
        assert P.verified_ready(d)["owner"] == "UKIM"
        assert P.verified_ready(d, start_time=lambda pid: 12345) is None  # pid reused by another process
        data = json.loads((d / P.READY_FILE).read_text(encoding="utf-8"))
        (d / P.READY_FILE).write_text(json.dumps(dict(data, pid=data["pid"] + 1)), encoding="utf-8")
        assert P.verified_ready(d) is None  # written by another process than the lock holder
    finally:
        lock.release()


def test_clear_marker_only_removes_its_own(state):
    _, d = state
    P.write_ready_marker(d, "UKIM")
    P.clear_ready_marker(d, pid=os.getpid() + 1)
    assert (d / P.READY_FILE).exists()
    P.clear_ready_marker(d, pid=os.getpid())
    assert not (d / P.READY_FILE).exists()


# --- plugin status ------------------------------------------------------------------------------------

def make_ops(cfg_dir, monkeypatch, pid=None, ready=None):
    ops = P.PluginOps(FakeCli(), cfg_dir, out=lambda line: None, env={})
    monkeypatch.setattr(ops, "running_pid", lambda: pid)
    monkeypatch.setattr(ops, "verified_bridge", lambda: (None, "x"))
    monkeypatch.setattr(P, "verified_ready", lambda state_dir, start_time=None: ready)
    return ops


def test_status_ready_only_with_a_verified_marker(state, monkeypatch):
    cfg_dir, _ = state
    assert slack_line(make_ops(cfg_dir, monkeypatch, pid=1234, ready={"owner": "UKIM"})) == "slack: ready (owner UKIM)"


def test_status_owner_key_alone_is_not_ready(state, monkeypatch):
    cfg_dir, _ = state
    line = slack_line(make_ops(cfg_dir, monkeypatch, pid=1234))
    assert "not ready" in line and line != "slack: configured"
    assert slack_line(make_ops(cfg_dir, monkeypatch)) == "slack: configured; the bridge is not running"


def test_status_reports_failed_and_starting_activation(state, monkeypatch):
    cfg_dir, d = state
    pairing_path(d).write_text(json.dumps({"state": "failed", "at": 1}), encoding="utf-8")
    assert "owner mode failed to start" in slack_line(make_ops(cfg_dir, monkeypatch, pid=1234))
    pairing_path(d).write_text(json.dumps({"state": "activating", "at": 1}), encoding="utf-8")
    assert slack_line(make_ops(cfg_dir, monkeypatch, pid=1234)) == "slack: paired; owner mode is starting"


def test_status_failed_activation_while_env_still_empty(state, monkeypatch):
    """The owner is saved before activation, but a status check may read .env in between."""
    cfg_dir, d = state
    (cfg_dir / ".env").write_text(ENV.format(owner=""), encoding="utf-8")
    pairing_path(d).write_text(json.dumps({"state": "failed", "at": 1}), encoding="utf-8")
    assert "owner mode failed to start" in slack_line(make_ops(cfg_dir, monkeypatch, pid=1234))


def test_status_uses_the_real_marker_check(state, monkeypatch):
    cfg_dir, d = state
    P.write_ready_marker(d, "UKIM")  # stale: no lock holder
    ops = P.PluginOps(FakeCli(), cfg_dir, out=lambda line: None, env={})
    monkeypatch.setattr(ops, "running_pid", lambda: 1234)
    monkeypatch.setattr(ops, "verified_bridge", lambda: (None, "x"))
    assert "not ready" in slack_line(ops)


# --- the bridge writes and clears it ----------------------------------------------------------------------

class _Handler:
    def close(self):
        pass


def _run_bridge(monkeypatch, cfg_dir, on_loop, owner="UKIM"):
    """main.run() with Herdr, Slack and the stop request stubbed; `on_loop(state_dir, built)` runs in
    the first main-loop iteration."""
    import herdr_slackbot.slack_app as slack_app

    (cfg_dir / ".env").write_text(ENV.format(owner=owner), encoding="utf-8")
    monkeypatch.setenv("HERDR_PLUGIN_CONFIG_DIR", "unused")
    monkeypatch.delenv("SLACK_OWNER_USER_ID", raising=False)
    monkeypatch.delenv("HERDR_PANE_ID", raising=False)

    class Client:
        def ping(self):
            return {}

    monkeypatch.setattr(main_mod.HerdrClient, "from_env", classmethod(lambda cls, *a, **k: Client()))
    monkeypatch.setattr(P.HerdrCli, "run", lambda self, *args, **k: "")
    built = {}

    def fake_build_app(cfg, bridge, **kwargs):
        built.update(kwargs, bridge=bridge)
        return object()

    def fake_stop_requested(state_dir, pid, start):
        on_loop(state_dir, built)
        return True

    import herdr_slackbot.bridge as bridge_mod

    monkeypatch.setattr(bridge_mod.Bridge, "start", lambda self: None)
    monkeypatch.setattr(bridge_mod.Bridge, "stop", lambda self: None)
    monkeypatch.setattr(slack_app, "build_app", fake_build_app)
    monkeypatch.setattr(slack_app, "run_socket_mode", lambda app, token: _Handler())
    monkeypatch.setattr(P, "stop_requested", fake_stop_requested)
    root = logging.getLogger()
    before = list(root.handlers)
    try:
        assert main_mod.main(["run", "--config-dir", str(cfg_dir)]) == 0
    finally:
        for h in root.handlers[:]:
            if h not in before:
                root.removeHandler(h)
                h.close()
    return built


def test_run_in_owner_mode_marks_ready_and_clears_on_exit(monkeypatch, tmp_path):
    seen = []
    _run_bridge(monkeypatch, tmp_path, lambda state_dir, built: seen.append(P.verified_ready(state_dir)))
    assert seen[0]["owner"] == "UKIM" and seen[0]["pid"] == os.getpid()
    assert not (tmp_path / "state" / P.READY_FILE).exists()


def test_run_in_pairing_mode_marks_ready_only_after_activation(monkeypatch, tmp_path):
    seen = {}

    def on_loop(state_dir, built):
        seen["before"] = P.verified_ready(state_dir)
        pairing = built["pairing"]
        code = json.loads(pairing_path(state_dir).read_text(encoding="utf-8"))["code"]
        assert pairing.attempt("UKIM", code).ok
        built["bridge"].activate_owner = lambda user: None  # owner mode "starts"
        built["on_paired"]("UKIM")
        seen["after"] = P.verified_ready(state_dir)
        seen["pairing_file"] = pairing_path(state_dir).exists()

    _run_bridge(monkeypatch, tmp_path, on_loop, owner="")
    assert seen["before"] is None
    assert seen["after"]["owner"] == "UKIM" and seen["pairing_file"] is False
    assert not (tmp_path / "state" / P.READY_FILE).exists()


def test_run_in_pairing_mode_failed_activation_writes_no_marker(monkeypatch, tmp_path):
    seen = {}

    def on_loop(state_dir, built):
        pairing = built["pairing"]
        pairing.sleep = lambda s: None
        code = json.loads(pairing_path(state_dir).read_text(encoding="utf-8"))["code"]
        pairing.attempt("UKIM", code)

        def broken(user):
            raise RuntimeError("open_dm failed")

        built["bridge"].activate_owner = broken
        built["on_paired"]("UKIM")
        seen["ready"] = P.verified_ready(state_dir)
        seen["state"] = json.loads(pairing_path(state_dir).read_text(encoding="utf-8"))["state"]

    _run_bridge(monkeypatch, tmp_path, on_loop, owner="")
    assert seen == {"ready": None, "state": "failed"}
