import dataclasses
import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from fakes import Clock, DeferredExecutor, FakeHerdr, FakeManager, FakeTransport, SyncExecutor
from herdr_slackbot import blocks as B
from herdr_slackbot.bridge import Bridge, _Resume, prompt_visible
from herdr_slackbot.claude_session import cwd_slug
from herdr_slackbot.config import load_config
from herdr_slackbot.events import AgentTransition
from herdr_slackbot.herdr_client import HerdrError, HerdrOutcomeUnknown
from herdr_slackbot.results import ResultStore
from herdr_slackbot.slack_transport import SlackPermanentError, SlackTransientError
from herdr_slackbot.state import StateStore

FIXTURES = Path(__file__).parent / "fixtures"
SHORT = (FIXTURES / "transcripts" / "claude_short.txt").read_text(encoding="utf-8")
LONG = (FIXTURES / "transcripts" / "claude_long_multiblock.txt").read_text(encoding="utf-8")


def make_env(tmp_path, executor=None, start=True):
    cfg = dataclasses.replace(load_config(env={"USERNAME": "me"}, config_dir=tmp_path),
                              slack_owner_user_id="UOWNER", slack_bot_token="xoxb-t", slack_app_token="xapp-t")
    herdr, transport, clock = FakeHerdr(), FakeTransport(), Clock()
    sleeps = []

    def sleep(s):
        sleeps.append(s)
        clock.sleep(s)

    state = StateStore(tmp_path / "state.json")
    bridge = Bridge(cfg, herdr, state, transport, ResultStore(tmp_path / "results"), clock=clock, sleep=sleep,
                    executor=executor or SyncExecutor(), manager=FakeManager(),
                    claude_projects=tmp_path / "projects", codex_home_dir=tmp_path / "codex", ack_budget=0.3)
    env = SimpleNamespace(cfg=cfg, herdr=herdr, transport=transport, clock=clock, sleeps=sleeps, state=state,
                          bridge=bridge, tmp=tmp_path)
    if start:
        bridge.start()
    return env


@pytest.fixture
def env(tmp_path):
    e = make_env(tmp_path)
    yield e
    e.bridge.stop()


def run(env, text):
    """Slash command after ack; returns the text of the response_url reply (or None)."""
    before = len(env.transport.responses)
    env.bridge.run_command("UOWNER", "C1", text, "TRIG", "https://hooks.slack/resp")
    new = env.transport.responses[before:]
    return new[-1]["text"] if new else None


def transition(env, pane_id, prev, status, **kw):
    info = env.herdr.find_agent(pane_id)
    session = kw.pop("session", None) or ((info or {}).get("agent_session") or {}).get("value")
    return AgentTransition(pane_id, pane_id.split(":")[0], prev, status, env.clock(), session,
                           (info or {}).get("name"), info, **kw)


def results_posted(env):
    return [p for p in env.transport.posts if p["text"].startswith("✅")]


# --- commands -----------------------------------------------------------------------------

def test_usage_list_status_unknown(env):
    assert "/herdr-me list" in run(env, "")
    env.herdr.add_agent("w1:p1", "working", name="coder")
    run(env, "list")
    assert "coder" in json.dumps(env.transport.responses[-1]["blocks"])
    run(env, "status")
    assert "Herdr 0.8.2" in env.transport.responses[-1]["blocks"][0]["text"]["text"]
    assert "Unknown command" in run(env, "frobnicate")
    assert all(r["url"] == "https://hooks.slack/resp" for r in env.transport.responses)


def test_review1_new_modal_opens_loading_view_before_any_herdr_call(env):
    env.herdr.log = env.transport.events  # one ordered log for both sides
    run(env, "new")
    assert env.transport.events[0] == "open_view"
    assert env.transport.views_opened[0]["trigger_id"] == "TRIG"
    assert "Loading" in json.dumps(env.transport.views_opened[0]["view"])
    view = env.transport.views_updated[-1]["view"]
    assert view["callback_id"] == B.NEW_CALLBACK
    assert [o["value"] for o in view["blocks"][0]["element"]["options"]] == ["w1", "w2"]  # bridge ws hidden
    assert view["blocks"][1]["element"]["initial_value"] == "D:\\main"
    assert json.loads(view["private_metadata"]) == {"channel": "C1"}


def test_review1_send_modal_loading_then_filled_and_herdr_error(env):
    env.herdr.add_agent("w1:p1", "idle", name="coder")
    env.herdr.log = env.transport.events
    run(env, "send")
    assert env.transport.events[0] == "open_view"
    assert env.transport.views_updated[-1]["view"]["callback_id"] == B.SEND_CALLBACK

    def broken():
        raise HerdrError("unavailable", "pipe gone")
    env.herdr.list_agents = broken
    run(env, "send")
    assert "Herdr error" in json.dumps(env.transport.views_updated[-1]["view"])


def test_review1_view_submission_does_not_wait_for_slow_herdr(tmp_path):
    import time as _time
    env = make_env(tmp_path, executor=DeferredExecutor())
    env.herdr.add_agent("w1:p1", "idle", name="coder")
    env.herdr.slow["list_agents"] = 1.0
    view = {"state": {"values": {"target": {"value": {"selected_option": {"value": "coder"}}},
                                 "prompt": {"value": {"value": "x"}}}}}
    t0 = _time.monotonic()
    assert env.bridge.submit_send_view(view) is None  # acked after the 0.3s budget
    assert _time.monotonic() - t0 < 0.8
    env.herdr.slow.clear()
    env.bridge.executor.run_all()  # the worker validates and sends
    assert env.herdr.prompts() == [("prompt_agent", "w1:p1", "x")]
    env.bridge.stop()


def test_update_new_modal_workspace_and_kind(env):
    view = {"id": "V1", "hash": "H1", "private_metadata": "{}", "state": {"values": {
        "ws": {"new_ws": {"selected_option": {"value": "w2"}}},
        "cwd:w1": {"value": {"value": "D:\\main"}},
        "kind": {"new_kind": {"selected_option": {"value": "claude"}}},
        "prompt": {"value": {"value": "keep me"}},
    }}}
    env.bridge.update_new_modal(B.ACTION_NEW_WS, view)
    upd = env.transport.views_updated[-1]
    assert upd["view_id"] == "V1" and upd["hash"] == "H1"
    cwd = next(b for b in upd["view"]["blocks"] if b["block_id"].startswith("cwd:"))
    assert cwd["block_id"] == "cwd:w2" and cwd["element"]["initial_value"] == "D:\\other"
    view["state"]["values"]["kind"]["new_kind"]["selected_option"]["value"] = "codex"
    env.bridge.update_new_modal(B.ACTION_NEW_KIND, view)
    ids = [b["block_id"] for b in env.transport.views_updated[-1]["view"]["blocks"]]
    assert "model:codex" in ids and "perm" not in ids


def test_new_agent_direct_flow(env):
    assert run(env, "new Main review the diff please").startswith("🚀 Starting a claude agent in *Main*")
    create = next(c for c in env.herdr.calls if c[0] == "create_tab")
    assert create == ("create_tab", "w1", "slack-1 review the diff please", "D:\\main")
    start = next(c for c in env.herdr.calls if c[0] == "start_agent")
    assert start[1:3] == ("slack-1", "claude")
    assert start[4] == ["--model", "opus", "--effort", "high", "--permission-mode", "auto"]
    pane = start[3]
    assert env.herdr.prompts() == [("prompt_agent", pane, "review the diff please")]
    root = env.transport.posts[0]
    assert root["thread_ts"] is None and "🚀" in root["blocks"][0]["text"]["text"]
    assert root["blocks"][-1]["block_id"] == B.BLOCK_THREAD_CTL
    session = env.herdr.find_agent("slack-1")["agent_session"]["value"]
    entry = env.state.get_thread(session)
    assert entry["origin"] == "slack" and entry["thread_ts"] == root["ts"] and entry["agent_name"] == "slack-1"
    task = entry["pending_task"]
    assert task["working_announced"] is False and task["task_id"] and "seq0" in task


def test_new_agent_modal_submission_validates_name(env):
    env.herdr.add_agent("w1:p1", name="taken")
    values = {"ws": {"new_ws": {"selected_option": {"value": "w1"}}},
              "kind": {"new_kind": {"selected_option": {"value": "claude"}}},
              "name": {"value": {"value": "Bad Name"}}, "prompt": {"value": {"value": ""}}}
    assert set(env.bridge.submit_new_view({"state": {"values": values}})) == {"name", "prompt"}
    values["name"]["value"]["value"] = "taken"
    values["prompt"]["value"]["value"] = "go"
    assert "already running" in env.bridge.submit_new_view({"state": {"values": values}})["name"]
    values["name"]["value"]["value"] = "fresh"
    assert env.bridge.submit_new_view({"state": {"values": values}}) is None
    pane = next(c for c in env.herdr.calls if c[0] == "start_agent")[3]
    assert ("prompt_agent", pane, "go") in env.herdr.prompts()
    assert next(c for c in env.herdr.calls if c[0] == "create_tab")[2] == "fresh"


def test_new_codex_waits_before_prompt_and_uses_codex_args(env):
    run(env, "new w2 kind=codex model=gpt-6-sol effort=max hello")
    start = next(c for c in env.herdr.calls if c[0] == "start_agent")
    assert start[4] == ["-m", "gpt-6-sol", "-c", "model_reasoning_effort=max"]
    assert env.cfg.codex_prompt_delay in env.sleeps
    assert len(env.herdr.prompts()) == 1


def test_new_agent_start_errors_are_reported_via_response_url(env):
    env.herdr.start_error = HerdrError("agent_not_ready", "blocked")
    run(env, "new Main hi")
    assert any("waiting for confirmation on PC" in r["text"] for r in env.transport.responses)
    env.herdr.start_error = HerdrOutcomeUnknown("pipe closed")
    run(env, "new Main hi")
    assert any("nothing was retried" in r["text"] for r in env.transport.responses)
    assert len([c for c in env.herdr.calls if c[0] == "start_agent"]) == 2
    assert env.herdr.prompts() == []


def test_new_command_errors(env):
    assert "Unknown workspace" in run(env, "new Nope hi")
    assert "Missing prompt" in run(env, "new Main")
    assert "Unmatched double quote" in run(env, 'new Main cwd="D:\\x hi')


# --- send / D6 --------------------------------------------------------------------------------

@pytest.mark.parametrize("status,needle", [("working", "busy"), ("blocked", "needs confirmation on PC"),
                                           ("unknown", "state is unknown")])
def test_send_rejections(env, status, needle):
    env.herdr.add_agent("w1:p1", status, name="coder")
    assert needle in run(env, "send coder do it")
    assert env.herdr.prompts() == []


def test_send_creates_thread_then_reuses_it(env):
    env.herdr.add_agent("w1:p1", "idle", name="coder", session="S1")
    env.herdr.after_prompt = None  # stays idle and unchanged: an unreacted reservation
    assert "Sending to *coder*" in run(env, "send coder first task")
    root = env.transport.posts[0]
    assert root["thread_ts"] is None and "📨 first task" in root["blocks"][0]["text"]["text"]
    assert env.state.get_thread("S1")["origin"] == "slack"
    run(env, "send coder again")  # within the grace period: rejected by the worker
    assert any("previous Slack task" in r["text"] for r in env.transport.responses)
    env.clock.now += 60  # the agent never reacted: the old reservation may be replaced
    run(env, "send coder second task")
    assert env.transport.posts[-1]["thread_ts"] == root["ts"] and "second task" in env.transport.posts[-1]["text"]
    assert [p[2] for p in env.herdr.prompts()] == ["first task", "second task"]


def test_send_modal_submission(env):
    env.herdr.add_agent("w1:p1", "working", name="coder")
    view = {"state": {"values": {"target": {"value": {"selected_option": {"value": "coder"}}},
                                 "prompt": {"value": {"value": "x"}}}}}
    errors = env.bridge.submit_send_view(view)
    assert "busy" in errors["target"] and "*" not in errors["target"]
    env.herdr.set_status("w1:p1", "done")
    assert env.bridge.submit_send_view(view) is None
    assert env.herdr.prompts() == [("prompt_agent", "w1:p1", "x")]


def test_send_agent_blocked_error_clears_pending(env):
    env.herdr.add_agent("w1:p1", "idle", name="coder", session="S1")
    env.herdr.prompt_script = [HerdrError("agent_blocked", "blocked")]
    run(env, "send coder hi")
    assert env.state.get_thread("S1")["pending_task"] is None
    assert "needs confirmation on PC" in env.transport.posts[-1]["text"]


# --- #2 atomic admission ---------------------------------------------------------------------------

def test_review2_queued_sends_prompt_once(tmp_path):
    env = make_env(tmp_path, executor=DeferredExecutor())
    env.herdr.add_agent("w1:p1", "idle", name="coder", session="S1")
    run(env, "send coder first")
    run(env, "send coder second")
    env.bridge.executor.run_all()
    assert [p[2] for p in env.herdr.prompts()] == ["first"]
    last = env.transport.responses[-1]["text"]
    assert "busy" in last or "previous Slack task" in last
    assert len([p for p in env.transport.posts if p["thread_ts"] is None]) == 1
    env.bridge.stop()


def test_review2_simultaneous_sends_create_one_root(tmp_path):
    env = make_env(tmp_path, executor=DeferredExecutor())
    env.herdr.add_agent("w1:p1", "idle", name="coder", session="S1")
    env.herdr.after_prompt = None
    env.transport.post_delay = 0.05
    run(env, "send coder one")
    run(env, "send coder two")
    jobs, env.bridge.executor.jobs = env.bridge.executor.jobs, []
    threads = [threading.Thread(target=fn, args=args) for fn, args in jobs]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
    roots = [p for p in env.transport.posts if p["thread_ts"] is None]
    assert len(roots) == 1 and len(env.herdr.prompts()) == 1
    assert env.state.get_thread("S1")["thread_ts"] == roots[0]["ts"]
    env.bridge.stop()


# --- #3 identity re-validation -----------------------------------------------------------------------

@pytest.mark.parametrize("name", ["coder", None])
def test_review3_queued_thread_reply_not_sent_to_replacement(tmp_path, name):
    env = make_env(tmp_path, executor=DeferredExecutor())
    env.herdr.add_agent("w1:p1", "idle", name=name, session="A")
    env.state.upsert_thread("A", channel="D-OWNER", thread_ts="500.1", agent_name=name, pane_id="w1:p1")
    env.bridge.handle_dm_message("D-OWNER", "UOWNER", "bound to A", "500.9", "500.1")
    env.herdr.add_agent("w1:p1", "idle", name=name, session="B")  # A exited, B took the pane/name
    env.bridge.executor.run_all()
    assert env.herdr.prompts() == []
    assert "different agent" in env.transport.posts[-1]["text"] and env.transport.posts[-1]["thread_ts"] == "500.1"
    assert env.state.get_thread("B") is None
    env.bridge.stop()


def test_review3_direct_send_queued_then_replaced(tmp_path):
    env = make_env(tmp_path, executor=DeferredExecutor())
    env.herdr.add_agent("w1:p1", "idle", name="coder", session="A")
    run(env, "send coder hi")
    env.herdr.add_agent("w1:p1", "idle", name="coder", session="B")
    env.bridge.executor.run_all()
    assert env.herdr.prompts() == []
    assert "different agent" in env.transport.responses[-1]["text"]
    env.bridge.stop()


def test_review3_identity_changes_between_admission_and_prompt(env):
    env.herdr.add_agent("w1:p1", "idle", name="coder", session="A")
    orig = env.herdr.find_agent
    state = {"admitted": False}
    orig_upsert = env.state.upsert_thread

    def mark(key, **fields):
        if fields.get("pending_task") is not None:
            state["admitted"] = True
        return orig_upsert(key, **fields)

    def swap_after_admission(target):
        if state["admitted"]:
            env.herdr.add_agent("w1:p1", "idle", name="coder", session="B")
            state["admitted"] = False
        return orig(target)

    env.state.upsert_thread = mark
    env.herdr.find_agent = swap_after_admission
    run(env, "send coder hi")
    assert env.herdr.prompts() == []
    assert env.state.get_thread("A")["pending_task"] is None
    assert "different agent" in env.transport.posts[-1]["text"]


def test_thread_reply_sends_to_bound_agent(env):
    env.herdr.add_agent("w1:p1", "idle", name="coder", session="S1")
    env.state.upsert_thread("S1", channel="D-OWNER", thread_ts="500.1", agent_name="coder", pane_id="w1:p1")
    env.bridge.handle_dm_message("D-OWNER", "UOWNER", "please continue", "500.9", "500.1")
    assert env.herdr.prompts() == [("prompt_agent", "w1:p1", "please continue")]
    assert env.transport.posts == []  # no echo of the user's own thread message


def test_thread_reply_follows_moved_pane_then_exited(env):
    env.herdr.add_agent("w1:p1", "idle", name="coder", session="S1")
    env.state.upsert_thread("S1", channel="D-OWNER", thread_ts="500.1", agent_name="coder", pane_id="w1:p1")
    info = env.herdr.agents.pop("w1:p1")
    env.herdr.agents["w2:p7"] = dict(info, pane_id="w2:p7", workspace_id="w2")
    env.bridge.handle_dm_message("D-OWNER", "UOWNER", "hi", "500.9", "500.1")
    assert env.herdr.prompts() == [("prompt_agent", "w2:p7", "hi")]
    del env.herdr.agents["w2:p7"]
    env.clock.now += 60
    env.bridge.handle_dm_message("D-OWNER", "UOWNER", "again", "501.0", "500.1")
    assert "has exited" in env.transport.posts[-1]["text"]


def test_thread_reply_busy(env):
    env.herdr.add_agent("w1:p1", "working", name="coder", session="S1")
    env.state.upsert_thread("S1", channel="D-OWNER", thread_ts="500.1", agent_name="coder", pane_id="w1:p1")
    env.bridge.handle_dm_message("D-OWNER", "UOWNER", "hi", "500.9", "500.1")
    assert "busy" in env.transport.posts[-1]["text"]


def test_plain_dm_and_unbound_thread(env):
    env.bridge.handle_dm_message("D-OWNER", "UOWNER", "hello", "600.1", None)
    assert env.transport.ephemerals and "slash command" in env.transport.ephemerals[0]["text"]
    env.bridge.handle_dm_message("D-OWNER", "UOWNER", "hello", "600.2", "599.0")
    assert "not bound to an agent" in env.transport.posts[-1]["text"]


# --- #4 no resend on unknown outcomes ---------------------------------------------------------------

def _stalled():
    return HerdrError("agent_prompt_stalled", "no state change within 5000 ms")


@pytest.mark.parametrize("screen,error", [
    ("", None),  # nothing visible (clipped / cleared)
    (None, HerdrError("agent_not_idle", "busy")),  # screen unreadable
    ("› short bullet points: what is a named p", None),  # partial paste, tail missing
])
@pytest.mark.parametrize("first_error", [HerdrOutcomeUnknown("eof"), _stalled()])
def test_review4_unknown_outcome_is_never_resent(env, screen, error, first_error):
    env.herdr.add_agent("w1:p1", "idle", name="cx", kind="codex", session="S1")
    env.herdr.after_prompt = None
    env.herdr.prompt_script = [first_error]
    if screen is not None:
        env.herdr.visible["w1:p1"] = screen
    env.herdr.visible_error = error
    run(env, "send cx Answer: what is a named pipe on Windows?")
    assert len(env.herdr.prompts()) == 1
    assert "not retried" in env.transport.posts[-1]["text"]
    assert env.state.get_thread("S1")["pending_task"] is not None  # may still start


def test_stall_then_agent_starts_counts_as_sent(env):
    env.herdr.add_agent("w1:p1", "idle", name="cx", kind="codex", session="S1")

    def stall_but_start_later():
        env.herdr.set_status("w1:p1", "working")
        return _stalled()

    env.herdr.prompt_script = [stall_but_start_later]
    run(env, "send cx hello there")
    assert len(env.herdr.prompts()) == 1
    assert not any(t.startswith(("⚠️", "❌")) for t in env.transport.texts())


def test_stall_with_text_on_screen_says_so(env):
    env.herdr.add_agent("w1:p1", "idle", name="cx", kind="codex", session="S1")
    env.herdr.after_prompt = None
    env.herdr.prompt_script = [_stalled()]
    env.herdr.visible["w1:p1"] = "› short bullet points: what is a named pipe on Windows\n"
    run(env, "send cx Answer in 3 short bullet points: what is a named pipe on Windows?")
    assert len(env.herdr.prompts()) == 1
    assert "has not started" in env.transport.posts[-1]["text"]


def test_fresh_agent_prompt_retries_until_resolvable(env):
    not_ready = HerdrError("agent_not_ready", "agent slack-1 is not an active named agent")
    env.herdr.prompt_script = [not_ready, not_ready, None]
    run(env, "new Main hi there")
    pane = next(c for c in env.herdr.calls if c[0] == "start_agent")[3]
    assert [p[1:] for p in env.herdr.prompts()] == [(pane, "hi there")] * 3
    assert not any(t.startswith(("❌", "⚠️")) for t in env.transport.texts())


def test_existing_agent_not_found_is_not_retried(env):
    env.herdr.add_agent("w1:p1", "idle", name="coder", session="S1")
    env.herdr.prompt_script = [HerdrError("agent_not_found", "gone")]
    run(env, "send coder hi")
    assert len(env.herdr.prompts()) == 1
    assert env.transport.posts[-1]["text"].startswith("❌ Could not send")


def test_prompt_visible():
    assert prompt_visible("› …bullet points: what is a named pipe on Windows\n", "Q: what is a named pipe on Windows?")
    assert not prompt_visible("nothing here", "what is a named pipe on Windows?")
    assert not prompt_visible("anything", "   ")


# --- #5 task identity ----------------------------------------------------------------------------------

def test_review5_delayed_completion_does_not_clear_newer_task(env):
    env.herdr.add_agent("w1:p1", "idle", name="coder", session="S1")
    env.herdr.screens["w1:p1"] = SHORT
    run(env, "send coder TASK A")  # -> working
    env.clock.now += 60
    env.herdr.set_status("w1:p1", "done")
    a_done = transition(env, "w1:p1", "working", "done")  # queued, not processed yet
    run(env, "send coder NEW TASK")  # admission settles A first (posts A's result), then reserves B
    assert len(results_posted(env)) == 1
    task_b = env.state.get_thread("S1")["pending_task"]
    assert task_b["prompt"] == "NEW TASK" and env.herdr.find_agent("w1:p1")["agent_status"] == "working"
    env.bridge.handle_transition(a_done)  # the old completion arrives late
    assert len(results_posted(env)) == 1  # no duplicate
    assert env.state.get_thread("S1")["pending_task"]["task_id"] == task_b["task_id"]  # B untouched
    env.herdr.set_status("w1:p1", "idle")
    env.bridge.handle_transition(transition(env, "w1:p1", "working", "idle"))
    assert len(results_posted(env)) == 2
    assert env.state.get_thread("S1")["pending_task"] is None


def test_slack_task_started_and_completed(env):
    env.herdr.add_agent("w1:p1", "idle", name="coder", session="S1")
    run(env, "send coder what is 2+2")
    root_ts = env.transport.posts[0]["ts"]
    env.bridge.handle_transition(transition(env, "w1:p1", "idle", "working"))
    assert env.transport.posts[-1]["text"] == "⏳ started (working)"
    assert env.state.get_thread("S1")["pending_task"]["working_announced"] is True
    env.bridge.handle_transition(transition(env, "w1:p1", "blocked", "working"))
    assert sum(1 for p in env.transport.posts if "started" in p["text"]) == 1
    env.herdr.set_status("w1:p1", "idle")
    env.herdr.screens["w1:p1"] = SHORT
    env.clock.now += 125
    env.bridge.handle_transition(transition(env, "w1:p1", "working", "idle"))
    result = env.transport.posts[-1]
    assert result["thread_ts"] == root_ts
    assert result["blocks"][0]["text"]["text"].startswith("✅ *coder* · Main · 2m 5s")
    assert any(b["type"] == "section" and b["text"]["text"] == "2+2 equals 4." for b in result["blocks"])
    assert env.state.get_thread("S1")["pending_task"] is None
    env.bridge.handle_transition(transition(env, "w1:p1", "working", "idle"))  # replay: no duplicate
    assert len(results_posted(env)) == 1


# --- #6 provisional migration needs evidence ----------------------------------------------------------

def test_review6_old_ended_event_does_not_adopt_new_provisional_thread(env):
    env.herdr.add_agent("w1:p1", "idle", name="cx", kind="codex", session=False)
    env.herdr.agents["w1:p1"]["terminal_id"] = "new-terminal"
    env.state.upsert_thread("pending:new-terminal", channel="D-OWNER", thread_ts="9.0", pane_id="w1:p1",
                            terminal_id="new-terminal", provisional=True, agent_name="cx",
                            pending_task={"task_id": "t1", "started_at": env.clock(), "seq0": 1})
    old = AgentTransition("w1:p1", "w1", "working", "unknown", env.clock(), "OLD", "old", None, ended=True)
    env.bridge.handle_transition(old)
    assert env.state.get_thread("OLD") is None
    assert env.state.get_thread("pending:new-terminal")["pending_task"]["task_id"] == "t1"
    assert env.transport.posts == []


def test_review6_live_transition_without_terminal_evidence_does_not_adopt(env):
    env.state.upsert_thread("pending:T-new", channel="D-OWNER", thread_ts="9.0", pane_id="w1:p1",
                            terminal_id="T-new", provisional=True,
                            pending_task={"task_id": "t1", "started_at": env.clock(), "seq0": 1})
    info = {"pane_id": "w1:p1", "workspace_id": "w1", "agent_status": "done", "terminal_id": "T-old",
            "agent_session": {"value": "OLD"}, "state_change_seq": 5}
    env.bridge.handle_transition(AgentTransition("w1:p1", "w1", "working", "done", 1.0, "OLD", None, info))
    assert env.state.get_thread("pending:T-new")["pending_task"]["task_id"] == "t1"


def test_codex_without_session_uses_provisional_thread_then_rekeys(env):
    env.herdr.sessionless_kinds = {"codex"}
    env.herdr.after_prompt = None
    run(env, "new Main name=cx kind=codex hello")
    pane = env.herdr.find_agent("cx")["pane_id"]
    key = env.state.find_provisional(pane)
    assert key and key.startswith("pending:term-")
    root_ts = env.transport.posts[0]["ts"]
    env.herdr.set_status(pane, "working")
    env.bridge.handle_transition(transition(env, pane, "idle", "working"))
    assert env.transport.posts[-1]["text"] == "⏳ started (working)"
    env.herdr.agents[pane]["agent_session"] = {"value": "CODEX-S"}
    env.herdr.set_status(pane, "done")
    env.herdr.screens[pane] = "• answer\n\n› Ask Codex to do anything\n"
    env.bridge.handle_transition(transition(env, pane, "working", "done"))
    assert env.state.get_thread(key) is None
    entry = env.state.get_thread("CODEX-S")
    assert entry["thread_ts"] == root_ts and "provisional" not in entry and entry["pending_task"] is None
    assert env.transport.posts[-1]["thread_ts"] == root_ts and env.transport.posts[-1]["text"].startswith("✅ cx")


def test_send_to_sessionless_pc_agent(env):
    env.herdr.add_agent("w2:p4", "idle", name="cx2", kind="codex", session=False)
    run(env, "send cx2 hi")
    assert env.herdr.prompts() == [("prompt_agent", "w2:p4", "hi")]
    assert env.state.find_provisional("w2:p4") is not None


def test_thread_reply_to_provisional_thread(env):
    env.herdr.add_agent("w1:p1", "idle", name="cx", kind="codex", session=False)
    env.state.upsert_thread("pending:term-w1:p1", channel="D-OWNER", thread_ts="7.0", pane_id="w1:p1",
                            terminal_id="term-w1:p1", provisional=True, agent_name="cx")
    env.bridge.handle_dm_message("D-OWNER", "UOWNER", "go", "7.5", "7.0")
    assert env.herdr.prompts() == [("prompt_agent", "w1:p1", "go")]
    env.herdr.agents["w1:p1"]["terminal_id"] = "term-other"
    env.clock.now += 60
    env.bridge.handle_dm_message("D-OWNER", "UOWNER", "again", "7.6", "7.0")
    assert len(env.herdr.prompts()) == 1
    assert "different agent" in env.transport.posts[-1]["text"]


# --- #7 mute after migration --------------------------------------------------------------------------

@pytest.mark.parametrize("via", ["transition", "restart"])
def test_review7_mute_button_after_provisional_migration(env, via):
    env.herdr.add_agent("w1:p1", "working", name="cx", kind="codex", session="REAL")
    env.herdr.agents["w1:p1"]["terminal_id"] = "T1"
    root = B.thread_root_blocks("🚀 *cx*", [], "pending:T1", muted=False)
    env.state.upsert_thread("pending:T1", channel="D-OWNER", thread_ts="1.0", pane_id="w1:p1", terminal_id="T1",
                            provisional=True, pending_task={"task_id": "t", "started_at": env.clock(), "seq0": 0,
                                                            "working_announced": True})
    if via == "transition":
        env.bridge.handle_transition(transition(env, "w1:p1", "idle", "working"))
    else:
        env.bridge.handle_resume("pending:T1")
    assert env.state.get_thread("REAL") is not None and env.state.get_thread("pending:T1") is None
    old_value = root[-1]["elements"][0]["value"]  # still carries the provisional key
    env.bridge.toggle_mute("D-OWNER", {"ts": "1.0", "blocks": root}, old_value)
    assert env.state.is_muted("REAL")
    assert env.state.get_thread("pending:T1") is None  # no stray entry created
    new_value = json.loads(env.transport.updates[-1]["blocks"][-1]["elements"][0]["value"])
    assert new_value == {"s": "REAL", "m": False}
    env.bridge.toggle_mute("D-OWNER", {"ts": "9.9"}, json.dumps({"s": "ghost", "m": True}))
    assert env.state.get_thread("ghost") is None


# --- #8 resume vs live transition ----------------------------------------------------------------------

@pytest.mark.parametrize("order", ["transition_first", "resume_first"])
def test_review8_resume_and_transition_post_once(env, order):
    env.herdr.add_agent("w1:p1", "done", name="coder", session="S1")
    env.herdr.screens["w1:p1"] = SHORT
    env.state.upsert_thread("S1", channel="D-OWNER", thread_ts="1.0", agent_name="coder", pane_id="w1:p1",
                            pending_task={"task_id": "t", "started_at": env.clock() - 30, "seq0": 0,
                                          "working_announced": True})
    t = transition(env, "w1:p1", "working", "done")
    steps = [lambda: env.bridge.handle_transition(t), lambda: env.bridge.handle_resume("S1")]
    for step in (steps if order == "transition_first" else list(reversed(steps))):
        step()
    assert len(results_posted(env)) == 1
    assert env.state.get_thread("S1")["pending_task"] is None


def test_review8_resume_runs_on_notifier_queue(tmp_path):
    env = make_env(tmp_path, start=False)
    env.herdr.add_agent("w1:p1", "done", name="coder", session="S1")
    env.herdr.screens["w1:p1"] = SHORT
    env.state.upsert_thread("S1", channel="D-OWNER", thread_ts="1.0", agent_name="coder", pane_id="w1:p1",
                            pending_task={"task_id": "t", "started_at": env.clock() - 30, "seq0": 0,
                                          "working_announced": True})
    env.bridge.enqueue_transition(transition(env, "w1:p1", "working", "done"))  # live event queued first
    env.bridge.start()  # resume item queued behind it
    assert env.bridge.wait_idle()
    assert len(results_posted(env)) == 1
    env.bridge.stop()


def test_resume_pending_on_start(tmp_path):
    env = make_env(tmp_path, start=False)
    herdr, state, clock = env.herdr, env.state, env.clock
    herdr.add_agent("w1:p1", "done", name="finished", session="S-done")
    herdr.add_agent("w1:p2", "working", name="busy", session="S-busy")
    herdr.add_agent("w1:p3", "idle", name="other", session="S-new")
    herdr.screens["w1:p1"] = SHORT
    task = {"task_id": "t", "started_at": clock() - 30, "working_announced": True, "seq0": 0}
    for session, pane, ts in (("S-done", "w1:p1", "1.1"), ("S-busy", "w1:p2", "1.2"), ("S-gone", "w1:p4", "1.3")):
        state.upsert_thread(session, channel="D-OWNER", thread_ts=ts, agent_name=session, pane_id=pane,
                            pending_task=task)
    env.bridge.start()
    assert env.bridge.wait_idle()
    by_thread = {p["thread_ts"]: p["text"] for p in env.transport.posts}
    assert by_thread["1.1"].startswith("✅ finished finished")
    assert "ended before completing" in by_thread["1.3"]
    assert "1.2" not in by_thread
    assert state.get_thread("S-done")["pending_task"] is None
    assert state.get_thread("S-gone")["pending_task"] is None
    assert state.get_thread("S-busy")["pending_task"] is not None
    env.bridge.stop()


# --- #9 Slack failures are retried ------------------------------------------------------------------------

def test_review9_completion_retried_until_slack_recovers(env):
    env.herdr.add_agent("w1:p1", "done", name="coder", session="S1")
    env.herdr.screens["w1:p1"] = SHORT
    env.state.upsert_thread("S1", channel="D-OWNER", thread_ts="1.0", agent_name="coder", pane_id="w1:p1",
                            pending_task={"task_id": "t", "started_at": env.clock(), "seq0": 0,
                                          "working_announced": True})
    env.transport.fail_posts = [SlackTransientError("internal_error"), SlackTransientError("ratelimited", 7)]
    assert env.bridge.process_with_retry(transition(env, "w1:p1", "working", "done"))
    assert len(results_posted(env)) == 1
    assert env.state.get_thread("S1")["pending_task"] is None
    assert 7 in env.sleeps and env.bridge.stats["slack_retries"] == 2


def test_review9_pc_completion_with_thread_creation_retried(env):
    env.herdr.add_agent("w2:p3", "done", session="PC1")
    env.herdr.screens["w2:p3"] = SHORT
    env.transport.fail_posts = [None, SlackTransientError("timeout")]  # root ok, result fails once
    assert env.bridge.process_with_retry(transition(env, "w2:p3", "working", "done"))
    roots = [p for p in env.transport.posts if p["thread_ts"] is None]
    results = results_posted(env)
    assert len(roots) == 1 and len(results) == 1 and results[0]["thread_ts"] == roots[0]["ts"]


def test_review9_notifier_thread_retries_in_background(env):
    env.herdr.add_agent("w1:p1", "done", name="coder", session="S1")
    env.herdr.screens["w1:p1"] = SHORT
    env.transport.fail_posts = [SlackTransientError("timeout")]
    env.bridge.enqueue_transition(transition(env, "w1:p1", "working", "done"))
    assert env.bridge.wait_idle()
    assert len(results_posted(env)) == 1


def test_review9_permanent_error_is_dropped(env):
    env.herdr.add_agent("w1:p1", "done", name="coder", session="S1")
    env.transport.fail_posts = [SlackPermanentError("invalid_blocks")]
    assert env.bridge.process_with_retry(transition(env, "w1:p1", "working", "done")) is False
    assert env.transport.posts == []


def test_review9_worker_posts_are_retried(env):
    env.herdr.add_agent("w1:p1", "idle", name="coder", session="S1")
    env.transport.fail_posts = [SlackTransientError("timeout")]  # the thread root fails once
    run(env, "send coder hi")
    assert len([p for p in env.transport.posts if p["thread_ts"] is None]) == 1
    assert env.herdr.prompts() == [("prompt_agent", "w1:p1", "hi")]


# --- other notification behaviour --------------------------------------------------------------------------

def test_pc_agent_done_creates_thread_and_posts_result(env):
    env.herdr.add_agent("w2:p3", "done", session="PC1", title="Build fix")
    env.herdr.screens["w2:p3"] = SHORT
    env.bridge.handle_transition(transition(env, "w2:p3", "working", "done"))
    root, result = env.transport.posts[0], env.transport.posts[1]
    assert root["thread_ts"] is None and "w2:p3" in root["blocks"][0]["text"]["text"]
    assert result["thread_ts"] == root["ts"]
    assert env.state.get_thread("PC1")["origin"] == "pc"
    env.bridge.handle_transition(transition(env, "w2:p3", "working", "idle"))
    assert len(env.transport.posts) == 2


def test_muted_pc_agent_is_silent_but_slack_task_is_not(env):
    env.herdr.add_agent("w1:p1", "done", name="coder", session="S1")
    env.state.upsert_thread("S1", channel="D-OWNER", thread_ts="1.0", muted=True, pane_id="w1:p1")
    env.bridge.handle_transition(transition(env, "w1:p1", "working", "done"))
    env.bridge.handle_transition(transition(env, "w1:p1", "working", "blocked"))
    assert env.transport.posts == []
    env.state.set_pending_task("S1", {"task_id": "t", "started_at": env.clock(), "seq0": 0,
                                      "working_announced": True})
    env.herdr.screens["w1:p1"] = SHORT
    env.herdr.set_status("w1:p1", "done")
    env.bridge.handle_transition(transition(env, "w1:p1", "working", "done"))
    assert env.transport.posts[-1]["thread_ts"] == "1.0"


def test_blocked_notification_once_per_seq(env):
    env.herdr.add_agent("w1:p1", "blocked", name="coder", session="S1")
    t = transition(env, "w1:p1", "working", "blocked")
    env.bridge.handle_transition(t)
    env.bridge.handle_transition(t)
    assert sum(1 for p in env.transport.posts if "needs confirmation" in p["text"]) == 1


def test_ended_before_completion_notice(env):
    env.herdr.add_agent("w1:p1", "working", name="coder", session="S1")
    env.state.upsert_thread("S1", channel="D-OWNER", thread_ts="1.0", agent_name="coder",
                            pending_task={"task_id": "t", "started_at": 1.0, "working_announced": True})
    t = AgentTransition("w1:p1", "w1", "working", "unknown", 2.0, "S1", "coder", None, ended=True)
    env.bridge.handle_transition(t)
    assert "ended before completing" in env.transport.posts[-1]["text"]
    assert env.state.get_thread("S1")["pending_task"] is None
    env.bridge.handle_transition(t)
    assert len(env.transport.posts) == 1


def test_result_prefers_claude_jsonl(env):
    session = "c6565ec4-87cd-4855-b9ee-4a940f248743"
    proj = env.tmp / "projects" / cwd_slug("D:\\main")
    proj.mkdir(parents=True)
    (proj / f"{session}.jsonl").write_bytes((FIXTURES / "claude_session.jsonl").read_bytes())
    env.herdr.add_agent("w1:p1", "done", name="coder", session=session)
    env.herdr.screens["w1:p1"] = SHORT
    env.bridge.handle_transition(transition(env, "w1:p1", "working", "done"))
    body = [b for b in env.transport.posts[-1]["blocks"] if b["type"] == "section"][-1]["text"]["text"]
    assert body.startswith("The docs directory contains")


def test_long_result_truncated_and_full_text_uploaded(env):
    env.herdr.add_agent("w1:p1", "done", name="coder", session="S1")
    env.herdr.screens["w1:p1"] = LONG
    env.bridge.handle_transition(transition(env, "w1:p1", "working", "done"))
    msg = env.transport.posts[-1]
    button = msg["blocks"][-1]["elements"][0]
    assert button["action_id"] == B.ACTION_SHOW_FULL
    assert all(len(b["text"]["text"]) <= 3000 for b in msg["blocks"] if b["type"] == "section")
    env.bridge.show_full("D-OWNER", {"ts": msg["ts"], "thread_ts": msg["thread_ts"]}, button["value"])
    up = env.transport.uploads[-1]
    assert up["thread_ts"] == msg["thread_ts"] and up["filename"].startswith("coder-") and up["filename"].endswith(".md")
    assert len(up["content"]) > 4000
    env.bridge.show_full("D-OWNER", {"ts": "1"}, "../../etc")
    assert "no longer available" in env.transport.posts[-1]["text"]


def test_mute_toggle_updates_state_and_message(env):
    root_blocks = B.thread_root_blocks("🤖 *coder*", [], "S1", muted=False)
    env.state.upsert_thread("S1", channel="D-OWNER", thread_ts="1.0")
    value = root_blocks[-1]["elements"][0]["value"]
    env.bridge.toggle_mute("D-OWNER", {"ts": "1.0", "text": "coder", "blocks": root_blocks}, value)
    assert env.state.is_muted("S1")
    upd = env.transport.updates[-1]
    assert upd["ts"] == "1.0" and "Unmute" in upd["blocks"][-1]["elements"][0]["text"]["text"]
    env.bridge.toggle_mute("D-OWNER", {"ts": "1.0", "blocks": upd["blocks"]}, upd["blocks"][-1]["elements"][0]["value"])
    assert not env.state.is_muted("S1")
    env.bridge.toggle_mute("D-OWNER", {"ts": "1.0"}, "not json")


def test_moved_agent_keeps_pending_task_and_thread(env):
    env.herdr.add_agent("w1:p1", "working", name="coder", session="S1")
    env.state.upsert_thread("S1", channel="D-OWNER", thread_ts="3.0", agent_name="coder", pane_id="w1:p1",
                            muted=True, pending_task={"task_id": "t", "started_at": env.clock() - 10, "seq0": 0,
                                                      "working_announced": True})
    info = env.herdr.agents.pop("w1:p1")
    env.herdr.agents["w2:p9"] = dict(info, pane_id="w2:p9", workspace_id="w2", agent_status="idle")
    env.herdr.screens["w2:p9"] = SHORT
    env.bridge.handle_transition(transition(env, "w2:p9", "working", "idle"))
    assert env.transport.posts[-1]["thread_ts"] == "3.0" and env.transport.posts[-1]["text"].startswith("✅ coder")
    entry = env.state.get_thread("S1")
    assert entry["pane_id"] == "w2:p9" and entry["pending_task"] is None


def test_fast_task_batched_by_manager_gets_started_and_result(env):
    env.herdr.add_agent("w1:p1", "idle", name="coder", session="S1")
    env.herdr.after_prompt = None
    run(env, "send coder quick one")
    env.herdr.set_status("w1:p1", "idle")
    env.herdr.screens["w1:p1"] = SHORT
    env.bridge.handle_transition(transition(env, "w1:p1", "idle", "working"))
    env.bridge.handle_transition(transition(env, "w1:p1", "working", "idle"))
    texts = env.transport.texts()
    assert "⏳ started (working)" in texts and texts[-1].startswith("✅ coder")
    assert env.state.get_thread("S1")["pending_task"] is None


def test_provisional_thread_found_after_move_by_terminal(env):
    env.herdr.add_agent("w2:p9", "working", name="cx", kind="codex", session=False)
    env.herdr.agents["w2:p9"]["terminal_id"] = "term-X"
    env.state.upsert_thread("pending:term-X", channel="D-OWNER", thread_ts="4.0", pane_id="w1:p1",
                            terminal_id="term-X", provisional=True, agent_name="cx",
                            pending_task={"task_id": "t", "started_at": env.clock(), "seq0": 0,
                                          "working_announced": False})
    t = AgentTransition("w2:p9", "w2", "idle", "working", env.clock(), None, "cx", env.herdr.find_agent("w2:p9"))
    env.bridge.handle_transition(t)
    assert env.transport.posts[-1]["thread_ts"] == "4.0" and "started" in env.transport.posts[-1]["text"]
    assert env.state.get_thread("pending:term-X")["pane_id"] == "w2:p9"


def test_resume_rekeys_provisional_entry(tmp_path):
    env = make_env(tmp_path, start=False)
    env.herdr.add_agent("w1:p1", "done", name="cx", kind="codex", session="REAL")
    env.herdr.screens["w1:p1"] = "• done\n"
    env.state.upsert_thread("pending:term-w1:p1", channel="D-OWNER", thread_ts="8.0", pane_id="w1:p1",
                            terminal_id="term-w1:p1", provisional=True, agent_name="cx",
                            pending_task={"task_id": "t", "started_at": env.clock() - 5, "seq0": 0,
                                          "working_announced": True})
    env.bridge.start()
    assert env.bridge.wait_idle()
    assert env.transport.posts[-1]["thread_ts"] == "8.0" and env.transport.posts[-1]["text"].startswith("✅ cx")
    assert env.state.get_thread("REAL")["pending_task"] is None
    assert env.state.get_thread("pending:term-w1:p1") is None
    env.bridge.stop()


def test_resume_item_class():
    assert _Resume("k").key == "k"


def test_new_agent_reported_unknown_right_after_start_is_waited_for(env):
    """Live finding: agent.start can return while Herdr still classifies the agent as unknown."""
    orig_start = env.herdr.start_agent

    def start_unknown(*a, **k):
        result = orig_start(*a, **k)
        pane = result["agent"]["pane_id"]
        env.herdr.agents[pane]["agent_status"] = "unknown"
        result["agent"]["agent_status"] = "unknown"
        orig_find = env.herdr.find_agent
        polls = {"n": 0}

        def settle_later(target):
            polls["n"] += 1
            if polls["n"] == 3:
                env.herdr.set_status(pane, "idle")
            return orig_find(target)
        env.herdr.find_agent = settle_later
        return result

    env.herdr.start_agent = start_unknown
    run(env, "new Main hello")
    assert len(env.herdr.prompts()) == 1
    assert not any("state is unknown" in t for t in env.transport.texts())
