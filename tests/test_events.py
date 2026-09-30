import itertools
import queue
import threading
import time

import pytest

from herdr_slackbot.events import (
    LIFECYCLE_SUBSCRIPTIONS,
    STATUS_CHANGED,
    SubscriptionManager,
    normalize_event_name,
    parse_event,
    plan_reconcile,
)
from herdr_slackbot.herdr_client import HerdrError


@pytest.mark.parametrize("raw,norm", [
    ("pane.agent_status_changed", "pane.agent_status_changed"),
    ("pane_agent_detected", "pane.agent_detected"),
    ("pane_created", "pane.created"),
    ("tab_closed", "tab.closed"),
    ("workspace_metadata_updated", "workspace.metadata_updated"),
    ("layout_updated", "layout.updated"),
    ("weird", "weird"),
    ("", ""),
])
def test_normalize_event_name(raw, norm):
    assert normalize_event_name(raw) == norm


def test_parse_event_real_envelopes():
    # Shapes captured from Herdr 0.8.2.
    status = parse_event({"data": {"agent": "claude", "agent_status": "done", "pane_id": "wD:p6",
                                   "workspace_id": "wD"}, "event": "pane.agent_status_changed"}, 1.0)
    assert status.kind == STATUS_CHANGED and status.pane_id == "wD:p6" and status.received_at == 1.0
    created = parse_event({"data": {"pane": {"pane_id": "w3:p24", "agent_status": "unknown"},
                                    "type": "pane_created"}, "event": "pane_created"})
    assert created.kind == "pane.created" and created.pane_id == "w3:p24"
    assert parse_event({"id": "s1", "result": {"type": "subscription_started"}}) is None


def test_plan_reconcile():
    add, drop = plan_reconcile({"a", "b"}, {"b", "c"})
    assert add == {"c"} and drop == {"a"}


# --- fakes ----------------------------------------------------------------------

class FakeStream:
    def __init__(self, subscriptions):
        self.subscriptions = subscriptions
        self.closed = False
        self._q = queue.Queue()

    def push(self, envelope):
        self._q.put(envelope)

    def __iter__(self):
        while True:
            item = self._q.get()
            if item is None or self.closed:
                return
            yield item

    def close(self):
        self.closed = True
        self._q.put(None)


class FakeClient:
    """Live agent table with a global, monotonic state_change_seq (as Herdr has)."""

    def __init__(self, *agents):
        self._seq = itertools.count(100)
        self.agents = {}
        for pane_id, status, *rest in agents:
            self.start(pane_id, status, *rest)
        self.streams: list[FakeStream] = []
        self.fail_subscribe = False
        self.fail_find = 0  # number of upcoming find_agent calls that fail
        self.on_subscribe = None  # hook(stream) run before subscribe() returns
        self.find_hook = None  # hook(target, result) run before find_agent() returns
        self.fail_list = 0  # number of upcoming list_agents calls that fail
        self.fail_find_for: dict = {}  # pane -> number of failing find_agent calls

    def start(self, pane_id, status, session=None, name=None, terminal=None):
        """session=False: an agent Herdr has not reported a session for yet (Codex)."""
        self.agents[pane_id] = {
            "pane_id": pane_id, "workspace_id": pane_id.split(":")[0], "agent_status": status,
            "agent": "claude", "name": name, "terminal_id": terminal or f"term-{pane_id}",
            "agent_session": None if session is False else {"value": session or "s-" + pane_id},
            "state_change_seq": next(self._seq),
        }

    def set_status(self, pane_id, status):
        self.agents[pane_id]["agent_status"] = status
        self.agents[pane_id]["state_change_seq"] = next(self._seq)

    def list_agents(self):
        if self.fail_list:
            self.fail_list -= 1
            raise HerdrError("unavailable", "list hiccup")
        return [dict(a) for a in self.agents.values()]

    def find_agent(self, target):
        if self.fail_find:
            self.fail_find -= 1
            raise HerdrError("unavailable", "pipe hiccup")
        if self.fail_find_for.get(target):
            self.fail_find_for[target] -= 1
            raise HerdrError("unavailable", "pipe hiccup")
        a = self.agents.get(target)
        result = dict(a) if a else None
        if self.find_hook:
            self.find_hook(target, result)  # may block after the snapshot was taken
        return result

    def subscribe(self, subscriptions):
        if self.fail_subscribe:
            raise HerdrError("unavailable", "boom")
        s = FakeStream(subscriptions)
        self.streams.append(s)
        if self.on_subscribe:
            self.on_subscribe(s)
        return s

    def pane_stream(self, pane_id):
        live = [s for s in self.streams if not s.closed and s.subscriptions[0].get("pane_id") == pane_id]
        return live[-1] if live else None

    def lifecycle_stream(self):
        return [s for s in self.streams if not s.closed and s.subscriptions == LIFECYCLE_SUBSCRIPTIONS][-1]


def status_env(pane_id, status):
    return {"event": "pane.agent_status_changed",
            "data": {"pane_id": pane_id, "workspace_id": pane_id.split(":")[0], "agent_status": status}}


def ev(envelope, at=1.0):
    return parse_event(envelope, at)


def make_manager(client):
    got = []
    mgr = SubscriptionManager(client, got.append, resync_interval=3600, retry_delay=None, clock=lambda: 42.0)
    return mgr, got


def simple(transitions):
    return [(t.pane_id, t.prev_status, t.status, t.session) for t in transitions]


def wait_queue(mgr, n, timeout=2.0):
    deadline = time.time() + timeout
    while mgr._queue.qsize() < n and time.time() < deadline:
        time.sleep(0.005)


# --- basic behaviour -----------------------------------------------------------------

def test_resync_subscribes_one_stream_per_agent_pane():
    client = FakeClient(("w1:p1", "idle"), ("w2:p3", "working"))
    mgr, got = make_manager(client)
    mgr.resync()
    assert mgr.subscribed_panes() == {"w1:p1", "w2:p3"}
    assert client.pane_stream("w1:p1").subscriptions == [{"type": STATUS_CHANGED, "pane_id": "w1:p1"}]
    assert mgr.known_status("w2:p3") == "working"
    assert mgr.known_session("w2:p3") == "s-w2:p3"
    assert got == []  # seeding from the snapshot is not a transition
    mgr.stop()


def test_status_events_become_transitions_with_dedupe():
    client = FakeClient(("w1:p1", "idle"))
    mgr, got = make_manager(client)
    mgr.resync()
    mgr.handle_event(ev(status_env("w1:p1", "idle")))  # nothing changed
    client.set_status("w1:p1", "working")
    mgr.handle_event(ev(status_env("w1:p1", "working")))
    mgr.handle_event(ev(status_env("w1:p1", "working")))  # duplicate
    client.set_status("w1:p1", "done")
    mgr.handle_event(ev(status_env("w1:p1", "done")))
    assert simple(got) == [("w1:p1", "idle", "working", "s-w1:p1"), ("w1:p1", "working", "done", "s-w1:p1")]
    assert got[-1].info["agent_status"] == "done" and not got[-1].synthetic
    mgr.stop()


def test_new_agent_detected_then_pane_closed():
    client = FakeClient()
    mgr, got = make_manager(client)
    mgr.resync()
    client.start("w1:p5", "idle", name="slack-1")
    mgr.handle_event(ev({"event": "pane_agent_detected", "data": {"agent": "claude", "pane_id": "w1:p5"}}))
    assert mgr.subscribed_panes() == {"w1:p5"}
    stream = client.pane_stream("w1:p5")
    del client.agents["w1:p5"]
    mgr.handle_event(ev({"event": "pane_closed", "data": {"pane_id": "w1:p5", "workspace_id": "w1"}}))
    assert mgr.subscribed_panes() == set() and stream.closed
    assert mgr.known_status("w1:p5") is None
    assert [(t.status, t.ended, t.session) for t in got] == [("unknown", True, "s-w1:p5")]
    mgr.stop()


def test_released_agent_does_not_subscribe():
    client = FakeClient()
    mgr, _ = make_manager(client)
    mgr.handle_event(ev({"event": "pane_agent_detected",
                         "data": {"pane_id": "w1:p2", "released": True, "final_status": "unknown"}}))
    assert mgr.subscribed_panes() == set()


def test_replayed_detected_for_closed_pane_is_ignored():
    client = FakeClient(("w1:p1", "idle"))
    mgr, _ = make_manager(client)
    mgr.resync()
    mgr.handle_event(ev({"event": "pane_agent_detected", "data": {"agent": "claude", "pane_id": "w9:p99"}}))
    mgr.handle_event(ev({"event": "pane_closed", "data": {"pane_id": "w8:p1", "workspace_id": "w8"}}))
    assert mgr.subscribed_panes() == {"w1:p1"}
    mgr.stop()


def test_pane_moved_follows_new_pane_id():
    client = FakeClient(("w1:p1", "idle"))
    mgr, _ = make_manager(client)
    mgr.resync()
    info = client.agents.pop("w1:p1")
    client.agents["w2:p9"] = dict(info, pane_id="w2:p9", workspace_id="w2")
    mgr.handle_event(ev({"event": "pane_moved", "data": {
        "previous_pane_id": "w1:p1", "previous_workspace_id": "w1", "previous_tab_id": "w1:t1",
        "pane": {"pane_id": "w2:p9", "workspace_id": "w2"}}}))
    assert mgr.subscribed_panes() == {"w2:p9"}
    mgr.stop()


def test_resync_drops_gone_agents_and_reopens_dead_streams_with_synthetic_transition():
    client = FakeClient(("w1:p1", "working"), ("w1:p2", "idle"))
    mgr, got = make_manager(client)
    mgr.resync()
    del client.agents["w1:p2"]
    client.pane_stream("w1:p1").close()
    client.set_status("w1:p1", "done")
    mgr.resync()
    assert mgr.subscribed_panes() == {"w1:p1"}
    assert client.pane_stream("w1:p1") is not None
    assert sorted((t.pane_id, t.prev_status, t.status, t.synthetic, t.ended) for t in got) == [
        ("w1:p1", "working", "done", True, False),
        ("w1:p2", "idle", "unknown", True, True),
    ]
    mgr.stop()


def test_subscribe_failure_is_retried_on_next_resync():
    client = FakeClient(("w1:p1", "idle"))
    client.fail_subscribe = True
    mgr, _ = make_manager(client)
    mgr.resync()
    assert mgr.subscribed_panes() == set() and "w1:p1" in mgr.dirty_panes()
    client.fail_subscribe = False
    mgr.resync()
    assert mgr.subscribed_panes() == {"w1:p1"}
    mgr.stop()


# --- review regressions ------------------------------------------------------------

def test_review2_buffered_events_older_than_snapshot_are_ignored():
    """Reconnect: blocked and done happen between opening the stream and the snapshot."""
    client = FakeClient(("w1:p1", "working"))
    mgr, got = make_manager(client)
    mgr.resync()
    client.pane_stream("w1:p1").close()  # stream dies

    def buffer_changes(stream):
        client.set_status("w1:p1", "blocked")
        stream.push(status_env("w1:p1", "blocked"))
        client.set_status("w1:p1", "done")
        stream.push(status_env("w1:p1", "done"))

    client.on_subscribe = buffer_changes
    mgr.resync()  # reopens the stream, then looks up live state (done)
    wait_queue(mgr, 2)
    mgr.drain()  # buffered blocked/done are processed after the snapshot
    assert [(t.prev_status, t.status) for t in got] == [("working", "done")]
    mgr.stop()


def test_review3_queued_event_not_attributed_to_replacement_session():
    client = FakeClient(("w1:p1", "working", "A"))
    mgr, got = make_manager(client)
    mgr.resync()
    client.set_status("w1:p1", "done")
    queued = ev(status_env("w1:p1", "done"))
    # Session B replaces A in the same pane before the queued event is dispatched.
    client.start("w1:p1", "working", "B")
    mgr.handle_event(queued)
    assert [(t.session, t.prev_status, t.status, t.ended) for t in got] == [("A", "working", "unknown", True)]
    assert not any(t.session == "B" for t in got)
    assert mgr.known_session("w1:p1") == "B" and mgr.known_status("w1:p1") == "working"
    # B's own later completion is attributed to B.
    client.set_status("w1:p1", "done")
    mgr.handle_event(ev(status_env("w1:p1", "done")))
    assert simple(got[-1:]) == [("w1:p1", "working", "done", "B")]
    mgr.stop()


def test_review3_agent_detected_on_subscribed_pane_checks_session():
    client = FakeClient(("w1:p1", "idle", "A"))
    mgr, got = make_manager(client)
    mgr.resync()
    client.start("w1:p1", "idle", "B")
    mgr.handle_event(ev({"event": "pane_agent_detected", "data": {"agent": "claude", "pane_id": "w1:p1"}}))
    assert mgr.known_session("w1:p1") == "B"
    assert [(t.session, t.ended) for t in got] == [("A", True)]
    mgr.stop()


@pytest.mark.parametrize("envelope", [
    {"event": "pane_exited", "data": {"pane_id": "w1:p1", "workspace_id": "w1"}},
    {"event": "pane_closed", "data": {"pane_id": "w1:p1", "workspace_id": "w1"}},
    {"event": "pane_moved", "data": {"previous_pane_id": "w1:p1", "previous_workspace_id": "w1",
                                     "previous_tab_id": "w1:t1", "pane": {"pane_id": "w5:p50"}}},
])
def test_review4_replayed_destructive_event_keeps_live_subscription(envelope):
    client = FakeClient(("w1:p1", "working"))
    mgr, got = make_manager(client)
    mgr.resync()
    mgr.handle_event(ev(envelope))  # stale history: the pane still hosts the same agent
    assert "w1:p1" in mgr.subscribed_panes()
    assert mgr.known_status("w1:p1") == "working"
    assert got == []
    client.set_status("w1:p1", "done")
    mgr.handle_event(ev(status_env("w1:p1", "done")))
    assert [(t.prev_status, t.status) for t in got] == [("working", "done")]
    mgr.stop()


def test_review4_destructive_event_with_failed_lookup_keeps_state():
    client = FakeClient(("w1:p1", "working"))
    mgr, got = make_manager(client)
    mgr.resync()
    client.fail_find = 1
    mgr.handle_event(ev({"event": "pane_exited", "data": {"pane_id": "w1:p1", "workspace_id": "w1"}}))
    assert "w1:p1" in mgr.subscribed_panes() and mgr.known_status("w1:p1") == "working"
    assert "w1:p1" in mgr.dirty_panes()
    mgr.stop()


def test_review5_failed_lookup_recovers_exactly_one_completion():
    client = FakeClient(("w1:p1", "working", "S1"))
    mgr, got = make_manager(client)
    mgr.resync()
    client.set_status("w1:p1", "done")
    client.fail_find = 1
    mgr.handle_event(ev(status_env("w1:p1", "done")))
    assert got == [] and mgr.known_status("w1:p1") == "working"
    assert "w1:p1" in mgr.dirty_panes()
    mgr.handle_event(ev(status_env("w1:p1", "done")))  # repeat after recovery
    mgr.resync()
    mgr.handle_event(ev(status_env("w1:p1", "done")))
    assert simple(got) == [("w1:p1", "working", "done", "S1")]
    assert mgr.dirty_panes() == set()


def test_review5_retry_timer_rechecks_dirty_pane():
    client = FakeClient(("w1:p1", "working", "S1"))
    got = []
    mgr = SubscriptionManager(client, got.append, resync_interval=3600, retry_delay=0.05)
    mgr.resync()
    client.set_status("w1:p1", "done")
    client.fail_find = 1
    mgr.handle_event(ev(status_env("w1:p1", "done")))
    wait_queue(mgr, 1)
    mgr.drain()
    assert simple(got) == [("w1:p1", "working", "done", "S1")]
    mgr.stop()


def test_review6_stop_during_open_leaves_no_stream():
    client = FakeClient(("w1:p1", "idle"))
    opening, release = threading.Event(), threading.Event()

    def slow_open(stream):
        if stream.subscriptions[0].get("pane_id") == "w1:p1":
            opening.set()
            release.wait(2)

    client.on_subscribe = slow_open
    mgr, _ = make_manager(client)
    t = threading.Thread(target=mgr.resync)
    t.start()
    assert opening.wait(2)
    mgr.stop()
    release.set()
    t.join(2)
    assert mgr.subscribed_panes() == set()
    assert all(s.closed for s in client.streams)


def test_review6_stop_start_joins_old_workers_and_drops_old_callbacks():
    client = FakeClient(("w1:p1", "idle"))
    got = []
    mgr = SubscriptionManager(client, got.append, resync_interval=3600, retry_delay=None)
    mgr.start()
    old_stream = client.pane_stream("w1:p1")
    old_workers = list(mgr._workers)
    mgr.stop()
    assert all(not w.is_alive() for w in old_workers)
    assert old_stream.closed and all(s.closed for s in client.streams)
    mgr.start()
    try:
        client.set_status("w1:p1", "working")
        old_stream.push(status_env("w1:p1", "working"))  # old generation: must be ignored
        time.sleep(0.1)
        assert got == []
        client.pane_stream("w1:p1").push(status_env("w1:p1", "working"))
        deadline = time.time() + 2
        while not got and time.time() < deadline:
            time.sleep(0.01)
        assert [(t.prev_status, t.status) for t in got] == [("idle", "working")]
        live = [s for s in client.streams if not s.closed]
        assert len(live) == 2  # one pane stream + lifecycle
    finally:
        mgr.stop()


def test_threaded_end_to_end():
    client = FakeClient(("w1:p1", "idle"))
    got = []
    done = threading.Event()

    def on_transition(t):
        got.append(t)
        if t.status == "done":
            done.set()

    mgr = SubscriptionManager(client, on_transition, resync_interval=3600)
    mgr.start()
    try:
        stream = client.pane_stream("w1:p1")
        client.set_status("w1:p1", "working")
        stream.push(status_env("w1:p1", "working"))
        assert _wait(lambda: len(got) == 1)
        client.set_status("w1:p1", "done")
        stream.push(status_env("w1:p1", "done"))
        assert done.wait(2)
        assert [t.status for t in got] == ["working", "done"]
        client.start("w1:p7", "idle")
        client.lifecycle_stream().push({"event": "pane_agent_detected",
                                        "data": {"agent": "claude", "pane_id": "w1:p7", "workspace_id": "w1"}})
        assert _wait(lambda: "w1:p7" in mgr.subscribed_panes())
    finally:
        mgr.stop()
    assert all(s.closed for s in client.streams)


def _wait(cond, timeout=2.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(0.01)
    return False


# --- recheck regressions (docs/review/M1-recheck.md) ----------------------------------------------

from herdr_slackbot.notify import Action, decide_notification  # noqa: E402

TASK = {"started_at": 1.0, "working_announced": False}


def test_recheck_r6_old_dispatcher_cannot_touch_restarted_manager():
    client = FakeClient(("w1:p1", "working", "A"))
    got = []
    mgr = SubscriptionManager(client, got.append, resync_interval=3600, retry_delay=None, join_timeout=0.1)
    mgr.start()
    entered, release = threading.Event(), threading.Event()

    def block_dispatcher(target, result):
        if threading.current_thread().name == "herdr-events-dispatch" and not entered.is_set():
            entered.set()
            release.wait(5)

    client.find_hook = block_dispatcher
    client.set_status("w1:p1", "done")  # the blocked lookup holds A's done snapshot
    client.pane_stream("w1:p1").push(status_env("w1:p1", "done"))
    assert entered.wait(2)
    mgr.stop()  # join times out: the old dispatcher is still inside agent.get
    client.find_hook = None
    client.start("w1:p1", "working", "B")
    mgr.start()
    try:
        assert mgr.known_session("w1:p1") == "B"
        got.clear()
        release.set()
        time.sleep(0.3)
        assert mgr.known_session("w1:p1") == "B" and mgr.known_status("w1:p1") == "working"
        assert got == []  # no callback from the old generation
        client.set_status("w1:p1", "done")
        client.pane_stream("w1:p1").push(status_env("w1:p1", "done"))
        assert _wait(lambda: got)
        assert simple(got) == [("w1:p1", "working", "done", "B")]
    finally:
        release.set()
        mgr.stop()


@pytest.mark.parametrize("start,final", [("idle", "done"), ("done", "done"), ("idle", "idle")])
def test_recheck_n1_batched_fast_task_is_not_lost(start, final):
    client = FakeClient(("w1:p1", start, "S"))
    mgr, got = make_manager(client)
    mgr.resync()
    client.set_status("w1:p1", "working")
    queued = [ev(status_env("w1:p1", "working"))]
    client.set_status("w1:p1", final)
    queued.append(ev(status_env("w1:p1", final)))
    for e in queued:  # both dispatch after the task already settled
        mgr.handle_event(e)
    assert [(t.prev_status, t.status) for t in got] == [(start, "working"), ("working", final)]
    decisions = [decide_notification(t.prev_status, t.status, pending_task=TASK, muted=True) for t in got]
    assert [d.action for d in decisions] == [Action.STARTED, Action.COMPLETED]
    pc = [decide_notification(t.prev_status, t.status, pending_task=None, muted=False).action for t in got]
    assert pc[-1] == (Action.COMPLETED if final == "done" else Action.NONE)
    mgr.stop()


def test_recheck_n1_blocked_hint_does_not_raise_stale_alert():
    client = FakeClient(("w1:p1", "idle", "S"))
    mgr, got = make_manager(client)
    mgr.resync()
    client.set_status("w1:p1", "blocked")
    e1 = ev(status_env("w1:p1", "blocked"))
    client.set_status("w1:p1", "done")
    mgr.handle_event(e1)
    mgr.handle_event(ev(status_env("w1:p1", "done")))
    assert [(t.prev_status, t.status) for t in got] == [("idle", "working"), ("working", "done")]
    mgr.stop()


def test_recheck_n1_threaded_back_to_back_events():
    client = FakeClient(("w1:p1", "idle", "S"))
    got = []
    mgr = SubscriptionManager(client, got.append, resync_interval=3600, retry_delay=None)
    mgr.start()
    try:
        stream = client.pane_stream("w1:p1")
        client.set_status("w1:p1", "working")
        stream.push(status_env("w1:p1", "working"))
        client.set_status("w1:p1", "done")  # no wait for the working callback
        stream.push(status_env("w1:p1", "done"))
        assert _wait(lambda: got and got[-1].status == "done")
        assert ("working", "done") in [(t.prev_status, t.status) for t in got]
    finally:
        mgr.stop()


def _move(client, old, new):
    info = client.agents.pop(old)
    client.agents[new] = dict(info, pane_id=new, workspace_id=new.split(":")[0])


def moved_env(old, new):
    return {"event": "pane_moved", "data": {"previous_pane_id": old, "previous_workspace_id": old.split(":")[0],
                                            "previous_tab_id": "t", "pane": {"pane_id": new}}}


def test_recheck_n2_move_keeps_session_and_pending_task():
    client = FakeClient(("w1:p1", "working", "S"))
    mgr, got = make_manager(client)
    mgr.resync()
    _move(client, "w1:p1", "w2:p9")
    mgr.handle_event(ev(moved_env("w1:p1", "w2:p9")))
    assert got == []  # no ended transition for a move
    assert mgr.subscribed_panes() == {"w2:p9"}
    assert (mgr.known_session("w2:p9"), mgr.known_status("w2:p9")) == ("S", "working")
    assert mgr.known_status("w1:p1") is None
    client.set_status("w2:p9", "idle")
    mgr.handle_event(ev(status_env("w2:p9", "idle")))
    assert simple(got) == [("w2:p9", "working", "idle", "S")]
    decision = decide_notification(got[0].prev_status, got[0].status, pending_task=TASK, muted=True)
    assert decision.action == Action.COMPLETED
    mgr.stop()


def test_recheck_n2_move_seen_by_resync():
    client = FakeClient(("w1:p1", "working", "S"))
    mgr, got = make_manager(client)
    mgr.resync()
    _move(client, "w1:p1", "w2:p9")
    client.set_status("w2:p9", "done")
    mgr.resync()
    assert [(t.pane_id, t.prev_status, t.status, t.ended) for t in got] == [("w2:p9", "working", "done", False)]
    assert mgr.subscribed_panes() == {"w2:p9"}
    mgr.stop()


def test_recheck_n2_destination_detected_before_move_event():
    client = FakeClient(("w1:p1", "working", "S"))
    mgr, got = make_manager(client)
    mgr.resync()
    _move(client, "w1:p1", "w2:p9")
    mgr.handle_event(ev({"event": "pane_agent_detected", "data": {"agent": "claude", "pane_id": "w2:p9"}}))
    mgr.handle_event(ev({"event": "pane_closed", "data": {"pane_id": "w1:p1", "workspace_id": "w1"}}))
    assert got == []
    assert mgr.known_status("w2:p9") == "working"
    mgr.stop()


# --- recheck2 regressions (docs/review/M1-recheck2.md) ---------------------------------------------

def test_recheck2_r6_old_lookup_does_not_clear_new_runs_dirty_flag():
    client = FakeClient(("w1:p1", "working", "A"))
    got = []
    mgr = SubscriptionManager(client, got.append, resync_interval=3600, retry_delay=None, join_timeout=0.1)
    mgr.start()
    entered, release = threading.Event(), threading.Event()

    def block_dispatcher(target, result):
        if threading.current_thread().name == "herdr-events-dispatch" and not entered.is_set():
            entered.set()
            release.wait(5)

    client.find_hook = block_dispatcher
    client.set_status("w1:p1", "done")
    client.pane_stream("w1:p1").push(status_env("w1:p1", "done"))
    assert entered.wait(2)
    mgr.stop()
    client.find_hook = None
    client.start("w1:p1", "working", "B")
    mgr.start()
    try:
        # New run: B finishes but its lookup fails -> pane is dirty (retry pending).
        client.set_status("w1:p1", "done")
        client.fail_find_for["w1:p1"] = 1
        client.pane_stream("w1:p1").push(status_env("w1:p1", "done"))
        assert _wait(lambda: "w1:p1" in mgr.dirty_panes())
        release.set()  # the old run's lookup completes now
        time.sleep(0.3)
        assert "w1:p1" in mgr.dirty_panes()  # still scheduled for retry
        got.clear()
        mgr._queue.put(__import__("herdr_slackbot.events", fromlist=["_Recheck"])._Recheck("w1:p1"))
        assert _wait(lambda: got)
        assert simple(got) == [("w1:p1", "working", "done", "B")]
    finally:
        release.set()
        mgr.stop()


def test_recheck2_n1_resync_does_not_overtake_queued_events():
    client = FakeClient(("w1:p1", "idle", "S"))
    mgr, got = make_manager(client)
    mgr.resync()
    stream = client.pane_stream("w1:p1")
    client.set_status("w1:p1", "working")
    stream.push(status_env("w1:p1", "working"))
    client.set_status("w1:p1", "done")
    stream.push(status_env("w1:p1", "done"))
    wait_queue(mgr, 2)
    mgr.resync()  # runs while both events are still queued
    assert got == []
    mgr.drain()
    assert [(t.prev_status, t.status) for t in got] == [("idle", "working"), ("working", "done")]
    decisions = [decide_notification(t.prev_status, t.status, pending_task=TASK, muted=True).action for t in got]
    assert decisions == [Action.STARTED, Action.COMPLETED]
    mgr.stop()


def test_recheck2_n1_failed_lookup_on_the_only_active_hint():
    client = FakeClient(("w1:p1", "idle", "S"))
    mgr, got = make_manager(client)
    mgr.resync()
    client.set_status("w1:p1", "working")
    e_working = ev(status_env("w1:p1", "working"))
    client.set_status("w1:p1", "done")
    client.fail_find_for["w1:p1"] = 1
    mgr.handle_event(e_working)  # lookup fails: activity must survive
    assert got == [] and "w1:p1" in mgr.dirty_panes()
    mgr.handle_event(ev(status_env("w1:p1", "done")))
    assert [(t.prev_status, t.status) for t in got] == [("idle", "working"), ("working", "done")]
    mgr.recheck("w1:p1")  # the scheduled retry finds nothing new: no duplicate
    mgr.resync()
    assert len(got) == 2
    mgr.stop()


def _moved_working(client, mgr):
    mgr.resync()
    _move(client, "w1:p1", "w2:p9")


def test_recheck2_n2_destination_lookup_fails_session_not_ended():
    client = FakeClient(("w1:p1", "working", "S"))
    mgr, got = make_manager(client)
    _moved_working(client, mgr)
    client.fail_find_for["w2:p9"] = 1
    mgr.handle_event(ev(moved_env("w1:p1", "w2:p9")))
    assert not any(t.ended for t in got)
    assert mgr.known_session("w2:p9") == "S" and mgr.known_status("w2:p9") == "working"
    client.set_status("w2:p9", "idle")
    mgr.handle_event(ev(status_env("w2:p9", "idle")))
    assert simple(got) == [("w2:p9", "working", "idle", "S")]
    mgr.stop()


def test_recheck2_n2_destination_subscribe_fails_session_not_ended():
    client = FakeClient(("w1:p1", "working", "S"))
    mgr, got = make_manager(client)
    _moved_working(client, mgr)
    client.fail_subscribe = True
    mgr.handle_event(ev(moved_env("w1:p1", "w2:p9")))
    assert not any(t.ended for t in got)
    client.fail_subscribe = False
    client.set_status("w2:p9", "done")
    mgr.resync()
    assert simple(got) == [("w2:p9", "working", "done", "S")]
    mgr.stop()


def test_recheck2_n2_old_pane_event_before_move_event():
    client = FakeClient(("w1:p1", "working", "S"))
    mgr, got = make_manager(client)
    _moved_working(client, mgr)
    mgr.handle_event(ev(status_env("w1:p1", "idle")))  # buffered event of the old pane first
    assert not any(t.ended for t in got)
    mgr.handle_event(ev(moved_env("w1:p1", "w2:p9")))
    client.set_status("w2:p9", "done")
    mgr.handle_event(ev(status_env("w2:p9", "done")))
    assert simple(got) == [("w2:p9", "working", "done", "S")]
    mgr.stop()


def test_recheck2_n2_source_reused_before_resync():
    client = FakeClient(("w1:p1", "working", "S"))
    mgr, got = make_manager(client)
    _moved_working(client, mgr)
    client.start("w1:p1", "idle", "B")  # the old pane now hosts B; listed before S's new pane
    client.agents = {"w1:p1": client.agents["w1:p1"], "w2:p9": client.agents["w2:p9"]}
    mgr.resync()
    assert not any(t.ended for t in got)
    assert mgr.known_session("w1:p1") == "B" and mgr.known_session("w2:p9") == "S"
    assert mgr.known_status("w2:p9") == "working"
    mgr.stop()


def test_recheck2_n2_list_failure_parks_decision_until_resync():
    client = FakeClient(("w1:p1", "working", "S"))
    mgr, got = make_manager(client)
    mgr.resync()
    del client.agents["w1:p1"]  # really gone
    client.fail_list = 1
    mgr.handle_event(ev({"event": "pane_closed", "data": {"pane_id": "w1:p1", "workspace_id": "w1"}}))
    assert got == []  # not ended on a guess
    mgr.resync()
    assert [(t.session, t.ended) for t in got] == [("S", True)]
    mgr.stop()


def test_recheck2_n2_provisional_agent_moves_and_gets_session_at_completion():
    client = FakeClient()
    client.start("w1:p1", "working", session=False, terminal="X")
    mgr, got = make_manager(client)
    mgr.resync()
    info = client.agents.pop("w1:p1")
    client.agents["w2:p9"] = dict(info, pane_id="w2:p9", workspace_id="w2", agent_status="idle",
                                  agent_session={"value": "REAL"}, state_change_seq=999)
    mgr.handle_event(ev(moved_env("w1:p1", "w2:p9")))
    assert [(t.pane_id, t.prev_status, t.status, t.session) for t in got] == [("w2:p9", "working", "idle", "REAL")]
    assert got[0].info["terminal_id"] == "X"
    mgr.stop()
