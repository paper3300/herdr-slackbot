"""Bolt wiring tested by dispatching Socket Mode payloads (no network)."""

import dataclasses
import json

import pytest
from slack_bolt.authorization import AuthorizeResult
from slack_bolt.request import BoltRequest

from herdr_slackbot import blocks as B
import time
from herdr_slackbot.config import load_config
from herdr_slackbot.slack_app import NOT_OWNER_TEXT, build_app, request_user

OWNER = "UOWNER"
STRANGER = "USTRANGER"


class RecordingBridge:
    def __init__(self):
        self.calls = []
        self.view_errors = None
        self.command_delay = 0.0

    def run_command(self, user, channel, text, trigger_id, response_url):
        if self.command_delay:
            time.sleep(self.command_delay)
        self.calls.append(("command", user, channel, text, trigger_id, response_url))

    def submit_new_view(self, view):
        self.calls.append(("submit_new", view["id"]))
        return self.view_errors

    def submit_send_view(self, view):
        self.calls.append(("submit_send", view["id"]))
        return self.view_errors

    def update_new_modal(self, action_id, view):
        self.calls.append(("update_new_modal", action_id, view["id"]))

    def toggle_mute(self, channel, message, value):
        self.calls.append(("mute", channel, message["ts"], value))

    def show_full(self, channel, message, value):
        self.calls.append(("show_full", channel, value))

    def handle_dm_message(self, channel, user, text, ts, thread_ts):
        self.calls.append(("dm", channel, user, text, ts, thread_ts))


def authorize(**kwargs):
    return AuthorizeResult(enterprise_id=None, team_id="T1", bot_token="xoxb-test", bot_id="B1", bot_user_id="UBOT")


@pytest.fixture
def setup(tmp_path):
    cfg = dataclasses.replace(load_config(env={"USERNAME": "me"}, config_dir=tmp_path), slack_owner_user_id=OWNER)
    bridge = RecordingBridge()
    app = build_app(cfg, bridge, authorize=authorize, process_before_response=True)
    return app, bridge, cfg


def dispatch(app, body):
    return app.dispatch(BoltRequest(body=body, mode="socket_mode"))


def command(user, text="list", cmd="/herdr-me"):
    return {"team_id": "T1", "user_id": user, "command": cmd, "text": text, "channel_id": "C1",
            "trigger_id": "TRIG", "api_app_id": "A1", "response_url": "https://hooks.slack/resp"}


def block_action(user, action_id, value="v", view=None):
    body = {"type": "block_actions", "team": {"id": "T1"}, "user": {"id": user}, "api_app_id": "A1",
            "trigger_id": "TRIG", "channel": {"id": "D1"}, "container": {"type": "message"},
            "message": {"ts": "1.0", "text": "t", "blocks": []},
            "actions": [{"action_id": action_id, "block_id": "b", "type": "button", "value": value,
                         "action_ts": "2.0"}]}
    if view is not None:
        body["view"] = view
        body.pop("message")
        body["container"] = {"type": "view", "view_id": view["id"]}
    return body


def view_submission(user, callback_id):
    return {"type": "view_submission", "team": {"id": "T1"}, "user": {"id": user}, "api_app_id": "A1",
            "view": {"id": "V1", "type": "modal", "callback_id": callback_id, "state": {"values": {}}, "private_metadata": "{}",
                     "hash": "h"}}


def message_event(user, text="hi", thread_ts=None, **extra):
    event = {"type": "message", "channel": "D1", "channel_type": "im", "user": user, "text": text, "ts": "5.5"}
    if thread_ts:
        event["thread_ts"] = thread_ts
    event.update(extra)
    return {"type": "event_callback", "team_id": "T1", "api_app_id": "A1", "event_id": "Ev1",
            "event_time": 1, "event": event}


def body_json(resp):
    return json.loads(resp.body) if resp.body else {}


# --- routing ------------------------------------------------------------------------------------

def test_command_acks_then_runs(setup):
    app, bridge, _ = setup
    resp = dispatch(app, command(OWNER, "list"))
    assert resp.status == 200 and body_json(resp) == {}  # empty ack; the answer goes to response_url
    assert bridge.calls == [("command", OWNER, "C1", "list", "TRIG", "https://hooks.slack/resp")]


def test_review1_command_acked_before_slow_work_with_default_bolt_processing(tmp_path):
    """Real bolt dispatch (not FaaS mode): the ack must not wait for Herdr work."""
    cfg = dataclasses.replace(load_config(env={"USERNAME": "me"}, config_dir=tmp_path), slack_owner_user_id=OWNER)
    bridge = RecordingBridge()
    bridge.command_delay = 3.4  # slower than Slack's 3s window
    app = build_app(cfg, bridge, authorize=authorize)
    t0 = time.monotonic()
    resp = dispatch(app, command(OWNER, "list"))
    assert resp.status == 200
    assert time.monotonic() - t0 < 1.0
    deadline = time.monotonic() + 6
    while not bridge.calls and time.monotonic() < deadline:
        time.sleep(0.05)
    assert bridge.calls and bridge.calls[0][3] == "list"


def test_other_slash_command_is_not_handled(setup):
    app, bridge, _ = setup
    dispatch(app, command(OWNER, "list", cmd="/other"))
    assert bridge.calls == []


@pytest.mark.parametrize("callback", [B.NEW_CALLBACK, B.SEND_CALLBACK])
def test_view_submission_errors_and_success(setup, callback):
    app, bridge, _ = setup
    bridge.view_errors = {"name": "bad"}
    resp = dispatch(app, view_submission(OWNER, callback))
    assert body_json(resp) == {"response_action": "errors", "errors": {"name": "bad"}}
    bridge.view_errors = None
    resp = dispatch(app, view_submission(OWNER, callback))
    assert resp.status == 200 and body_json(resp) == {}


@pytest.mark.parametrize("action_id", [B.ACTION_NEW_WS, B.ACTION_NEW_KIND])
def test_modal_dispatch_actions(setup, action_id):
    app, bridge, _ = setup
    dispatch(app, block_action(OWNER, action_id, view={"id": "V9", "type": "modal", "state": {"values": {}}}))
    assert bridge.calls == [("update_new_modal", action_id, "V9")]


def test_mute_and_show_full_actions(setup):
    app, bridge, _ = setup
    dispatch(app, block_action(OWNER, B.ACTION_MUTE, '{"s": "S1", "m": true}'))
    dispatch(app, block_action(OWNER, B.ACTION_SHOW_FULL, "abc"))
    assert bridge.calls == [("mute", "D1", "1.0", '{"s": "S1", "m": true}'), ("show_full", "D1", "abc")]


def test_dm_messages_routed_and_filtered(setup):
    app, bridge, _ = setup
    dispatch(app, message_event(OWNER, "go on", thread_ts="1.0"))
    dispatch(app, message_event(OWNER, "edited", subtype="message_changed"))
    dispatch(app, message_event(OWNER, "bot", bot_id="B1"))
    dispatch(app, message_event(OWNER, "in channel", channel_type="channel"))
    assert bridge.calls == [("dm", "D1", OWNER, "go on", "5.5", "1.0")]


# --- owner guard on every entry point -----------------------------------------------------------------

def test_guard_command(setup):
    app, bridge, _ = setup
    resp = dispatch(app, command(STRANGER, "list"))
    assert body_json(resp)["text"] == NOT_OWNER_TEXT
    assert body_json(resp)["response_type"] == "ephemeral"
    assert bridge.calls == []


@pytest.mark.parametrize("body", [
    block_action(STRANGER, B.ACTION_MUTE, '{"s": "S1", "m": true}'),
    block_action(STRANGER, B.ACTION_SHOW_FULL, "abc"),
    block_action(STRANGER, B.ACTION_NEW_WS, view={"id": "V1", "type": "modal", "state": {"values": {}}}),
    view_submission(STRANGER, B.NEW_CALLBACK),
    view_submission(STRANGER, B.SEND_CALLBACK),
    message_event(STRANGER, "let me in", thread_ts="1.0"),
    {"type": "block_suggestion", "team": {"id": "T1"}, "user": {"id": STRANGER}, "api_app_id": "A1",
     "action_id": "x", "block_id": "b", "value": "q"},
    {"type": "view_closed", "team": {"id": "T1"}, "user": {"id": STRANGER}, "api_app_id": "A1",
     "view": {"id": "V1", "type": "modal", "callback_id": B.NEW_CALLBACK}},
])
def test_guard_rejects_non_owner_everywhere(setup, body):
    app, bridge, _ = setup
    resp = dispatch(app, body)
    assert resp.status == 200
    assert bridge.calls == []
    if body.get("type") == "block_suggestion":
        assert body_json(resp) == {"options": []}


def test_guard_rejects_everything_when_owner_unset(tmp_path):
    cfg = dataclasses.replace(load_config(env={"USERNAME": "me"}, config_dir=tmp_path), slack_owner_user_id="")
    bridge = RecordingBridge()
    app = build_app(cfg, bridge, authorize=authorize, process_before_response=True)
    dispatch(app, command("", "list"))
    dispatch(app, command("UANY", "list"))
    assert bridge.calls == []


def test_request_user_shapes():
    assert request_user({"user_id": "U1", "command": "/x"}) == "U1"
    assert request_user({"type": "block_actions", "user": {"id": "U2"}}) == "U2"
    assert request_user({"event": {"user": "U3"}}) == "U3"
    assert request_user({"event": {"user": "UBOT", "bot_id": "B1"}}) is None
    assert request_user({}) is None


def test_real_bridge_through_bolt(tmp_path):
    from fakes import Clock, FakeHerdr, FakeManager, FakeTransport, SyncExecutor
    from herdr_slackbot.bridge import Bridge
    from herdr_slackbot.results import ResultStore
    from herdr_slackbot.state import StateStore

    cfg = dataclasses.replace(load_config(env={"USERNAME": "me"}, config_dir=tmp_path), slack_owner_user_id=OWNER)
    herdr, transport = FakeHerdr(), FakeTransport()
    herdr.add_agent("w1:p1", "working", name="coder")
    bridge = Bridge(cfg, herdr, StateStore(tmp_path / "s.json"), transport, ResultStore(tmp_path / "r"),
                    clock=Clock(), executor=SyncExecutor(), manager=FakeManager(), codex_home_dir=tmp_path)
    bridge.start()
    app = build_app(cfg, bridge, authorize=authorize, process_before_response=True)
    assert body_json(dispatch(app, command(OWNER, "list"))) == {}
    assert "coder" in json.dumps(transport.responses[-1]["blocks"])
    dispatch(app, command(OWNER, "send coder hi"))
    assert "busy" in transport.responses[-1]["text"]
    dispatch(app, command(OWNER, "new"))
    assert transport.views_opened and transport.views_opened[0]["trigger_id"] == "TRIG"
    assert transport.events.index("open_view") < transport.events.index("update_view")
    bridge.stop()
