"""Regression tests for docs/review/M2-recheck.md (R2, R5, R9, N1, N2, N3)."""

import threading
import time

import pytest

from fakes import Accepted, DeferredExecutor
from herdr_slackbot.bridge import Bridge, _Resume, target_of
from herdr_slackbot.results import ResultStore
from herdr_slackbot.slack_transport import SlackTransientError, SlackUncertainError
from test_bridge import SHORT, env, make_env, results_posted, run, transition  # noqa: F401 (env fixture)


def roots(env):
    return [p for p in env.transport.posts if p["thread_ts"] is None]


def gate_calls(obj, method, thread_name):
    """Block the first `method` call made from thread `thread_name` until released."""
    entered, release = threading.Event(), threading.Event()
    orig = getattr(obj, method)

    def gated(*a, **k):
        if threading.current_thread().name == thread_name and not entered.is_set():
            entered.set()
            release.wait(5)
        return orig(*a, **k)
    setattr(obj, method, gated)
    return entered, release


# --- R2: admission and notifier share the thread lock --------------------------------------------

def test_r2_send_worker_and_notifier_create_one_root(env):
    env.herdr.add_agent("w2:p3", "done", name="pc", session="S")
    env.herdr.screens["w2:p3"] = SHORT
    env.herdr.after_prompt = None
    completion = transition(env, "w2:p3", "working", "done")  # PC work finished, queued
    agent = env.herdr.find_agent("w2:p3")
    entered, release = gate_calls(env.transport, "post_message", "worker")
    worker = threading.Thread(target=env.bridge.send, args=(target_of(agent), "hi"), name="worker")
    worker.start()
    assert entered.wait(5)  # the send worker is inside its root post
    notifier = threading.Thread(target=env.bridge.handle_transition, args=(completion,), name="notifier")
    notifier.start()
    time.sleep(0.1)
    assert notifier.is_alive() and env.transport.posts == []  # waits for admission instead of racing it
    release.set()
    worker.join(5)
    notifier.join(5)
    assert len(roots(env)) == 1
    root_ts = roots(env)[0]["ts"]
    assert env.state.get_thread("S")["thread_ts"] == root_ts
    assert [p["thread_ts"] for p in results_posted(env)] == [root_ts]
    assert len(env.herdr.prompts()) == 1


# --- R5: resume can't clear a task admitted after its snapshot ----------------------------------

def test_r5_resume_does_not_clear_newer_task(tmp_path):
    env = make_env(tmp_path, start=False)
    env.herdr.add_agent("w1:p1", "done", name="coder", session="S1")
    env.herdr.screens["w1:p1"] = SHORT
    env.state.upsert_thread("S1", channel="D-OWNER", thread_ts="1.0", agent_name="coder", pane_id="w1:p1",
                            pending_task={"task_id": "A", "started_at": env.clock() - 30, "seq0": 0,
                                          "working_announced": True})
    entered, release = gate_calls(env.herdr, "find_agent", "resume")
    resume = threading.Thread(target=env.bridge.handle_resume, args=("S1", "A"), name="resume")
    resume.start()
    assert entered.wait(5)  # resume holds A's done snapshot
    worker = threading.Thread(target=env.bridge.send,
                              args=(target_of(env.herdr.find_agent("w1:p1")), "task B"), name="worker")
    worker.start()
    time.sleep(0.1)
    assert worker.is_alive() and env.herdr.prompts() == []  # admission waits for resume
    release.set()
    resume.join(5)
    worker.join(5)
    assert len(results_posted(env)) == 1  # A's result, once
    pending = env.state.get_thread("S1")["pending_task"]
    assert pending is not None and pending["prompt"] == "task B"
    assert env.herdr.find_agent("w1:p1")["agent_status"] == "working"
    assert [p[2] for p in env.herdr.prompts()] == ["task B"]


def test_r5_resume_ignores_a_different_task(env):
    env.herdr.add_agent("w1:p1", "done", name="coder", session="S1")
    env.state.upsert_thread("S1", channel="D-OWNER", thread_ts="1.0", pane_id="w1:p1",
                            pending_task={"task_id": "B", "started_at": env.clock(), "seq0": 0})
    env.bridge.handle_resume("S1", "A")  # A was settled; B is someone else's
    assert env.transport.posts == []
    assert env.state.get_thread("S1")["pending_task"]["task_id"] == "B"


def test_r5_resume_of_unstarted_task_does_not_claim_a_result(env):
    info = env.herdr.add_agent("w1:p1", "idle", name="coder", session="S1")
    env.state.upsert_thread("S1", channel="D-OWNER", thread_ts="1.0", agent_name="coder", pane_id="w1:p1",
                            pending_task={"task_id": "A", "started_at": env.clock(),
                                          "seq0": info["state_change_seq"]})
    env.bridge.handle_resume("S1", "A")
    assert results_posted(env) == []
    assert "never started" in env.transport.posts[-1]["text"]
    assert env.state.get_thread("S1")["pending_task"] is None


# --- R9: accepted-but-unacknowledged posts are reconciled, not repeated ------------------------------

def _pending(env, session="S1", pane="w1:p1"):
    env.herdr.add_agent(pane, "done", name="coder", session=session)
    env.herdr.screens[pane] = SHORT
    env.state.upsert_thread(session, channel="D-OWNER", thread_ts="1.0", agent_name="coder", pane_id=pane,
                            pending_task={"task_id": "t1", "started_at": env.clock(), "seq0": 0,
                                          "working_announced": True})


def test_r9_result_accepted_then_response_lost_posts_once(env):
    _pending(env)
    env.transport.fail_posts = [Accepted()]
    assert env.bridge.process_with_retry(transition(env, "w1:p1", "working", "done"))
    assert len(results_posted(env)) == 1
    assert env.transport.finds == [("result:t1", "1.0")]
    entry = env.state.get_thread("S1")
    assert entry["pending_task"] is None and entry["last_result_seq"] > 0
    assert not entry.get("post_intents")


def test_r9_uncertain_post_that_was_not_accepted_is_posted_on_retry(env):
    _pending(env)
    env.transport.fail_posts = [SlackUncertainError("ReadTimeout")]  # lost before Slack stored it
    assert env.bridge.process_with_retry(transition(env, "w1:p1", "working", "done"))
    assert len(results_posted(env)) == 1


def test_r9_failed_lookup_is_retried_without_reposting(env):
    _pending(env)
    env.transport.fail_posts = [Accepted()]
    env.transport.fail_finds = [SlackTransientError("ratelimited", 3)]
    assert env.bridge.process_with_retry(transition(env, "w1:p1", "working", "done"))
    assert len(results_posted(env)) == 1 and len(env.transport.finds) == 2


def test_r9_pc_root_accepted_then_lost_is_reused(env):
    env.herdr.add_agent("w2:p3", "done", session="PC1")
    env.herdr.screens["w2:p3"] = SHORT
    env.transport.fail_posts = [Accepted()]  # the root
    assert env.bridge.process_with_retry(transition(env, "w2:p3", "working", "done"))
    assert len(roots(env)) == 1
    root_ts = roots(env)[0]["ts"]
    assert env.state.get_thread("PC1")["thread_ts"] == root_ts
    assert [p["thread_ts"] for p in results_posted(env)] == [root_ts]


def test_r9_send_root_accepted_then_lost_creates_one_bound_root(env):
    env.herdr.add_agent("w1:p1", "idle", name="coder", session="S1")
    env.transport.fail_posts = [Accepted()]
    run(env, "send coder hi")
    assert len(roots(env)) == 1
    entry = env.state.get_thread("S1")
    assert entry["thread_ts"] == roots(env)[0]["ts"] and entry.get("root_op") is None
    assert env.herdr.prompts() == [("prompt_agent", "w1:p1", "hi")]


def test_r9_root_intent_survives_a_restart(tmp_path):
    env = make_env(tmp_path)
    env.herdr.add_agent("w2:p3", "done", session="PC1")
    info = env.herdr.find_agent("w2:p3")
    env.transport.fail_posts = [Accepted()]
    with pytest.raises(SlackUncertainError):
        env.bridge._ensure_thread("PC1", info, "title", [], "pc")
    env.bridge.stop()
    persisted = env.state.get_thread("PC1")
    assert persisted["root_op"] and persisted["root_op"] in persisted["post_intents"]
    # A new bridge process (same state + Slack) must find the accepted root, not post another.
    bridge2 = Bridge(env.cfg, env.herdr, env.state, env.transport, ResultStore(tmp_path / "results"),
                     clock=env.clock, sleep=env.clock.sleep, executor=env.bridge.executor,
                     manager=env.bridge.manager)
    bridge2.dm_channel = "D-OWNER"
    entry = bridge2._ensure_thread("PC1", info, "title", [], "pc")
    assert len(roots(env)) == 1 and entry["thread_ts"] == roots(env)[0]["ts"]
    assert not entry.get("post_intents")


def test_r9_non_critical_notice_is_at_most_once(env):
    env.transport.fail_posts = [Accepted()]
    env.bridge._post_safe("⚠️ some notice")
    assert len(env.transport.posts) == 1 and env.transport.finds == []


def test_r9_definite_transient_error_is_still_retried(env):
    env.transport.fail_posts = [SlackTransientError("ratelimited", 2)]
    env.bridge._post_safe("⚠️ some notice")
    assert len(env.transport.posts) == 1 and 2 in env.sleeps


def test_r9_started_notice_is_idempotent(env):
    env.herdr.add_agent("w1:p1", "working", name="coder", session="S1")
    env.state.upsert_thread("S1", channel="D-OWNER", thread_ts="1.0", agent_name="coder", pane_id="w1:p1",
                            pending_task={"task_id": "t1", "started_at": env.clock(), "seq0": 0,
                                          "working_announced": False})
    env.transport.fail_posts = [Accepted()]
    assert env.bridge.process_with_retry(transition(env, "w1:p1", "idle", "working"))
    assert [p["text"] for p in env.transport.posts] == ["⏳ started (working)"]
    assert env.state.get_thread("S1")["pending_task"]["working_announced"] is True


# --- N1: the startup wait never adopts a replacement agent ---------------------------------------

@pytest.mark.parametrize("sessionless", [False, True])
def test_n1_replacement_during_startup_wait_is_not_prompted(env, sessionless):
    if sessionless:
        env.herdr.sessionless_kinds.add("codex")
    orig_start = env.herdr.start_agent

    def start_then_replace(*a, **k):
        result = orig_start(*a, **k)
        pane = result["agent"]["pane_id"]
        env.herdr.agents[pane]["agent_status"] = "unknown"
        result["agent"]["agent_status"] = "unknown"
        orig_find = env.herdr.find_agent

        def replaced(target):
            if env.herdr.agents[pane].get("name") != "intruder":
                b = env.herdr.add_agent(pane, "idle", name="intruder", session="B")
                b["terminal_id"] = "term-B"
            return orig_find(target)
        env.herdr.find_agent = replaced
        return result

    env.herdr.start_agent = start_then_replace
    run(env, "new Main " + ("kind=codex " if sessionless else "") + "prompt intended for original")
    assert env.herdr.prompts() == []
    assert env.state.get_thread("B") is None
    assert any("replaced by a different agent" in r["text"] for r in env.transport.responses)


def test_n1_ordinary_unknown_then_idle_still_works(env):
    orig_start = env.herdr.start_agent

    def start_unknown(*a, **k):
        result = orig_start(*a, **k)
        pane = result["agent"]["pane_id"]
        env.herdr.agents[pane]["agent_status"] = "unknown"
        result["agent"]["agent_status"] = "unknown"
        orig_find = env.herdr.find_agent

        def settle(target):
            env.herdr.agents[pane]["agent_status"] = "idle"
            return orig_find(target)
        env.herdr.find_agent = settle
        return result

    env.herdr.start_agent = start_unknown
    run(env, "new Main hello")
    assert len(env.herdr.prompts()) == 1


# --- N2: provisional entries are reused only for the same terminal --------------------------------

def test_n2_send_after_terminal_replacement_gets_its_own_thread(env):
    env.state.upsert_thread("pending:old-terminal", channel="D-OWNER", thread_ts="7.0", pane_id="w1:p1",
                            terminal_id="old-terminal", provisional=True, agent_name="x")
    env.herdr.add_agent("w1:p1", "idle", name="cx", kind="codex", session=False)  # terminal term-w1:p1
    run(env, "send cx do it")
    key = "pending:term-w1:p1"
    entry = env.state.get_thread(key)
    assert entry["thread_ts"] != "7.0" and entry["pending_task"]["prompt"] == "do it"
    assert env.state.get_thread("pending:old-terminal").get("pending_task") is None
    env.herdr.set_status("w1:p1", "idle")
    env.herdr.screens["w1:p1"] = "• done\n"
    env.bridge.handle_transition(transition(env, "w1:p1", "working", "idle"))
    assert [p["thread_ts"] for p in results_posted(env)] == [entry["thread_ts"]]


def test_n2_new_agent_ignores_stale_pane_binding(env):
    env.herdr.sessionless_kinds.add("codex")
    env.state.upsert_thread("pending:old-terminal", channel="D-OWNER", thread_ts="7.0", pane_id="w1:p50",
                            terminal_id="old-terminal", provisional=True)
    run(env, "new Main kind=codex hi")
    entry = env.state.get_thread("pending:term-w1:p50")
    assert entry is not None and entry["thread_ts"] != "7.0" and entry["pending_task"]
    assert env.state.get_thread("pending:old-terminal").get("pending_task") is None
    assert len(roots(env)) == 1


def test_n2_matching_terminal_entry_elsewhere_is_found(env):
    # The agent moved: the pane id differs, the terminal matches -> reuse that thread.
    env.state.upsert_thread("pending:term-w1:p2", channel="D-OWNER", thread_ts="8.0", pane_id="w1:p9",
                            terminal_id="term-w1:p2", provisional=True)
    env.state.upsert_thread("pending:stale", channel="D-OWNER", thread_ts="9.0", pane_id="w1:p2",
                            terminal_id="other", provisional=True)
    env.herdr.add_agent("w1:p2", "idle", name="cx", kind="codex", session=False)
    run(env, "send cx go")
    assert env.state.get_thread("pending:term-w1:p2")["pending_task"]["prompt"] == "go"
    assert env.transport.posts[-1]["thread_ts"] == "8.0"


# --- N3: a resume retry follows its task after provisional migration ---------------------------------

def test_n3_resume_retry_after_migration_delivers_once(env):
    env.herdr.add_agent("w1:p1", "done", name="cx", kind="codex", session="REAL")
    env.herdr.screens["w1:p1"] = "• done\n"
    env.state.upsert_thread("pending:term-w1:p1", channel="D-OWNER", thread_ts="8.0", pane_id="w1:p1",
                            terminal_id="term-w1:p1", provisional=True, agent_name="cx",
                            pending_task={"task_id": "t", "started_at": env.clock() - 5, "seq0": 0,
                                          "working_announced": True})
    env.transport.fail_posts = [SlackTransientError("timeout")]  # first result post rejected
    assert env.bridge.process_with_retry(_Resume("pending:term-w1:p1", "t"))
    assert len(results_posted(env)) == 1 and results_posted(env)[0]["thread_ts"] == "8.0"
    assert env.state.get_thread("pending:term-w1:p1") is None
    assert env.state.get_thread("REAL")["pending_task"] is None


def test_n3_start_queues_resume_with_task_id(tmp_path):
    env = make_env(tmp_path, start=False, executor=DeferredExecutor())
    env.state.upsert_thread("S1", channel="D-OWNER", thread_ts="1.0", pane_id="w1:p1",
                            pending_task={"task_id": "tid", "started_at": env.clock(), "seq0": 0})
    queued = []
    orig_put = env.bridge._notify_q.put
    env.bridge._notify_q.put = lambda item: (queued.append(item), orig_put(item))
    env.bridge.start()
    assert _Resume("S1", "tid") in queued
    env.bridge.stop()
