"""App Home tab: view builder, bridge handlers, debounced refresh, owner guard."""

import dataclasses

import pytest

from herdr_slackbot import blocks as B
from herdr_slackbot import slack_manifest as M
from herdr_slackbot.bridge import HomeRefresher
from herdr_slackbot.config import load_config
from herdr_slackbot.herdr_client import HerdrError
from herdr_slackbot.slack_app import build_app
from herdr_slackbot.slack_transport import SlackTransientError
from test_bridge import env, transition  # noqa: F401 (env fixture)
from test_slack_app import OWNER, STRANGER, RecordingBridge, authorize, block_action, dispatch


def _agent(pane, status="idle", name=None, ws="w1", title="Claude Code"):
    return {"pane_id": pane, "workspace_id": ws, "agent_status": status, "name": name, "agent": "claude",
            "terminal_title_stripped": title}


# --- view builder ------------------------------------------------------------------------------------

def test_home_view_layout_and_send_buttons():
    agents = [_agent("w1:p1", "idle", "coder"), _agent("w1:p2", "working"), _agent("w2:p1", "done", ws="w2"),
              _agent("w2:p2", "blocked", "stuck", ws="w2")]
    view = B.home_view("Herdr (me)", ["⏱ uptime 5m", "4 agents", "`/herdr-me`"], agents, {"w1": "Main", "w2": "Other"})
    assert view["type"] == "home"
    blocks = view["blocks"]
    assert blocks[0]["type"] == "header" and blocks[0]["text"]["text"] == "Herdr (me)"
    assert "uptime 5m" in blocks[1]["elements"][0]["text"] and "/herdr-me" in blocks[1]["elements"][0]["text"]
    actions = blocks[2]["elements"]
    assert [a["action_id"] for a in actions] == [B.ACTION_HOME_NEW, B.ACTION_HOME_SEND, B.ACTION_HOME_REFRESH]
    assert [a["text"]["text"] for a in actions] == ["➕ New Agent", "📤 Send", "🔄 Refresh"]
    rows = [b for b in blocks[4:] if b["type"] == "section"]
    texts = [r["text"]["text"] for r in rows]
    assert texts[0] == "*Main*" and texts[3] == "*Other*"
    buttons = {r["text"]["text"].split(" · ")[0]: (r.get("accessory") or {}).get("value") for r in rows[1:3] + rows[4:]}
    assert buttons["🟢 *coder*"] == "coder"  # idle -> [Send] with the agent's name
    assert buttons["✅ *w2:p1*"] == "w2:p1"  # done, unnamed -> pane id
    assert buttons["⏳ *w1:p2*"] is None and buttons["⚠️ *stuck*"] is None  # working / blocked: no button
    assert all(r["accessory"]["action_id"] == B.ACTION_HOME_SEND_AGENT for r in rows if r.get("accessory"))


def test_home_view_respects_the_100_block_limit():
    agents = [_agent(f"w1:p{i}", "idle", f"a{i}") for i in range(150)]
    blocks = B.home_view("H", ["x"], agents, {"w1": "Main"})["blocks"]
    assert len(blocks) == B.HOME_MAX_BLOCKS
    shown = sum(1 for b in blocks if b.get("accessory"))
    assert blocks[-1]["type"] == "context" and f"{150 - shown} more agents" in blocks[-1]["elements"][0]["text"]


def test_home_view_error_and_empty():
    err = B.home_view("H", ["x"], None, {}, error="server_not_running")["blocks"]
    assert "server_not_running" in err[-1]["text"]["text"]
    empty = B.home_view("H", ["x"], [], {})["blocks"]
    assert "No agents" in empty[-1]["text"]["text"]


# --- manifest -------------------------------------------------------------------------------------------

def test_manifest_enables_home_tab_and_keeps_messages_tab():
    m = M.build_manifest("/herdr-me", "Herdr (me)", "me")
    home = m["features"]["app_home"]
    assert home == {"home_tab_enabled": True, "messages_tab_enabled": True, "messages_tab_read_only_enabled": False}
    assert m["settings"]["event_subscriptions"]["bot_events"] == ["message.im", "app_home_opened"]
    assert M.required_scopes() == ["chat:write", "commands", "files:write", "im:history", "im:write"]  # unchanged


# --- bridge -------------------------------------------------------------------------------------------------

class Published:
    def __init__(self, transport):
        self.views = []
        transport.publish_view = lambda user, view: self.views.append((user, view))


def test_home_opened_publishes_owner_view(env):
    env.herdr.add_agent("w1:p1", "idle", name="coder")
    pub = Published(env.transport)
    env.bridge.handle_home_opened("UOWNER")
    (user, view), = pub.views
    assert user == "UOWNER" and view["type"] == "home"
    assert any("coder" in (b.get("text") or {}).get("text", "") for b in view["blocks"])


def test_home_publish_failures_are_logged_not_raised(env, caplog):
    def fail(user, view):
        raise SlackTransientError("ratelimited", 30)
    env.transport.publish_view = fail
    env.bridge.handle_home_opened("UOWNER")  # must not raise
    assert "Home tab refresh failed (ratelimited)" in caplog.text


def test_home_herdr_error_shows_in_view(env):
    pub = Published(env.transport)

    def broken():
        raise HerdrError("server_not_running", "down")
    env.herdr.list_agents = broken
    env.bridge.handle_home_opened("UOWNER")
    assert "server_not_running" in pub.views[0][1]["blocks"][-1]["text"]["text"]


def test_home_buttons_open_existing_modals(env):
    env.herdr.add_agent("w1:p1", "idle", name="coder")
    pub = Published(env.transport)
    env.bridge.handle_home_opened("UOWNER")
    env.bridge.home_action(B.ACTION_HOME_NEW, "T1")
    assert env.transport.views_opened[-1]["view"]["callback_id"] == B.NEW_CALLBACK
    env.bridge.home_action(B.ACTION_HOME_SEND_AGENT, "T2", "coder")
    assert env.transport.views_opened[-1]["trigger_id"] == "T2"
    send = env.transport.views_updated[-1]["view"]
    assert send["callback_id"] == B.SEND_CALLBACK
    assert send["blocks"][0]["element"]["initial_option"]["value"] == "coder"  # preselected
    env.bridge.home_action(B.ACTION_HOME_SEND, "T3")
    assert "initial_option" not in env.transport.views_updated[-1]["view"]["blocks"][0]["element"]
    env.bridge.home_action(B.ACTION_HOME_SEND_AGENT, "T4", "gone-agent")  # stale button: no preselection
    assert "initial_option" not in env.transport.views_updated[-1]["view"]["blocks"][0]["element"]
    env.bridge.home_action(B.ACTION_HOME_REFRESH, None)
    assert len(pub.views) == 2


def test_transitions_refresh_home_only_after_it_was_opened(env):
    requests = []
    env.bridge.home.request = lambda: requests.append(1)
    env.herdr.add_agent("w1:p1", "working", name="coder")
    env.bridge.enqueue_transition(transition(env, "w1:p1", "idle", "working"))
    assert requests == []  # the owner never opened Home: no publishes at all
    Published(env.transport)
    env.bridge.handle_home_opened("UOWNER")
    env.bridge.enqueue_transition(transition(env, "w1:p1", "working", "done"))
    assert requests == [1]


# --- debounce ---------------------------------------------------------------------------------------------------

class FakeTimer:
    created = []

    def __init__(self, delay, fn):
        self.delay, self.fn, self.cancelled, self.started = delay, fn, False, False
        FakeTimer.created.append(self)

    def start(self):
        self.started = True

    def cancel(self):
        self.cancelled = True


@pytest.fixture
def refresher():
    FakeTimer.created = []
    now = {"t": 100.0}
    published = []
    r = HomeRefresher(lambda: published.append(now["t"]), interval=5.0, settle=1.0, clock=lambda: now["t"],
                      timer_factory=FakeTimer)
    return r, now, published


def test_debounce_coalesces_a_burst_into_one_publish(refresher):
    r, now, published = refresher
    for _ in range(10):
        r.request()
    assert len(FakeTimer.created) == 1 and FakeTimer.created[0].delay == 1.0  # settle
    now["t"] += 1
    FakeTimer.created[0].fn()
    assert published == [101.0]


def test_debounce_keeps_at_least_the_interval_between_publishes(refresher):
    r, now, published = refresher
    r.request()
    now["t"] += 1
    FakeTimer.created[0].fn()
    now["t"] += 0.5
    r.request()
    assert FakeTimer.created[1].delay == pytest.approx(4.5)  # 5s after the previous publish
    r.request()  # coalesced into the scheduled one
    assert len(FakeTimer.created) == 2


def test_minor_manual_publish_resets_the_debounce_timer(refresher):
    r, now, published = refresher
    r.request()  # automatic refresh scheduled (settle)
    auto = FakeTimer.created[0]
    now["t"] += 0.5
    r.run_now()  # manual refresh: publishes now and replaces the scheduled one
    assert published == [100.5] and auto.cancelled
    r.request()
    assert FakeTimer.created[-1].delay == pytest.approx(5.0)  # spacing restarts from the manual publish
    auto.fn()  # a cancelled timer that still fires must not publish
    assert published == [100.5]


def test_minor_retry_after_is_honoured_and_the_refresh_retried_once(refresher):
    r, now, published = refresher
    calls = []

    def flaky():
        calls.append(now["t"])
        if len(calls) <= 2:
            raise SlackTransientError("ratelimited", 20)
        published.append(now["t"])
    r.publish = flaky
    r.run_now()  # fails with Retry-After 20
    retry = FakeTimer.created[-1]
    assert retry.delay == pytest.approx(20.0)  # one retry, after the cooldown
    now["t"] += 3
    r.run_now()  # manual refresh inside the cooldown: deferred, not published
    r.request()  # transitions inside the cooldown: coalesced too
    assert calls == [100.0] and len(FakeTimer.created) == 1
    now["t"] += 17
    retry.fn()  # retry fails again: cooldown again, but no endless retries
    assert calls == [100.0, 120.0]
    assert FakeTimer.created[-1] is retry  # nothing new scheduled
    now["t"] += 25
    r.request()  # a later transition works normally (after the cooldown)
    FakeTimer.created[-1].fn()
    assert published == [145.0]


def test_debounce_publish_errors_do_not_escape_and_close_cancels(refresher, caplog):
    r, now, _ = refresher
    r.publish = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
    r.request()
    FakeTimer.created[0].fn()  # logged, not raised
    assert "Home tab refresh failed" in caplog.text
    r.request()
    r.close()
    assert FakeTimer.created[-1].cancelled
    r.request()
    assert len(FakeTimer.created) == 2  # closed: no new timers


# --- owner guard (real bolt dispatch) ------------------------------------------------------------------------------

class HomeBridge(RecordingBridge):
    def handle_home_opened(self, user):
        self.calls.append(("home_opened", user))

    def home_action(self, action_id, trigger_id, value=None):
        self.calls.append(("home_action", action_id, trigger_id, value))


@pytest.fixture
def home_app(tmp_path):
    cfg = dataclasses.replace(load_config(env={"USERNAME": "me"}, config_dir=tmp_path), slack_owner_user_id=OWNER)
    bridge = HomeBridge()
    return build_app(cfg, bridge, authorize=authorize, process_before_response=True), bridge


def _home_event(user, tab="home"):
    return {"type": "event_callback", "team_id": "T1", "api_app_id": "A1", "event_id": "Ev2", "event_time": 1,
            "event": {"type": "app_home_opened", "user": user, "channel": "D1", "tab": tab, "event_ts": "1.0"}}


def test_home_opened_only_for_the_owner(home_app):
    app, bridge = home_app
    assert dispatch(app, _home_event(OWNER)).status == 200
    assert dispatch(app, _home_event(STRANGER)).status == 200
    assert dispatch(app, _home_event(OWNER, tab="messages")).status == 200
    assert bridge.calls == [("home_opened", OWNER)]  # strangers get nothing (no agent info)


@pytest.mark.parametrize("action_id", B.HOME_ACTIONS)
def test_home_buttons_are_acked_and_owner_guarded(home_app, action_id):
    app, bridge = home_app
    body = block_action(OWNER, action_id, value="coder")
    body.pop("message")
    body.pop("channel")
    body["container"] = {"type": "view", "view_id": "VH"}
    body["view"] = {"id": "VH", "type": "home", "state": {"values": {}}}
    resp = dispatch(app, body)
    assert resp.status == 200
    assert bridge.calls == [("home_action", action_id, "TRIG", "coder")]
    stranger = dict(body, user={"id": STRANGER})
    dispatch(app, stranger)
    assert len(bridge.calls) == 1


# --- H1: single in-flight publish, one pending refresh, nothing after shutdown ----------------------

def test_h1_slow_publishes_never_pile_up_and_stop_after_shutdown(tmp_path):
    import threading

    from test_bridge import make_env

    env = make_env(tmp_path, start=False)
    env.herdr.add_agent("w1:p1", "idle", name="coder")
    now = {"t": 1000.0}
    timers = []

    class Timer:
        def __init__(self, delay, fn):
            self.delay, self.fn, self.cancelled = delay, fn, False
            timers.append(self)

        def start(self):
            pass

        def cancel(self):
            self.cancelled = True

    bridge = env.bridge
    bridge.home = HomeRefresher(bridge.publish_home, clock=lambda: now["t"], timer_factory=Timer)
    bridge._home_user = "UOWNER"
    entered, release = threading.Event(), threading.Event()
    publishes = []

    def slow_publish(user, view):
        publishes.append(now["t"])
        if len(publishes) == 1:
            entered.set()
            release.wait(5)  # Slack is slow (e.g. a 30s timeout)
    env.transport.publish_view = slow_publish

    bridge.home.request()
    first = threading.Thread(target=timers[0].fn)
    first.start()
    assert entered.wait(5)
    fired = []
    for _ in range(6):  # transitions keep coming while the publish hangs
        now["t"] += 5
        bridge.home.request()
        fired += [t for t in timers if t not in fired]
    assert len(timers) == 1  # no new callbacks while one publish is in flight...
    assert bridge.home._pending is True  # ...just one coalesced pending refresh

    now["t"] += 1
    release.set()
    first.join(5)
    assert len(timers) == 2  # the pending refresh, scheduled from the actual completion
    assert timers[1].delay == pytest.approx(5.0)  # spaced from when the slow publish finished
    bridge.stop()
    assert timers[1].cancelled  # shutdown cancels the pending refresh
    timers[1].fn()  # even if its thread still fires
    assert len(publishes) == 1

    # a callback that got past the scheduler just before stop still does no work
    bridge.home._closed = False
    bridge.home._timer = None
    snapshots = []
    env.herdr.list_agents = lambda: snapshots.append(1) or []
    bridge.publish_home()
    assert snapshots == [] and len(publishes) == 1


def test_h1_manual_refresh_during_a_publish_becomes_the_pending_one(refresher):
    import threading

    r, now, published = refresher
    entered, release = threading.Event(), threading.Event()

    def slow():
        published.append(now["t"])
        if len(published) == 1:
            entered.set()
            release.wait(5)
    r.publish = slow
    t = threading.Thread(target=r.run_now)
    t.start()
    assert entered.wait(5)
    r.run_now()  # does not start a second publish in parallel
    r.run_now()
    assert len(published) == 1 and FakeTimer.created == []
    release.set()
    t.join(5)
    assert len(FakeTimer.created) == 1  # exactly one follow-up refresh
    FakeTimer.created[0].fn()
    assert len(published) == 2


def test_minor_preselected_agent_beyond_100_is_in_the_modal():
    agents = [_agent("wA:p1", "idle", "a-first", ws="wA")] + \
             [_agent(f"wB:p{i}", "idle", f"b{i}", ws="wB") for i in range(99)] + \
             [_agent("wA:p2", "idle", "a-last", ws="wA")]
    home = B.home_view("H", ["x"], agents, {"wA": "A", "wB": "B"})["blocks"]
    assert any((b.get("accessory") or {}).get("value") == "a-last" for b in home)  # shown near the top
    select = B.send_view(agents, {}, initial_target="a-last")["blocks"][0]["element"]
    assert len(select["options"]) == B.MAX_OPTIONS
    assert select["initial_option"]["value"] == "a-last"
    plain_select = B.send_view(agents, {}, initial_target="b3")["blocks"][0]["element"]
    assert [o["value"] for o in plain_select["options"]][:3] == ["a-first", "b0", "b1"]  # order kept otherwise
