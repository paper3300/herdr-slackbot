"""Owner pairing (release R2): code state machine, Slack gate while unpaired, live owner switch."""

import dataclasses
import json

import pytest

from fakes import Clock, FakeHerdr, FakeManager, FakeTransport, SyncExecutor
from herdr_slackbot import blocks as B
from herdr_slackbot.bridge import Bridge
from herdr_slackbot.config import load_config, load_env_file
from herdr_slackbot.pairing import (
    CODE_TTL,
    MAX_WRONG,
    STATE_ACTIVATING,
    STATE_FAILED,
    USER_MAX_WRONG,
    USER_WINDOW,
    PairResult,
    PairStatus,
    Pairing,
    code_banner,
    generate_code,
    not_paired_text,
    pairing_path,
    read_pairing,
    read_pairing_record,
    result_text,
)
from herdr_slackbot.plugin import PluginOps
from herdr_slackbot.results import ResultStore
from herdr_slackbot.setup_cmd import run_setup, update_env_file
from herdr_slackbot.slack_app import NOT_OWNER_TEXT, build_app
from herdr_slackbot.state import StateStore
from test_slack_app import authorize, block_action, body_json, command, dispatch, message_event, view_submission

CMD = "/herdr-me"


class Codes:
    """Deterministic code generator: 100001, 100002, ..."""

    def __init__(self):
        self.n = 100000

    def __call__(self):
        self.n += 1
        return str(self.n)


@pytest.fixture
def machine(tmp_path):
    clock, saved, shown = Clock(), [], []
    p = Pairing(tmp_path, saved.append, lambda code, reason: shown.append((code, reason)), clock=clock,
                new_code=Codes(), sleep=clock.sleep)
    return p, clock, saved, shown, tmp_path


# --- code generation / file ------------------------------------------------------------------

def test_generate_code_is_six_digits_zero_padded():
    assert generate_code(lambda n: 42) == "000042"
    assert generate_code(lambda n: n - 1) == "999999"
    codes = {generate_code() for _ in range(50)}
    assert all(len(c) == 6 and c.isdigit() for c in codes) and len(codes) > 1


def test_begin_writes_pairing_file_and_announces(machine):
    p, clock, saved, shown, state_dir = machine
    assert read_pairing(state_dir) is None
    p.begin()
    assert shown == [("100001", "start")]
    rec = json.loads(pairing_path(state_dir).read_text(encoding="utf-8"))
    assert rec == {"state": "waiting", "code": "100001", "created": clock.now, "attempts": 0}
    assert read_pairing(state_dir)["code"] == "100001"


def test_read_pairing_ignores_garbage(tmp_path):
    pairing_path(tmp_path).write_text('{"code": "12ab"}', encoding="utf-8")
    assert read_pairing(tmp_path) is None
    pairing_path(tmp_path).write_text("not json", encoding="utf-8")
    assert read_pairing(tmp_path) is None


# --- attempts ----------------------------------------------------------------------------------

def test_correct_code_is_two_phase_saved_then_activated(machine):
    p, _, saved, _, state_dir = machine
    p.begin()
    result = p.attempt("UKIM", " 100001 ")
    assert result.status is PairStatus.PAIRED and result.owner == "UKIM" and result.ok
    # phase 1: owner saved, code consumed, but not served yet
    assert saved == ["UKIM"] and p.pending == "UKIM" and p.owner is None and not p.active
    assert read_pairing(state_dir) is None and read_pairing_record(state_dir)["state"] == STATE_ACTIVATING
    assert p.attempt("UOTHER", "100001").status is PairStatus.ALREADY
    # phase 2: owner mode started
    activated = []
    assert p.complete(activated.append) is True
    assert activated == ["UKIM"] and p.owner == "UKIM" and p.pending is None
    assert not pairing_path(state_dir).exists()
    assert p.complete(lambda u: pytest.fail("already done")) is True


def test_activation_is_retried_then_succeeds(machine):
    p, clock, _, shown, _ = machine
    p.begin()
    p.attempt("UKIM", "100001")
    calls = []

    def flaky(user):
        calls.append(clock.now)
        if len(calls) < 3:
            raise RuntimeError("open_dm: ratelimited")

    t0 = clock.now
    assert p.complete(flaky) is True
    assert [round(t - t0) for t in calls] == [0, 1, 4] and p.owner == "UKIM"
    assert [r for _, r in shown] == ["start"]


def test_activation_failure_is_visible_and_needs_restart(machine):
    p, _, _, shown, state_dir = machine
    p.begin()
    p.attempt("UKIM", "100001")

    def broken(user):
        raise RuntimeError("herdr down")

    assert p.complete(broken) is False
    assert p.failed and p.owner is None and p.pending == "UKIM"
    assert read_pairing_record(state_dir)["state"] == STATE_FAILED
    assert shown[-1] == ("", "failed")
    assert "restart" in code_banner("", CMD, "failed")


def test_wrong_attempts_count_and_rotate_after_max(machine):
    p, _, saved, shown, state_dir = machine
    p.begin()
    for i in range(MAX_WRONG - 1):
        r = p.attempt("UX", "999999")
        assert r.status is PairStatus.WRONG and r.remaining == MAX_WRONG - 1 - i
    assert read_pairing(state_dir)["attempts"] == MAX_WRONG - 1
    r = p.attempt("UY", "999998")  # attempts from all users count together
    assert r.status is PairStatus.ROTATED
    assert shown[-1] == ("100002", "attempts") and read_pairing(state_dir)["code"] == "100002"
    assert read_pairing(state_dir)["attempts"] == 0
    assert p.attempt("UZ", "100001").status is PairStatus.WRONG  # the old code is dead
    assert p.attempt("UZ", "100002").ok and saved == ["UZ"]


def test_non_numeric_codes_count_as_wrong_but_blank_is_usage(machine):
    p, *_ = machine
    p.begin()
    assert p.attempt("UX", "").status is PairStatus.USAGE
    assert p.attempt("UX", "abc").status is PairStatus.WRONG
    assert p.attempt("UX", "1000011").status is PairStatus.WRONG  # longer than the code


def test_code_expires_and_rotates_on_attempt(machine):
    p, clock, saved, shown, _ = machine
    p.begin()
    clock.now += CODE_TTL + 1
    r = p.attempt("UX", "100001")  # right code, too late
    assert r.status is PairStatus.EXPIRED and saved == []
    assert shown[-1] == ("100002", "expired")
    assert p.attempt("UX", "100002").ok


def test_tick_rotates_expired_code(machine):
    p, clock, _, shown, state_dir = machine
    p.begin()
    clock.now += CODE_TTL - 1
    p.tick()
    assert len(shown) == 1
    clock.now += 2
    p.tick()
    assert shown[-1] == ("100002", "expired") and read_pairing(state_dir)["code"] == "100002"
    p.tick()
    assert len(shown) == 2  # the new code has its own TTL


def test_no_code_before_begin(machine):
    p, *_ = machine
    assert p.attempt("UX", "100001").status is PairStatus.NO_CODE


def test_a_guessing_user_is_throttled_alone_and_released_after_the_window(machine):
    p, clock, saved, shown, _ = machine
    p.begin()
    statuses = [p.attempt("UBAD", "000000").status for _ in range(USER_MAX_WRONG)]
    assert statuses[-1] is PairStatus.ROTATED  # 5 wrong in total rotate the code, as specified
    code = shown[-1][0]
    r = p.attempt("UBAD", code)  # even the right code: this user is not checked while throttled
    assert r.status is PairStatus.THROTTLED and 0 < r.retry_after <= USER_WINDOW
    assert "Try again in 10 min" in result_text(r, CMD)
    assert saved == [] and read_pairing(p.state_dir)["attempts"] == 0  # throttled attempts don't count
    clock.now += USER_WINDOW
    p.tick()
    assert p.attempt("UBAD", "000000").status is PairStatus.WRONG  # window over


def test_nobody_can_lock_out_the_owner(machine):
    """Many guessing accounts only rotate the code and throttle themselves; the owner (no wrong
    guesses) always gets the current code checked."""
    p, clock, saved, shown, _ = machine
    p.begin()
    for n in range(40):
        for _ in range(USER_MAX_WRONG + 2):
            p.attempt(f"UBAD{n}", "000000")
        clock.now += 1
    assert p.active
    code = read_pairing(p.state_dir)["code"]
    assert p.attempt("UOWNER", code).ok and saved == ["UOWNER"]


def test_owner_typos_throttle_only_briefly(machine):
    p, clock, saved, shown, _ = machine
    p.begin()
    for _ in range(USER_MAX_WRONG):
        p.attempt("UKIM", "000000")
    code = read_pairing(p.state_dir)["code"]
    assert p.attempt("UKIM", code).status is PairStatus.THROTTLED
    clock.now += USER_WINDOW
    p.tick()  # the code expired meanwhile: a new one is shown
    assert p.attempt("UKIM", read_pairing(p.state_dir)["code"]).ok and saved == ["UKIM"]


def test_persist_failure_keeps_waiting(machine):
    p, *_ = machine

    def boom(user):
        raise OSError("disk full")

    p.persist_owner = boom
    p.begin()
    assert p.attempt("UX", "100001").status is PairStatus.ERROR
    assert p.owner is None and p.active
    p.persist_owner = lambda user: None
    assert p.attempt("UX", "100001").ok


def test_codes_are_compared_in_constant_time(machine, monkeypatch):
    import hmac

    calls = []
    real = hmac.compare_digest
    monkeypatch.setattr(hmac, "compare_digest", lambda a, b: calls.append((a, b)) or real(a, b))
    p, *_ = machine
    p.begin()
    p.attempt("UX", "123456")
    assert calls == [(b"123456", b"100001")]


def test_codes_never_logged(machine, caplog):
    p, clock, *_ = machine
    caplog.set_level("DEBUG")
    p.begin()
    p.attempt("UX", "555555")
    clock.now += CODE_TTL + 1
    p.tick()
    p.attempt("UX", "100002")
    assert "10000" not in caplog.text and "555555" not in caplog.text


def test_texts():
    assert "pair <code>" in not_paired_text(CMD) and "waiting for pairing" in not_paired_text(CMD)
    banner = code_banner("123456", CMD, "start")
    assert "123456" in banner and f"{CMD} pair 123456" in banner
    assert "could not start" in code_banner("", CMD, "failed")

    assert result_text(PairResult(PairStatus.PAIRED, "U1"), CMD).startswith("paired ✅")
    assert "3 attempts left" in result_text(PairResult(PairStatus.WRONG, remaining=3), CMD)
    for status in PairStatus:
        assert result_text(PairResult(status), CMD)


# --- Slack gate while unpaired ----------------------------------------------------------------------

class GateBridge:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        return lambda *a, **k: self.calls.append((name, a))


@pytest.fixture
def gate(tmp_path):
    cfg = dataclasses.replace(load_config(env={"USERNAME": "me"}, config_dir=tmp_path), slack_owner_user_id="")
    saved, paired = [], []
    pairing = Pairing(tmp_path, saved.append, lambda c, r: None, new_code=Codes(), sleep=lambda s: None)
    pairing.begin()
    bridge, transport = GateBridge(), FakeTransport()
    app = build_app(cfg, bridge, pairing=pairing, on_paired=paired.append, transport=transport,
                    background=lambda fn: fn(), authorize=authorize, process_before_response=True)
    return app, bridge, transport, pairing, saved, paired


def test_pair_command_with_right_code_pairs_and_switches_owner(gate):
    app, bridge, transport, pairing, saved, paired = gate
    resp = body_json(dispatch(app, command("UKIM", "pair 100001")))
    assert resp["response_type"] == "ephemeral" and resp["text"].startswith("paired ✅")
    assert saved == ["UKIM"] and paired == ["UKIM"] and bridge.calls == []
    # owner mode not started yet (on_paired here only records): the guard stays closed
    resp = body_json(dispatch(app, command("UKIM", "list")))
    assert "still starting" in resp["text"] and bridge.calls == []
    assert body_json(dispatch(app, command("UOTHER", "pair 100001")))["text"] == NOT_OWNER_TEXT
    pairing.complete(lambda user: None)
    # from now on the new owner is served and everyone else is rejected, including `pair`
    assert body_json(dispatch(app, command("UKIM", "list"))) == {}
    assert bridge.calls[0][0] == "run_command"
    assert body_json(dispatch(app, command("UOTHER", "pair 100001")))["text"] == NOT_OWNER_TEXT
    assert body_json(dispatch(app, command("UOTHER", "list")))["text"] == NOT_OWNER_TEXT


def test_failed_activation_is_reported_to_the_paired_user(gate):
    app, bridge, transport, pairing, saved, paired = gate
    dispatch(app, command("UKIM", "pair 100001"))

    def broken(user):
        raise RuntimeError("open_dm failed")

    assert pairing.complete(broken) is False
    resp = body_json(dispatch(app, command("UKIM", "list")))
    assert "could not start" in resp["text"] and "restart" in resp["text"]
    dispatch(app, message_event("UKIM", "hello"))
    assert "could not start" in transport.ephemerals[-1]["text"]
    assert body_json(dispatch(app, command("UOTHER", "list")))["text"] == NOT_OWNER_TEXT
    assert bridge.calls == []


def test_pair_wrong_code_replies_ephemeral(gate):
    app, bridge, _, pairing, saved, paired = gate
    resp = body_json(dispatch(app, command("UX", "pair 000000")))
    assert resp["response_type"] == "ephemeral" and "Wrong pairing code" in resp["text"]
    resp = body_json(dispatch(app, command("UX", "PAIR")))
    assert "Usage" in resp["text"]
    assert saved == paired == [] and bridge.calls == [] and pairing.active


@pytest.mark.parametrize("text", ["", "list", "status", "new", "send coder hi"])
def test_other_commands_get_not_paired_notice(gate, text):
    app, bridge, *_ = gate
    resp = body_json(dispatch(app, command("UX", text)))
    assert resp == {"text": not_paired_text(CMD), "response_type": "ephemeral"}
    assert bridge.calls == []


def test_buttons_modals_messages_home_rejected_with_notice(gate):
    app, bridge, transport, *_ = gate
    msg_action = block_action("UX", B.ACTION_MUTE)
    msg_action["response_url"] = "https://hooks.slack/r"
    assert dispatch(app, msg_action).status == 200
    assert transport.responses == [{"url": "https://hooks.slack/r", "text": not_paired_text(CMD), "blocks": None}]

    dispatch(app, block_action("UX", B.ACTION_HOME_REFRESH, view={"id": "VH", "type": "home"}))
    dispatch(app, {"type": "event_callback", "team_id": "T1", "api_app_id": "A1", "event_id": "Ev2",
                   "event_time": 1, "event": {"type": "app_home_opened", "user": "UX", "tab": "home",
                                              "channel": "D1"}})
    assert [u for u, _ in transport.published] == ["UX", "UX"]
    assert "not paired yet" in json.dumps(transport.published[0][1])

    dispatch(app, message_event("UX", "hello"))
    assert transport.ephemerals == [{"channel": "D1", "user": "UX", "text": not_paired_text(CMD), "blocks": None}]

    resp = body_json(dispatch(app, view_submission("UX", B.SEND_CALLBACK)))
    assert resp["response_action"] == "update" and "not paired yet" in json.dumps(resp["view"])

    resp = body_json(dispatch(app, {"type": "block_suggestion", "team": {"id": "T1"}, "user": {"id": "UX"},
                                    "api_app_id": "A1", "action_id": "x", "block_id": "b", "value": "q"}))
    assert resp == {"options": []}
    assert bridge.calls == []


def test_without_pairing_object_an_unset_owner_still_rejects_all(tmp_path):
    cfg = dataclasses.replace(load_config(env={"USERNAME": "me"}, config_dir=tmp_path), slack_owner_user_id="")
    bridge = GateBridge()
    app = build_app(cfg, bridge, authorize=authorize, process_before_response=True)
    assert body_json(dispatch(app, command("UX", "pair 123456")))["text"] == NOT_OWNER_TEXT
    assert bridge.calls == []


# --- live owner switch through the real bridge ----------------------------------------------------

def test_real_bridge_pairs_live_without_restart(tmp_path):
    run_setup(tmp_path, username="me")
    env_file = tmp_path / ".env"
    update_env_file(env_file, {"SLACK_BOT_TOKEN": "xoxb-1", "SLACK_APP_TOKEN": "xapp-1"})
    cfg = load_config(env={"USERNAME": "me"}, config_dir=tmp_path)
    assert cfg.needs_pairing and cfg.missing_slack_settings() == []
    herdr, transport, manager = FakeHerdr(), FakeTransport(), FakeManager()
    herdr.add_agent("w1:p1", "idle", name="coder")
    bridge = Bridge(cfg, herdr, StateStore(tmp_path / "s.json"), transport, ResultStore(tmp_path / "r"),
                    clock=Clock(), executor=SyncExecutor(), manager=manager, codex_home_dir=tmp_path)
    pairing = Pairing(cfg.state_dir, lambda u: update_env_file(env_file, {"SLACK_OWNER_USER_ID": u}),
                      lambda c, r: None, new_code=Codes())
    app = build_app(cfg, bridge, pairing=pairing, on_paired=lambda u: pairing.complete(bridge.activate_owner),
                    transport=transport, background=lambda fn: fn(), authorize=authorize,
                    process_before_response=True)
    pairing.begin()
    assert not manager.started and transport.dm_users == []  # nothing runs for nobody

    dispatch(app, command("UKIM", "pair 100001"))
    assert load_env_file(env_file)["SLACK_OWNER_USER_ID"] == "UKIM"
    assert bridge.cfg.slack_owner_user_id == "UKIM" and manager.started and transport.dm_users == ["UKIM"]
    welcome = transport.posts[-1]
    assert welcome["channel"] == "D-OWNER" and "Paired" in welcome["text"] and f"{CMD} list" in welcome["text"]
    assert not pairing_path(cfg.state_dir).exists()

    dispatch(app, command("UKIM", "list"))
    assert "coder" in json.dumps(transport.responses[-1]["blocks"])
    dispatch(app, command("UKIM", "pair 100001"))
    assert "Already paired" in transport.responses[-1]["text"]
    bridge.activate_owner("UOTHER")  # a second activation is ignored
    assert bridge.cfg.slack_owner_user_id == "UKIM"
    bridge.stop()


def test_bridge_activation_resumes_after_a_failed_step(tmp_path):
    cfg = dataclasses.replace(load_config(env={"USERNAME": "me"}, config_dir=tmp_path), slack_owner_user_id="")
    transport, manager = FakeTransport(), FakeManager()
    fails = {"dm": 1, "manager": 1}
    real_open = transport.open_dm

    def open_dm(user):
        if fails["dm"]:
            fails["dm"] -= 1
            raise RuntimeError("ratelimited")
        return real_open(user)

    def manager_start():
        if fails["manager"]:
            fails["manager"] -= 1
            raise RuntimeError("herdr busy")
        manager.started = True

    transport.open_dm = open_dm
    manager.start = manager_start
    bridge = Bridge(cfg, FakeHerdr(), StateStore(tmp_path / "s.json"), transport, ResultStore(tmp_path / "r"),
                    clock=Clock(), executor=SyncExecutor(), manager=manager, codex_home_dir=tmp_path)
    pairing = Pairing(tmp_path, lambda u: None, lambda c, r: None, new_code=Codes(), sleep=lambda s: None)
    pairing.begin()
    pairing.attempt("UKIM", "100001")
    assert pairing.complete(bridge.activate_owner) is True
    assert manager.started and manager.stopped  # the half-started manager was stopped before the retry
    assert transport.dm_users == ["UKIM"] and bridge._notify_thread is not None
    assert sum("Paired" in p["text"] for p in transport.posts) == 1  # one welcome
    bridge.stop()


def test_bridge_stop_in_pairing_mode_does_not_touch_the_manager(tmp_path):
    cfg = dataclasses.replace(load_config(env={"USERNAME": "me"}, config_dir=tmp_path), slack_owner_user_id="")
    manager = FakeManager()
    bridge = Bridge(cfg, FakeHerdr(), StateStore(tmp_path / "s.json"), FakeTransport(), ResultStore(tmp_path / "r"),
                    clock=Clock(), executor=SyncExecutor(), manager=manager, codex_home_dir=tmp_path)
    bridge.stop()
    assert not manager.stopped
    bridge.activate_owner("UKIM")  # after stop: ignored
    assert not manager.started


# --- plugin status -------------------------------------------------------------------------------

def test_plugin_status_shows_waiting_for_pairing(tmp_path, monkeypatch):
    from test_plugin import FakeCli

    (tmp_path / ".env").write_text("SLACK_BOT_TOKEN=xoxb-1\nSLACK_APP_TOKEN=xapp-1\nSLACK_OWNER_USER_ID=\n",
                                   encoding="utf-8")
    out = []
    ops = PluginOps(FakeCli(), tmp_path, out=out.append, env={})
    assert "slack: tokens set, not paired yet (start the bridge to get a pairing code)" in ops.status_lines()
    monkeypatch.setattr(ops, "running_pid", lambda: 1234)
    monkeypatch.setattr(ops, "verified_bridge", lambda: (None, "x"))
    assert "slack: not paired yet (see the bridge pane)" in ops.status_lines()
    Pairing(ops.state_dir, lambda u: None, lambda c, r: None).begin()
    lines = ops.status_lines()
    assert "slack: waiting for pairing (code in herdr-slack pane)" in lines
    assert not any(read_pairing(ops.state_dir)["code"] in line for line in lines)


def test_run_starts_in_pairing_mode_without_owner(monkeypatch, tmp_path, capsys):
    """`run` with tokens but no owner: Slack connects, a code is printed + notified, nothing else starts."""
    import logging
    import time as _time

    from herdr_slackbot import __main__ as main_mod
    from herdr_slackbot import plugin as P
    import herdr_slackbot.slack_app as slack_app

    (tmp_path / ".env").write_text("SLACK_BOT_TOKEN=xoxb-1\nSLACK_APP_TOKEN=xapp-1\nSLACK_OWNER_USER_ID=\n",
                                   encoding="utf-8")
    monkeypatch.setenv("HERDR_PLUGIN_CONFIG_DIR", "unused")
    monkeypatch.delenv("SLACK_OWNER_USER_ID", raising=False)
    monkeypatch.delenv("HERDR_PANE_ID", raising=False)

    class Client:
        def ping(self):
            return {}

    monkeypatch.setattr(main_mod.HerdrClient, "from_env", classmethod(lambda cls, *a, **k: Client()))
    notes = []
    monkeypatch.setattr(P.HerdrCli, "run", lambda self, *args, **k: notes.append(args) or "")
    built = {}

    def fake_build_app(cfg, bridge, **kwargs):
        built.update(kwargs, bridge=bridge)
        return object()

    class Handler:
        closed = False

        def close(self):
            Handler.closed = True

    seen_file = []

    def fake_stop_requested(state_dir, pid, start):
        seen_file.append(read_pairing(state_dir))
        return True

    monkeypatch.setattr(slack_app, "build_app", fake_build_app)
    monkeypatch.setattr(slack_app, "run_socket_mode", lambda app, token: Handler())
    monkeypatch.setattr(P, "stop_requested", fake_stop_requested)
    root = logging.getLogger()
    before = list(root.handlers)
    try:
        assert main_mod.main(["run", "--config-dir", str(tmp_path)]) == 0
    finally:
        for h in root.handlers[:]:
            if h not in before:
                root.removeHandler(h)
                h.close()
    pairing = built["pairing"]
    assert callable(built["on_paired"]) and built["transport"] is not None
    code = seen_file[0]["code"]
    out = capsys.readouterr().out
    assert f"Slack pairing code:  {code}" in out and f"/herdr-" in out
    deadline = _time.time() + 5
    while not notes and _time.time() < deadline:
        _time.sleep(0.01)
    (note,) = notes
    assert note[:3] == ("notification", "show", "herdr-slackbot") and f"Slack pairing code: {code}" in note
    assert built["bridge"]._notify_thread is None  # the bridge pipeline waits for an owner
    assert Handler.closed and not pairing_path(tmp_path / "state").exists()
    assert code not in (tmp_path / "state" / "bridge.log").read_text(encoding="utf-8")
