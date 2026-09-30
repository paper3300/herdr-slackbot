"""SubscriptionManager -> Bridge integration (fake Herdr streams, fake Slack)."""

import dataclasses
from pathlib import Path

from fakes import Clock, FakeManager, FakeTransport, SyncExecutor
from herdr_slackbot.bridge import Bridge
from herdr_slackbot.config import load_config
from herdr_slackbot.events import SubscriptionManager
from herdr_slackbot.herdr_client import ReadResult
from herdr_slackbot.results import ResultStore
from herdr_slackbot.state import StateStore
from test_events import FakeClient, _move, ev, moved_env, status_env

SHORT = (Path(__file__).parent / "fixtures" / "transcripts" / "claude_short.txt").read_text(encoding="utf-8")


class HerdrForBridge(FakeClient):
    def list_workspaces(self):
        return [{"workspace_id": "w1", "label": "Main"}, {"workspace_id": "w2", "label": "Other"}]

    def list_panes(self, workspace_id=None):
        return []

    def read_agent(self, target, lines=200, **kw):
        return ReadResult(SHORT, 10, 1)

    def read_agent_once(self, target, lines, source="recent_unwrapped"):
        return ""

    def ping(self):
        return {"version": "0.8.2"}


def wire(tmp_path, client):
    cfg = dataclasses.replace(load_config(env={"USERNAME": "me"}, config_dir=tmp_path), slack_owner_user_id="U")
    transport, state = FakeTransport(), StateStore(tmp_path / "state.json")
    bridge = Bridge(cfg, client, state, transport, ResultStore(tmp_path / "r"), clock=Clock(),
                    sleep=lambda s: None, executor=SyncExecutor(), manager=FakeManager(),
                    claude_projects=tmp_path / "p")
    bridge.dm_channel = "D"
    mgr = SubscriptionManager(client, bridge.handle_transition, resync_interval=3600, retry_delay=None)
    return bridge, mgr, transport, state


def test_provisional_codex_moves_and_reports_session_at_completion(tmp_path):
    client = HerdrForBridge()
    client.start("w1:p1", "working", session=False, terminal="X")
    bridge, mgr, transport, state = wire(tmp_path, client)
    state.upsert_thread("pending:X", channel="D", thread_ts="1.0", pane_id="w1:p1", terminal_id="X",
                        provisional=True, agent_name="cx", muted=True,
                        pending_task={"task_id": "t", "started_at": 1.0, "seq0": 0, "working_announced": True})
    mgr.resync()
    info = client.agents.pop("w1:p1")
    client.agents["w2:p9"] = dict(info, pane_id="w2:p9", workspace_id="w2", agent_status="idle",
                                  agent_session={"value": "REAL"}, state_change_seq=999)
    mgr.handle_event(ev(moved_env("w1:p1", "w2:p9")))
    results = [p for p in transport.posts if p["text"].startswith("✅")]
    assert len(results) == 1 and results[0]["thread_ts"] == "1.0"
    entry = state.get_thread("REAL")
    assert entry["pending_task"] is None and entry["pane_id"] == "w2:p9"
    assert state.get_thread("pending:X") is None
    mgr.stop()


def test_moved_session_with_failed_destination_keeps_pending_muted_task(tmp_path):
    client = HerdrForBridge(("w1:p1", "working", "S"))
    bridge, mgr, transport, state = wire(tmp_path, client)
    state.upsert_thread("S", channel="D", thread_ts="2.0", pane_id="w1:p1", agent_name="coder", muted=True,
                        pending_task={"task_id": "t", "started_at": 1.0, "seq0": 0, "working_announced": True})
    mgr.resync()
    _move(client, "w1:p1", "w2:p9")
    client.fail_find_for["w2:p9"] = 1
    mgr.handle_event(ev(status_env("w1:p1", "idle")))  # old pane's buffered event first
    mgr.handle_event(ev(moved_env("w1:p1", "w2:p9")))
    assert not any("ended before" in p["text"] for p in transport.posts)
    assert state.get_thread("S")["pending_task"] is not None
    client.set_status("w2:p9", "idle")
    mgr.handle_event(ev(status_env("w2:p9", "idle")))
    results = [p for p in transport.posts if p["text"].startswith("✅")]
    assert len(results) == 1 and results[0]["thread_ts"] == "2.0"
    assert state.get_thread("S")["pending_task"] is None
    mgr.stop()
