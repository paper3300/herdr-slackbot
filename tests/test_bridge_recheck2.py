"""Regression tests for docs/review/M2-recheck2.md (N1, R9a, R9b, N4, N5)."""

import http.client
import json

import pytest

from fakes import Accepted
from herdr_slackbot.bridge import Bridge
from herdr_slackbot.results import ResultStore
from herdr_slackbot.slack_transport import (
    BLOCK_MARKER_PREFIX,
    MAX_LOOKUP_PAGES,
    POST_MARKER_EVENT,
    SlackLookupIncomplete,
    SlackTransientError,
    SlackUncertainError,
    WebClientTransport,
)
from herdr_slackbot.state import StateStore
from test_bridge import SHORT, env, make_env, results_posted, run, transition  # noqa: F401 (env fixture)


def roots(env):
    return [p for p in env.transport.posts if p["thread_ts"] is None]


# --- N1: a snapshot without a session never erases the pinned identity ---------------------------

def test_n1_absent_session_snapshot_then_same_terminal_replacement_is_rejected(env):
    orig_start = env.herdr.start_agent

    def start(*a, **k):
        result = orig_start(*a, **k)
        pane = result["agent"]["pane_id"]
        env.herdr.agents[pane]["agent_status"] = "unknown"
        result["agent"]["agent_status"] = "unknown"
        orig_find = env.herdr.find_agent
        calls = {"n": 0}

        def find(target):
            calls["n"] += 1
            agent = env.herdr.agents[pane]
            if calls["n"] == 1:  # idle, same terminal, session missing from the snapshot
                agent["agent_status"] = "idle"
                agent["agent_session"] = None
            elif calls["n"] == 2:  # replacement B in the same terminal
                agent["agent_session"] = {"value": "B"}
                agent["name"] = "intruder"
            return orig_find(target)
        env.herdr.find_agent = find
        return result

    env.herdr.start_agent = start
    run(env, "new Main prompt intended for A")
    assert env.herdr.prompts() == []
    assert env.state.get_thread("B") is None
    assert not any((e.get("pending_task") or {}) for e in env.state.all_threads().values())


def test_n1_observe_keeps_pinned_session_and_terminal(env):
    env.herdr.add_agent("w1:p1", "idle", session="A")
    env.herdr.agents["w1:p1"]["agent_session"] = None
    env.herdr.agents["w1:p1"].pop("terminal_id")
    expected = {"session": "A", "terminal_id": "term-w1:p1"}
    info, observed = env.bridge._observe("w1:p1", {"pane_id": "w1:p1"}, expected)
    assert observed and info["agent_session"] == {"value": "A"} and info["terminal_id"] == "term-w1:p1"


# --- R9a: the real SDK must not retry an accepted chat.postMessage by itself ----------------------

def _sdk_client(outcomes):
    from slack_sdk import WebClient

    client = WebClient(token="xoxb-test", base_url="https://slack.invalid/api/")
    attempts = []

    def perform(url, req):
        attempts.append(json.loads(req.data.decode("utf-8")) if req.data and req.data[:1] == b"{" else url)
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return {"status": 200, "headers": {}, "body": json.dumps(outcome)}

    client._perform_urllib_http_request_internal = perform
    return client, attempts


def test_r9a_sdk_retry_would_duplicate_without_the_transport():
    client, attempts = _sdk_client([http.client.RemoteDisconnected("lost"), {"ok": True, "ts": "2.0"}])
    client.chat_postMessage(channel="D", text="x")  # default SDK: retried internally
    assert len(attempts) == 2


def test_r9a_transport_disables_sdk_retries_so_the_bridge_sees_uncertain():
    client, attempts = _sdk_client([http.client.RemoteDisconnected("lost"), {"ok": True, "ts": "2.0"}])
    transport = WebClientTransport(client)
    assert client.retry_handlers == []
    with pytest.raises(SlackUncertainError):
        transport.post_message("D", "x", [{"type": "section", "text": {"type": "mrkdwn", "text": "x"}}],
                               op="result:task-A")
    assert len(attempts) == 1  # exactly one HTTP attempt; reconciliation is the bridge's decision


def test_r9a_connection_refused_is_retryable_not_uncertain():
    import urllib.error

    client, attempts = _sdk_client([urllib.error.URLError(ConnectionRefusedError())])
    with pytest.raises(SlackTransientError) as info:
        WebClientTransport(client).post_message("D", "x", op="o")
    assert not isinstance(info.value, SlackUncertainError) and len(attempts) == 1


# --- R9b: block_id fallback marker; invisible markers never mean "absent" --------------------------

class PagedWeb:
    """conversations.history/replies stub with pages; records chat.postMessage kwargs."""

    def __init__(self, pages):
        self.pages = list(pages)
        self.calls = []
        self.retry_handlers = ["default"]

    def chat_postMessage(self, **kw):
        self.calls.append(("post", kw))
        return {"ts": "9.9"}

    def _page(self, name, kw):
        self.calls.append((name, kw))
        page = self.pages.pop(0)
        if isinstance(page, Exception):
            raise page
        return page

    def conversations_history(self, **kw):
        return self._page("history", kw)

    def conversations_replies(self, **kw):
        return self._page("replies", kw)


def _msg(ts, op=None, via="metadata", bot=True):
    m = {"ts": ts, "blocks": [{"type": "section", "block_id": "abc"}]}
    if bot:
        m["bot_id"] = "B1"
    if op and via == "metadata":
        m["metadata"] = {"event_type": POST_MARKER_EVENT, "event_payload": {"op": op}}
    if op and via == "block":
        m["blocks"].append({"type": "context", "block_id": BLOCK_MARKER_PREFIX + op})
    return m


def test_r9b_post_carries_metadata_and_block_marker():
    web = PagedWeb([])
    blocks = [{"type": "header", "block_id": "keep"}, {"type": "section", "text": {"type": "mrkdwn", "text": "x"}}]
    WebClientTransport(web).post_message("D", "x", blocks, op="root:1")
    kw = web.calls[0][1]
    assert kw["metadata"]["event_payload"]["op"] == "root:1"
    assert [b.get("block_id") for b in kw["blocks"]] == ["keep", BLOCK_MARKER_PREFIX + "root:1"]
    assert "block_id" not in blocks[1]  # caller's blocks are not mutated


def test_r9b_block_marker_found_when_metadata_is_not_returned():
    web = PagedWeb([{"messages": [_msg("1.1", "other", "block"), _msg("1.2", "result:t", "block")]}])
    assert WebClientTransport(web).find_message("D", "result:t", "1.0") == "1.2"


def test_r9b_bot_messages_without_any_marker_are_unknown_not_absent():
    web = PagedWeb([{"messages": [_msg("1.1"), _msg("1.2")]}])  # markers stripped by Slack
    with pytest.raises(SlackLookupIncomplete, match="markers_not_visible"):
        WebClientTransport(web).find_message("D", "result:t", "1.0")


def test_r9b_complete_scan_with_visible_markers_is_absent():
    web = PagedWeb([{"messages": [_msg("1.1", "root:x"), _msg("1.2", "user", bot=False)]}])
    assert WebClientTransport(web).find_message("D", "result:t", "1.0") is None


def test_r9b_bridge_defers_instead_of_reposting_when_lookup_is_unknown(env):
    env.herdr.add_agent("w1:p1", "done", name="coder", session="S1")
    env.herdr.screens["w1:p1"] = SHORT
    env.state.upsert_thread("S1", channel="D-OWNER", thread_ts="1.0", pane_id="w1:p1",
                            pending_task={"task_id": "t1", "started_at": env.clock(), "seq0": 0,
                                          "working_announced": True})
    env.transport.fail_posts = [Accepted()]
    env.transport.fail_finds = [SlackLookupIncomplete("markers_not_visible")] * 100
    assert env.bridge.process_with_retry(transition(env, "w1:p1", "working", "done")) is False
    assert len(results_posted(env)) == 1  # never a second copy
    intents = env.state.get_thread("S1")["post_intents"]
    assert intents["result:t1"]["ts"] is None  # still unknown, kept for a later reconciliation


# --- N4: paginated lookups ------------------------------------------------------------------------

def test_n4_marker_on_a_later_page_is_found():
    web = PagedWeb([
        {"messages": [_msg("1.1", "root:x")], "has_more": True, "response_metadata": {"next_cursor": "c2"}},
        {"messages": [_msg("1.5", "result:t")], "has_more": False},
    ])
    assert WebClientTransport(web).find_message("D", "result:t", "1.0", oldest=10.0) == "1.5"
    assert [c[1].get("cursor") for c in web.calls] == [None, "c2"]
    assert web.calls[0][1]["oldest"] == "10.000000" and web.calls[0][1]["include_all_metadata"] is True


def test_n4_truncated_scan_is_unknown():
    web = PagedWeb([{"messages": [_msg("1.1", "root:x")], "has_more": True,
                     "response_metadata": {"next_cursor": f"c{i}"}} for i in range(MAX_LOOKUP_PAGES)])
    with pytest.raises(SlackLookupIncomplete, match="lookup_truncated"):
        WebClientTransport(web).find_message("D", "result:t")


def test_n4_failed_page_is_unknown():
    from slack_sdk.errors import SlackClientError

    web = PagedWeb([{"messages": [_msg("1.1", "root:x")], "has_more": True,
                     "response_metadata": {"next_cursor": "c"}}, SlackClientError("boom")])
    with pytest.raises(SlackTransientError):
        WebClientTransport(web).find_message("D", "result:t")


def test_n4_absence_right_after_the_attempt_defers(env):
    env.herdr.add_agent("w1:p1", "done", name="coder", session="S1")
    env.herdr.screens["w1:p1"] = SHORT
    env.state.upsert_thread("S1", channel="D-OWNER", thread_ts="1.0", pane_id="w1:p1",
                            pending_task={"task_id": "t1", "started_at": env.clock(), "seq0": 0,
                                          "working_announced": True})
    env.transport.fail_posts = [SlackUncertainError("ReadTimeout")]  # not stored by Slack
    assert env.bridge.process_with_retry(transition(env, "w1:p1", "working", "done"))
    assert len(results_posted(env)) == 1
    assert len(env.transport.finds) >= 2  # "not found" at +1s and +3s was not trusted yet


# --- N5: accepted posts survive an interruption before the local commit -----------------------------

def _fail_once(store, predicate):
    orig = store.upsert_thread
    state = {"failed": False}

    def upsert(key, **fields):
        if not state["failed"] and predicate(fields):
            state["failed"] = True
            raise OSError("disk full")
        return orig(key, **fields)
    store.upsert_thread = upsert


def test_n5_root_binding_write_fails_after_post(env):
    env.herdr.add_agent("w2:p3", "done", session="PC1")
    info = env.herdr.find_agent("w2:p3")
    _fail_once(env.state, lambda f: "thread_ts" in f)
    with pytest.raises(OSError):
        env.bridge._ensure_thread("PC1", info, "title", [], "pc")
    entry = env.bridge._ensure_thread("PC1", info, "title", [], "pc")
    assert len(roots(env)) == 1 and entry["thread_ts"] == roots(env)[0]["ts"]
    assert env.transport.finds == []  # the accepted ts was recorded; no lookup, no repost
    assert entry["post_intents"] == {} and entry.get("root_op") is None


def test_n5_result_commit_interrupted_then_restart_does_not_repost(tmp_path):
    env = make_env(tmp_path)
    env.herdr.add_agent("w1:p1", "done", name="coder", session="S1")
    env.herdr.screens["w1:p1"] = SHORT
    env.state.upsert_thread("S1", channel="D-OWNER", thread_ts="1.0", pane_id="w1:p1", agent_name="coder",
                            pending_task={"task_id": "t1", "started_at": env.clock(), "seq0": 0,
                                          "working_announced": True})
    _fail_once(env.state, lambda f: "last_result_seq" in f)
    with pytest.raises(OSError):
        env.bridge.handle_transition(transition(env, "w1:p1", "working", "done"))
    env.bridge.stop()
    assert len(results_posted(env)) == 1
    # New process: state reloaded from disk.
    state2 = StateStore(tmp_path / "state.json")
    persisted = state2.get_thread("S1")
    assert persisted["pending_task"] and persisted["post_intents"]["result:t1"]["ts"]
    bridge2 = Bridge(env.cfg, env.herdr, state2, env.transport, ResultStore(tmp_path / "results"),
                     clock=env.clock, sleep=env.clock.sleep, executor=env.bridge.executor,
                     manager=env.bridge.manager, claude_projects=tmp_path / "projects")
    bridge2.dm_channel = "D-OWNER"
    bridge2.handle_resume("S1", "t1")
    assert len(results_posted(env)) == 1 and env.transport.finds == []
    final = state2.get_thread("S1")
    assert final["pending_task"] is None and final["post_intents"] == {} and final["last_result_seq"] > 0


def test_n5_intent_is_marked_accepted_before_the_commit(env):
    env.herdr.add_agent("w1:p1", "done", name="coder", session="S1")
    env.herdr.screens["w1:p1"] = SHORT
    env.state.upsert_thread("S1", channel="D-OWNER", thread_ts="1.0", pane_id="w1:p1",
                            pending_task={"task_id": "t1", "started_at": env.clock(), "seq0": 0,
                                          "working_announced": True})
    seen = []
    orig = env.state.upsert_thread

    def spy(key, **fields):
        seen.append(sorted(fields))
        return orig(key, **fields)
    env.state.upsert_thread = spy
    env.bridge.handle_transition(transition(env, "w1:p1", "working", "done"))
    # intent added -> intent marked with ts -> one commit: result seq + task cleared + intent removed
    assert seen[-3:] == [["post_intents"], ["post_intents"], ["last_result_seq", "pending_task", "post_intents"]]


# --- marker-check (docs/LIVE_TEST.md) -----------------------------------------------------------

class MarkerSlack:
    """Minimal Slack: stores posts; `keep` decides which marker forms survive."""

    def __init__(self, keep=("metadata", "block")):
        self.keep = keep
        self.msgs = []
        self.n = 0

    def open_dm(self, user):
        return "D1"

    def post_message(self, channel, text, blocks=None, thread_ts=None, op=None):
        from herdr_slackbot.slack_transport import with_block_marker
        self.n += 1
        ts = f"100.{self.n:04d}"
        m = {"ts": ts, "bot_id": "B1", "thread_ts": thread_ts,
             "blocks": with_block_marker(blocks, op) if "block" in self.keep else blocks}
        if "metadata" in self.keep:
            m["metadata"] = {"event_type": POST_MARKER_EVENT, "event_payload": {"op": op}}
        self.msgs.append(m)
        return ts

    def recent_messages(self, channel, thread_ts=None, oldest=None):
        return [m for m in self.msgs if (m["thread_ts"] == thread_ts or m["ts"] == thread_ts)
                or (thread_ts is None and m["thread_ts"] is None)]

    def find_message(self, channel, op, thread_ts=None, oldest=None):
        web = PagedWeb([{"messages": self.recent_messages(channel, thread_ts, oldest)}])
        return WebClientTransport(web).find_message(channel, op, thread_ts, oldest)


@pytest.mark.parametrize("keep,code", [(("metadata", "block"), 0), (("block",), 0), (("metadata",), 0), ((), 1)])
def test_marker_check_reports_round_trip(keep, code):
    from herdr_slackbot.live_check import marker_check

    out = []
    assert marker_check(MarkerSlack(keep), "U1", out.append, sleep=lambda s: None) == code
    assert out[-1].startswith("PASS" if code == 0 else "FAIL")
    assert out[0].startswith("root :") and out[1].startswith("reply:")


# --- M2 recheck3: pagination signals are followed independently --------------------------------

@pytest.mark.parametrize("thread", ["1.0", None])
def test_recheck3_cursor_without_has_more_is_followed(thread):
    web = PagedWeb([
        {"messages": [_msg("1.1", "other")], "response_metadata": {"next_cursor": "page2"}},
        {"messages": [_msg("1.2", "result:t")]},
    ])
    assert WebClientTransport(web).find_message("D", "result:t", thread) == "1.2"
    assert [c[1].get("cursor") for c in web.calls] == [None, "page2"]
    assert {c[0] for c in web.calls} == {"replies" if thread else "history"}


@pytest.mark.parametrize("thread", ["1.0", None])
def test_recheck3_has_more_without_cursor_is_unknown(thread):
    web = PagedWeb([{"messages": [_msg("1.1", "other")], "has_more": True}])
    with pytest.raises(SlackLookupIncomplete, match="pagination_inconsistent"):
        WebClientTransport(web).find_message("D", "result:t", thread)


def test_recheck3_repeated_cursor_is_unknown():
    page = {"messages": [_msg("1.1", "other")], "response_metadata": {"next_cursor": "same"}}
    web = PagedWeb([dict(page), dict(page)])
    with pytest.raises(SlackLookupIncomplete, match="pagination_inconsistent"):
        WebClientTransport(web).find_message("D", "result:t")


def test_recheck3_cursor_pages_up_to_the_cap_are_unknown():
    web = PagedWeb([{"messages": [_msg("1.1", "other")], "response_metadata": {"next_cursor": f"c{i}"}}
                    for i in range(MAX_LOOKUP_PAGES)])
    with pytest.raises(SlackLookupIncomplete, match="lookup_truncated"):
        WebClientTransport(web).find_message("D", "result:t")


def test_recheck3_bridge_recovers_from_a_cursor_only_page_without_reposting(tmp_path):
    """End to end: accepted result + lost response, marker on page 2 behind a cursor-only page."""
    env = make_env(tmp_path)
    env.herdr.add_agent("w1:p1", "done", name="coder", session="S1")
    env.herdr.screens["w1:p1"] = SHORT
    env.state.upsert_thread("S1", channel="D-OWNER", thread_ts="1.0", pane_id="w1:p1",
                            pending_task={"task_id": "t1", "started_at": env.clock(), "seq0": 0,
                                          "working_announced": True})

    class Web(PagedWeb):
        def chat_postMessage(self, **kw):
            self.calls.append(("post", kw))
            raise ConnectionResetError("lost after accept")

    web = Web([
        {"messages": [_msg("1.1", "root:x")], "response_metadata": {"next_cursor": "page2"}},
        {"messages": [_msg("1.7", "result:t1")]},
    ])
    env.bridge.transport = WebClientTransport(web)
    env.bridge.dm_channel = "D-OWNER"
    assert env.bridge.process_with_retry(transition(env, "w1:p1", "working", "done"))
    posts = [c for c in web.calls if c[0] == "post"]
    reads = [c for c in web.calls if c[0] == "replies"]
    assert len(posts) == 1 and len(reads) == 2  # page 2 read; accepted result reused
    entry = env.state.get_thread("S1")
    assert entry["pending_task"] is None and entry["post_intents"] == {}
    env.bridge.stop()


# --- M2 recheck3 minor: marker-check reports failing steps instead of crashing ------------------

@pytest.mark.parametrize("fail_at", ["open_dm", "post_message", "recent_messages", "find_message"])
def test_recheck3_marker_check_reports_slack_errors(fail_at):
    from herdr_slackbot.live_check import marker_check
    from herdr_slackbot.slack_transport import SlackPermanentError

    slack = MarkerSlack()

    def boom(*a, **k):
        raise SlackPermanentError("missing_scope")
    setattr(slack, fail_at, boom)
    out = []
    assert marker_check(slack, "U1", out.append, sleep=lambda s: None) == 2
    assert any("missing_scope" in line for line in out)
    assert out[-1].startswith("ERROR")
    assert not any(line.startswith(("PASS", "FAIL")) for line in out)
