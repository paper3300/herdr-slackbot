"""Answering blocked dialogs from Slack (docs/progress/BLOCKED-ANSWER.md)."""

import json
import threading
import time

from conftest import load_transcript
from herdr_slackbot import blocks as B
from herdr_slackbot.bridge import DIALOG_BUTTONS_TEXT, DIALOG_GONE_TEXT, DIALOG_STALE_TEXT
from herdr_slackbot.herdr_client import HerdrError
from test_bridge import SHORT, env, make_env, run, transition  # noqa: F401 (env fixture)

PERMISSION = load_transcript("dialog_claude_permission.txt")
ASK_SINGLE = load_transcript("dialog_ask_single.txt")
ASK_MULTI = load_transcript("dialog_ask_multi.txt")
ASK_REVIEW = load_transcript("dialog_ask_review.txt")
ASK_TYPE = load_transcript("dialog_ask_type.txt")
PLAN = load_transcript("dialog_plan.txt")
CLAUDE_TRUST = load_transcript("dialog_claude_trust.txt")
CODEX_TRUST = load_transcript("dialog_codex_trust.txt")
CODEX_APPROVAL = load_transcript("dialog_codex_approval.txt")
CODEX_IDLE = load_transcript("codex_idle.txt")


def replies(env, text):
    """All response_url replies of one slash command (the worker's reply comes before the ack text)."""
    before = len(env.transport.responses)
    run(env, text)
    return " | ".join(r["text"] for r in env.transport.responses[before:])


def block(env, screen, pane="w1:p1", kind="claude", session="S1", name="coder"):
    """An agent that becomes blocked on `screen`; returns its pending dialog record."""
    env.herdr.add_agent(pane, "blocked", name=name, session=session, kind=kind)
    env.herdr.visible[pane] = screen
    env.bridge.handle_transition(transition(env, pane, "working", "blocked"))
    return env.state.get_thread(session)["dialog"]


def record(env, key="S1"):
    return (env.state.get_thread(key) or {}).get("dialog")


def message(rec):
    return {"ts": rec["message_ts"], "thread_ts": rec["thread_ts"], "text": "dialog",
            "blocks": [{"type": "section", "text": B.mrkdwn("q")}, {"type": "actions", "elements": []}]}


def click(env, rec, index=None, key=None):
    action = f"{B.ACTION_DIALOG_KEY}{key}" if key else f"{B.ACTION_DIALOG_OPTION}{index}"
    value = json.dumps({"t": rec["token"], "o": key or index})
    env.bridge.dialog_action(action, value, "TRIG", "D-OWNER", message(rec))


def keys_sent(env):
    return [c[2] for c in env.herdr.calls if c[0] == "send_keys"]


def updates_of(env, ts):
    return [u for u in env.transport.updates if u["ts"] == ts]


def last_text(update):
    return json.dumps(update["blocks"], ensure_ascii=False)


def becomes(env, status, screen="", pane="w1:p1", when=None):
    """on_keys script: after the keys (optionally only when `when` is among them) the agent moves on."""
    def on_keys(target, keys):
        if when is None or when in keys:
            env.herdr.set_status(pane, status)
            env.herdr.visible[pane] = screen
    return on_keys


def shows(env, screen, pane="w1:p1"):
    def on_keys(target, keys):
        env.herdr.visible[pane] = screen
    return on_keys


# --- posting --------------------------------------------------------------------------------------

def test_blocked_posts_dialog_with_buttons_and_record(env):
    rec = block(env, PERMISSION)
    post = env.transport.posts[-1]
    assert post["thread_ts"] == env.transport.posts[0]["ts"]  # in the agent's thread
    assert post["text"] == "⚠️ coder · Main is waiting for your answer"
    ids = [e["action_id"] for b in post["blocks"] if b["type"] == "actions" for e in b["elements"]]
    assert ids == ["dlg:opt:0", "dlg:opt:1", "dlg:opt:2", "dlg:key:esc", "dlg:screen"]
    assert rec["message_ts"] == post["ts"] and rec["kind"] == "permission"
    assert rec["agent_session"] == "S1" and rec["pane_id"] == "w1:p1"
    assert [o["label"] for o in rec["options"]][0] == "Yes"
    assert env.state.get_thread("S1")["last_blocked_seq"]
    assert env.state.find_dialog(rec["token"]) == "S1"


def test_unparsed_screen_gets_a_keypad(env):
    rec = block(env, "some\nspinner output\n")
    assert rec["kind"] == "keypad"
    post = env.transport.posts[-1]
    assert "spinner output" in json.dumps(post["blocks"])
    env.herdr.on_keys = becomes(env, "working", when="enter")
    click(env, rec, key="enter")
    assert keys_sent(env) == [["enter"]]
    assert "✅ *Enter* — answered from Slack" in last_text(updates_of(env, rec["message_ts"])[-1])


def test_muted_pc_agent_gets_no_dialog(env):
    env.state.upsert_thread("S1", channel="D-OWNER", thread_ts="1.0", muted=True, pane_id="w1:p1")
    env.herdr.add_agent("w1:p1", "blocked", name="coder", session="S1")
    env.herdr.visible["w1:p1"] = PERMISSION
    env.bridge.handle_transition(transition(env, "w1:p1", "working", "blocked"))
    assert env.transport.posts == [] and record(env) is None


def test_plan_dialog_reads_the_plan_file(env, tmp_path):
    plans = tmp_path / "home" / ".claude" / "plans"
    plans.mkdir(parents=True)
    body = "# Big plan\n" + "\n".join(f"- step {i}: do the thing carefully" for i in range(200))
    (plans / "plan-create-b-txt-demo-slug-for-tests-abcdef.md").write_text(body, encoding="utf-8")
    env.bridge.home_dir = tmp_path / "home"
    block(env, PLAN)
    post = env.transport.posts[-1]
    text = json.dumps(post["blocks"], ensure_ascii=False)
    assert "*Big plan*" in text and "step 3" in text
    full = [e for b in post["blocks"] if b["type"] == "actions" for e in b["elements"]
            if e["action_id"] == B.ACTION_SHOW_FULL]
    assert full and env.bridge.results.load(full[0]["value"])[0] == body


def test_plan_dialog_falls_back_to_the_screen_excerpt(env, tmp_path):
    env.bridge.home_dir = tmp_path / "nowhere"
    block(env, PLAN)
    assert "Create b.txt in the working directory" in json.dumps(env.transport.posts[-1]["blocks"])


# --- answering ---------------------------------------------------------------------------------------

def test_click_sends_keys_and_marks_answered(env):
    rec = block(env, PERMISSION)
    env.herdr.on_keys = becomes(env, "working", "● Writing a.txt")
    click(env, rec, 0)
    assert keys_sent(env) == [["1"]]
    update = updates_of(env, rec["message_ts"])[-1]
    assert "✅ *1. Yes* — answered from Slack" in last_text(update)
    assert not any(b["type"] == "actions" for b in update["blocks"])
    assert record(env) is None


def test_stale_fingerprint_sends_nothing_and_rerenders(env):
    rec = block(env, PERMISSION)
    env.herdr.visible["w1:p1"] = PERMISSION.replace("echo spike > a.txt", "rm -rf build")
    click(env, rec, 0)
    assert keys_sent(env) == []
    update = updates_of(env, rec["message_ts"])[-1]
    assert "rm -rf build" in last_text(update) and "changed on PC" in last_text(update)
    new = record(env)
    assert new["token"] != rec["token"] and new["message_ts"] == rec["message_ts"]


def test_not_blocked_any_more_sends_nothing(env):
    rec = block(env, PERMISSION)
    env.herdr.set_status("w1:p1", "working")
    click(env, rec, 0)
    assert keys_sent(env) == []
    assert "Already answered or changed on PC" in last_text(updates_of(env, rec["message_ts"])[-1])
    assert record(env) is None


def test_other_session_in_the_pane_sends_nothing(env):
    rec = block(env, PERMISSION)
    env.herdr.add_agent("w1:p1", "blocked", name="coder", session="OTHER")
    env.herdr.visible["w1:p1"] = PERMISSION
    click(env, rec, 0)
    assert keys_sent(env) == []
    assert "ended" in last_text(updates_of(env, rec["message_ts"])[-1])


def test_unknown_token_on_an_unrelated_message_strips_its_buttons(env):
    rec = block(env, PERMISSION)
    click(env, dict(rec, token="nope", message_ts="77.7"), 0)  # e.g. a message from before an upgrade
    assert keys_sent(env) == []
    stripped = updates_of(env, "77.7")[-1]
    assert not any(b["type"] == "actions" for b in stripped["blocks"])
    assert DIALOG_GONE_TEXT in json.dumps(stripped["blocks"])
    assert env.transport.posts[-1]["text"] == DIALOG_GONE_TEXT
    assert updates_of(env, rec["message_ts"]) == []
    assert record(env)["token"] == rec["token"]  # the real record is untouched


def test_stale_token_on_the_live_message_keeps_its_buttons(env):
    """M1: a click from an older render of the same message must not wipe the current buttons."""
    rec = block(env, PERMISSION)
    click(env, dict(rec, token="older-render"), 0)
    assert keys_sent(env) == []
    assert updates_of(env, rec["message_ts"]) == []
    assert env.transport.posts[-1]["text"] == DIALOG_STALE_TEXT
    assert record(env)["token"] == rec["token"]


def test_multi_step_question_edits_the_same_message(env):
    rec = block(env, ASK_SINGLE)
    posts_before = len(env.transport.posts)
    env.herdr.on_keys = shows(env, ASK_MULTI)
    click(env, rec, 1)  # Blue -> the next question tab
    rec2 = record(env)
    assert rec2["token"] != rec["token"] and rec2["message_ts"] == rec["message_ts"]
    assert "Pick fruits?" in last_text(updates_of(env, rec["message_ts"])[-1])
    env.herdr.on_keys = shows(env, ASK_MULTI.replace("1. [ ] Apple", "1. [✔] Apple"))
    click(env, rec2, 0)  # toggle Apple
    rec3 = record(env)
    assert "☑ 1. Apple" in last_text(updates_of(env, rec["message_ts"])[-1])
    env.herdr.on_keys = shows(env, ASK_REVIEW)
    click(env, rec3, key="right")  # Next -> review
    rec4 = record(env)
    assert rec4["kind"] == "question_review"
    env.herdr.on_keys = becomes(env, "working", "● thinking")
    click(env, rec4, 0)  # Submit answers
    assert keys_sent(env) == [["2"], ["1"], ["right"], ["1"]]
    assert "✅ *1. Submit answers* — answered from Slack" in last_text(updates_of(env, rec["message_ts"])[-1])
    assert len(env.transport.posts) == posts_before  # never a second dialog message
    assert record(env) is None


def test_changed_screen_must_settle_before_it_is_shown(env):
    rec = block(env, ASK_SINGLE)
    frames = iter([ASK_SINGLE.replace("Pick a color?", "Pick a colo"), ASK_MULTI, ASK_MULTI])
    env.herdr.on_keys = shows(env, ASK_SINGLE)
    original = env.herdr.read_agent_once

    def read(target, lines, source="recent_unwrapped"):
        if keys_sent(env):
            env.herdr.visible["w1:p1"] = next(frames, ASK_MULTI)
        return original(target, lines, source)
    env.herdr.read_agent_once = read
    click(env, rec, 1)
    assert "Pick fruits?" in last_text(updates_of(env, rec["message_ts"])[-1])  # not the partial frame


def test_no_change_after_timeout_keeps_buttons_and_warns(env):
    rec = block(env, PERMISSION)
    click(env, rec, 0)  # the screen never changes
    assert keys_sent(env) == [["1"]]
    update = updates_of(env, rec["message_ts"])[-1]  # same buttons, new token, with the note
    assert any(b["type"] == "actions" for b in update["blocks"])
    assert "Could not confirm the answer" in last_text(update)
    warn = env.transport.posts[-1]
    assert warn["text"] == "⚠️ Could not confirm the answer; check the screen."
    assert warn["thread_ts"] == rec["thread_ts"]
    assert "dlg:screen" in json.dumps(warn["blocks"])
    new = record(env)
    assert new["token"] != rec["token"] and new["fingerprint"] == rec["fingerprint"]


def test_click_queued_behind_an_unconfirmed_answer_sends_nothing(env):
    """N8: the second click of a double click, after "could not confirm", is out of date."""
    rec = block(env, PERMISSION)
    click(env, rec, 0)
    click(env, rec, 0)  # same (old) token
    assert keys_sent(env) == [["1"]]
    assert env.transport.posts[-1]["text"] == DIALOG_STALE_TEXT


def test_herdr_error_while_sending_is_reported(env):
    rec = block(env, PERMISSION)
    env.herdr.input_error = HerdrError("agent_not_found", "gone")
    click(env, rec, 0)
    assert "Could not send the answer" in env.transport.posts[-1]["text"]
    assert record(env)["token"] == rec["token"]


def test_double_click_sends_keys_once(env):
    rec = block(env, PERMISSION)
    entered, release = threading.Event(), threading.Event()

    def on_keys(target, keys):
        entered.set()
        release.wait(5)
        env.herdr.set_status("w1:p1", "working")
    env.herdr.on_keys = on_keys
    first = threading.Thread(target=env.bridge.answer_dialog, args=(rec["token"], ("opt", 0), "D-OWNER", message(rec)))
    second = threading.Thread(target=env.bridge.answer_dialog, args=(rec["token"], ("opt", 0), "D-OWNER", message(rec)))
    first.start()
    assert entered.wait(5)
    second.start()
    time.sleep(0.1)  # the second click waits for the answer lock
    release.set()
    first.join(5)
    second.join(5)
    assert keys_sent(env) == [["1"]]
    assert DIALOG_GONE_TEXT in env.transport.texts()


def test_show_screen_posts_the_screen_tail(env):
    rec = block(env, PERMISSION)
    env.bridge.dialog_action(B.ACTION_DIALOG_SCREEN, json.dumps({"t": rec["token"], "o": "screen"}), "TRIG",
                             "D-OWNER", message(rec))
    post = env.transport.posts[-1]
    assert post["thread_ts"] == rec["thread_ts"]
    assert "Do you want to proceed?" in post["blocks"][0]["text"]["text"]
    assert post["blocks"][0]["text"]["text"].startswith("```")


# --- free text -----------------------------------------------------------------------------------

def test_free_text_via_modal(env):
    rec = block(env, ASK_TYPE)
    env.bridge.dialog_action(f"{B.ACTION_DIALOG_TEXT}2", json.dumps({"t": rec["token"], "o": 2}), "TRIG",
                             "D-OWNER", message(rec))
    view = env.transport.views_opened[-1]["view"]
    assert view["callback_id"] == B.DIALOG_TEXT_CALLBACK and env.transport.views_opened[-1]["trigger_id"] == "TRIG"
    assert keys_sent(env) == []  # opening the modal sends nothing
    view["state"] = {"values": {B.BLOCK_DIALOG_TEXT: {B.ACTION_VALUE: {"value": ""}}}}
    assert env.bridge.submit_dialog_text(view) == {B.BLOCK_DIALOG_TEXT: "Enter an answer."}
    env.herdr.on_keys = becomes(env, "working", "● ok", when="enter")
    view["state"] = {"values": {B.BLOCK_DIALOG_TEXT: {B.ACTION_VALUE: {"value": "감마\r\nplease"}}}}
    assert env.bridge.submit_dialog_text(view) is None
    # Newlines go as they are (they break the line in the input, they do not submit); CRLF -> LF.
    assert env.herdr.inputs() == [("send_keys", "w1:p1", ["3"]), ("send_text", "w1:p1", "감마\nplease"),
                                  ("send_keys", "w1:p1", ["enter"])]
    assert "3. Type something.: 감마 please" in last_text(updates_of(env, rec["message_ts"])[-1])


def test_free_text_modal_for_a_closed_dialog(env):
    env.bridge.dialog_action(f"{B.ACTION_DIALOG_TEXT}2", json.dumps({"t": "gone", "o": 2}), "TRIG", "D-OWNER", {})
    assert DIALOG_GONE_TEXT in json.dumps(env.transport.views_opened[-1]["view"])


def test_free_text_via_thread_reply(env):
    rec = block(env, PLAN)
    env.herdr.on_keys = becomes(env, "working", "● replanning", when="enter")
    env.bridge.handle_dm_message("D-OWNER", "UOWNER", "use a smaller step", "9.9", rec["thread_ts"])
    assert env.herdr.inputs() == [("send_keys", "w1:p1", ["3"]), ("send_text", "w1:p1", "use a smaller step"),
                                  ("send_keys", "w1:p1", ["enter"])]
    assert env.herdr.prompts() == []
    assert "answered from Slack" in last_text(updates_of(env, rec["message_ts"])[-1])


def test_thread_reply_without_a_free_text_option(env):
    rec = block(env, PERMISSION)
    env.bridge.handle_dm_message("D-OWNER", "UOWNER", "yes please", "9.9", rec["thread_ts"])
    assert keys_sent(env) == [] and env.herdr.prompts() == []
    assert env.transport.posts[-1]["text"] == DIALOG_BUTTONS_TEXT
    assert env.transport.posts[-1]["thread_ts"] == rec["thread_ts"]


def test_thread_reply_after_the_dialog_was_answered_is_a_prompt(env):
    rec = block(env, PERMISSION)
    env.herdr.set_status("w1:p1", "idle")
    env.bridge.handle_dm_message("D-OWNER", "UOWNER", "next task", "9.9", rec["thread_ts"])
    assert keys_sent(env) == []
    assert env.herdr.prompts() == [("prompt_agent", "w1:p1", "next task")]
    assert record(env) is None


def test_send_command_still_rejects_a_blocked_agent(env):
    block(env, ASK_TYPE)
    assert "use the buttons in its thread" in run(env, "send coder hello")
    assert env.herdr.inputs() == [] and env.herdr.prompts() == []


# --- answered elsewhere --------------------------------------------------------------------------------

def test_answered_on_pc_strips_the_buttons(env):
    rec = block(env, PERMISSION)
    env.herdr.set_status("w1:p1", "working")
    env.bridge.handle_transition(transition(env, "w1:p1", "blocked", "working"))
    update = updates_of(env, rec["message_ts"])[-1]
    assert "answered on PC" in last_text(update) and not any(b["type"] == "actions" for b in update["blocks"])
    assert record(env) is None


def test_transition_keeps_a_dialog_that_is_still_open(env):
    rec = block(env, ASK_SINGLE)
    env.herdr.visible["w1:p1"] = ASK_SINGLE.replace("❯ 1. Red", "  1. Red").replace("  2. Blue", "❯ 2. Blue")
    env.bridge.handle_transition(transition(env, "w1:p1", "unknown", "blocked"))  # cursor moved only
    assert updates_of(env, rec["message_ts"]) == []
    assert record(env)["token"] == rec["token"]
    assert sum(1 for p in env.transport.posts if "waiting for your answer" in p["text"]) == 1


def test_new_dialog_on_pc_replaces_the_old_message(env):
    rec = block(env, PERMISSION)
    env.herdr.visible["w1:p1"] = ASK_SINGLE
    env.herdr.set_status("w1:p1", "blocked")  # a new state (seq)
    env.bridge.handle_transition(transition(env, "w1:p1", "working", "blocked"))
    assert "answered on PC" in last_text(updates_of(env, rec["message_ts"])[-1])
    new = record(env)
    assert new["kind"] == "question" and new["message_ts"] != rec["message_ts"]


def test_ended_agent_closes_the_dialog(env):
    from herdr_slackbot.events import AgentTransition
    rec = block(env, PERMISSION)
    env.herdr.agents.pop("w1:p1")
    env.bridge.handle_transition(AgentTransition("w1:p1", "w1", "blocked", "unknown", env.clock(), "S1", "coder",
                                                 None, ended=True))
    assert "ended" in last_text(updates_of(env, rec["message_ts"])[-1])
    assert record(env) is None


def test_codex_approval_dialog(env):
    rec = block(env, CODEX_APPROVAL, kind="codex")
    assert rec["kind"] == "codex_approval"
    env.herdr.on_keys = becomes(env, "working", "• Running", when="3")
    click(env, rec, 2)
    assert keys_sent(env) == [["3"]]
    assert "3. No, and tell Codex what to do differently (esc)" in last_text(updates_of(env, rec["message_ts"])[-1])


# --- startup dialogs --------------------------------------------------------------------------------------

def start_blocked_on(env, screen):
    """start_agent registers the agent, which is then stuck on `screen` (Herdr: agent_not_ready)."""
    def start(name, kind, pane_id, args=(), timeout_ms=None):
        env.herdr.calls.append(("start_agent", name, kind, pane_id, list(args)))
        env.herdr.add_agent(pane_id, "blocked", name=name, kind=kind)
        env.herdr.visible[pane_id] = screen
        raise HerdrError("agent_not_ready", "blocked")
    env.herdr.start_agent = start


def test_claude_startup_trust_dialog_then_prompt(env):
    start_blocked_on(env, CLAUDE_TRUST)
    reply = replies(env, "new Main hello there")
    assert "waiting for an answer during startup" in reply
    pane = next(c for c in env.herdr.calls if c[0] == "start_agent")[3]
    key = env.state.find_session_by_thread("D-OWNER", env.transport.posts[0]["ts"])
    entry = env.state.get_thread(key)
    assert entry["origin"] == "slack" and entry["deferred_prompt"]["text"] == "hello there"
    rec = entry["dialog"]
    assert rec["kind"] == "trust"
    assert "📨 hello there" in json.dumps(env.transport.posts[0]["blocks"], ensure_ascii=False)
    assert env.herdr.prompts() == []
    env.herdr.on_keys = becomes(env, "idle", SHORT, pane=pane, when="enter")
    click(env, rec, 1)  # "Yes, I trust this folder"
    assert keys_sent(env) == [["down", "enter"]]
    assert "Yes, I trust this folder" in last_text(updates_of(env, rec["message_ts"])[-1])
    assert env.herdr.prompts() == [("prompt_agent", pane, "hello there")]
    assert env.state.get_thread(key)["deferred_prompt"] is None
    assert env.transport.posts[-1]["text"].startswith("📨 hello there")  # echoed in the thread


def test_claude_startup_trust_answered_on_pc_sends_the_prompt(env):
    start_blocked_on(env, CLAUDE_TRUST)
    run(env, "new Main hello")
    pane = next(c for c in env.herdr.calls if c[0] == "start_agent")[3]
    env.herdr.set_status(pane, "idle")
    env.herdr.visible[pane] = SHORT
    env.bridge.handle_transition(transition(env, pane, "blocked", "idle"))
    assert env.herdr.prompts() == [("prompt_agent", pane, "hello")]
    assert any("answered on PC" in last_text(u) for u in env.transport.updates)


def test_startup_not_ready_without_a_dialog_keeps_the_old_notice(env):
    env.herdr.start_error = HerdrError("agent_not_ready", "slow")
    assert "Confirm it there, then use send" in replies(env, "new Main hi")


def test_codex_startup_trust_reported_idle(env):
    env.herdr.visible["w2:p50"] = CODEX_TRUST  # the first tab the fake creates in w2
    reply = replies(env, "new w2 kind=codex hi codex")
    assert "waiting for an answer during startup" in reply
    assert env.herdr.prompts() == []
    key = env.state.find_session_by_thread("D-OWNER", env.transport.posts[0]["ts"])
    rec = env.state.get_thread(key)["dialog"]
    assert rec["kind"] == "trust" and rec["idle"] is True
    env.herdr.on_keys = shows(env, CODEX_IDLE, pane="w2:p50")
    click(env, rec, 0)  # "1. Trust and continue" (Codex stays idle throughout)
    assert keys_sent(env) == [["1"]]
    assert "answered from Slack" in last_text(updates_of(env, rec["message_ts"])[-1])
    assert env.herdr.prompts() == [("prompt_agent", "w2:p50", "hi codex")]
    assert env.cfg.codex_prompt_delay in env.sleeps


def test_codex_start_without_trust_screen_prompts_directly(env):
    run(env, "new w2 kind=codex hi")
    assert len(env.herdr.prompts()) == 1
    assert all(record(env, k) is None for k in env.state.all_threads())


def test_deferred_prompt_waits_for_a_second_dialog(env):
    start_blocked_on(env, CLAUDE_TRUST)
    run(env, "new Main hello")
    pane = next(c for c in env.herdr.calls if c[0] == "start_agent")[3]
    key = env.state.find_session_by_thread("D-OWNER", env.transport.posts[0]["ts"])
    rec = env.state.get_thread(key)["dialog"]
    env.herdr.on_keys = becomes(env, "unknown", SHORT, pane=pane, when="enter")
    original = env.herdr.find_agent

    def find(target):
        info = original(target)
        if info and info["agent_status"] == "unknown" and record(env, key) is None:
            env.herdr.set_status(pane, "blocked")  # another dialog right after the trust screen
            info = original(target)
        return info
    env.herdr.find_agent = find
    click(env, rec, 1)
    assert "answered from Slack" in last_text(updates_of(env, rec["message_ts"])[-1])
    assert env.herdr.prompts() == []
    assert env.state.get_thread(key)["deferred_prompt"]["text"] == "hello"  # kept for the next answer


def test_start_error_other_than_not_ready_unchanged(tmp_path):
    e = make_env(tmp_path)
    try:
        e.herdr.start_error = HerdrError("agent_start_failed", "boom")
        assert "Could not start the agent" in replies(e, "new Main hi")
    finally:
        e.bridge.stop()


# --- review round 1 ---------------------------------------------------------------------------------

def test_stale_click_after_a_rerender_keeps_the_new_buttons(env):
    """M1: tap Apple, then quickly Cherry (sent with the first render's token)."""
    rec = block(env, ASK_MULTI)
    env.herdr.on_keys = shows(env, ASK_MULTI.replace("1. [ ] Apple", "1. [✔] Apple"))
    click(env, rec, 0)
    current = updates_of(env, rec["message_ts"])[-1]
    assert record(env)["token"] != rec["token"]
    click(env, rec, 2)  # the Cherry click still carries the old token
    assert keys_sent(env) == [["1"]]
    assert updates_of(env, rec["message_ts"])[-1] is current  # not overwritten
    assert any(b["type"] == "actions" for b in current["blocks"])
    assert env.transport.posts[-1]["text"] == DIALOG_STALE_TEXT
    assert record(env) is not None


def test_double_click_keeps_the_answered_outcome(env):
    rec = block(env, PERMISSION)
    env.herdr.on_keys = becomes(env, "working", "● go")
    click(env, rec, 0)
    click(env, rec, 0)
    assert keys_sent(env) == [["1"]]
    assert "answered from Slack" in last_text(updates_of(env, rec["message_ts"])[-1])
    assert len(updates_of(env, rec["message_ts"])) == 1
    assert env.transport.posts[-1]["text"] == DIALOG_GONE_TEXT
    assert env.state.get_thread("S1")["closed_dialogs"] == [rec["message_ts"]]


def test_replan_behind_an_open_record_sends_nothing(env):
    """M2: the plan changed on PC (same question, same options): no approval goes out."""
    rec = block(env, PLAN)
    env.herdr.visible["w1:p1"] = PLAN.replace("Create b.txt in the working directory", "Delete every file")
    click(env, rec, 0)
    assert keys_sent(env) == []
    update = updates_of(env, rec["message_ts"])[-1]
    assert "Delete every file" in last_text(update) and "changed on PC" in last_text(update)


def test_plan_file_rewritten_behind_an_open_record_sends_nothing(env, tmp_path):
    plans = tmp_path / "home" / ".claude" / "plans"
    plans.mkdir(parents=True)
    plan = plans / "plan-create-b-txt-demo-slug-for-tests-abcdef.md"
    plan.write_text("# Plan A\n- safe step", encoding="utf-8")
    env.bridge.home_dir = tmp_path / "home"
    rec = block(env, PLAN)
    assert rec["plan_hash"]
    plan.write_text("# Plan B\n- rm -rf everything", encoding="utf-8")
    click(env, rec, 0)
    assert keys_sent(env) == []
    assert "rm -rf everything" in last_text(updates_of(env, rec["message_ts"])[-1])


def test_retried_post_commits_the_record_of_the_posted_message(env):
    """M3: first dialog post times out (accepted), the screen moves on before the retry."""
    from fakes import Accepted
    from herdr_slackbot.dialog import parse_dialog
    from herdr_slackbot.slack_transport import SlackUncertainError
    env.herdr.add_agent("w1:p1", "blocked", name="coder", session="S1")
    env.herdr.visible["w1:p1"] = PERMISSION
    t = transition(env, "w1:p1", "working", "blocked")
    env.transport.fail_posts = [None, Accepted()]  # thread root ok, dialog accepted but unanswered
    try:
        env.bridge.handle_transition(t)
    except SlackUncertainError:
        pass
    env.herdr.visible["w1:p1"] = ASK_SINGLE  # answered on PC meanwhile: another dialog
    env.clock.now += 10
    env.bridge.handle_transition(t)  # the retry reuses the accepted message
    dialog_posts = [p for p in env.transport.posts if "waiting for your answer" in p["text"]]
    assert len(dialog_posts) == 1
    rec = record(env)
    assert rec["message_ts"] == dialog_posts[0]["ts"]
    assert rec["token"] in json.dumps(dialog_posts[0]["blocks"])
    assert rec["fingerprint"] == parse_dialog(PERMISSION, "claude").fingerprint  # what the message shows
    assert env.state.get_thread("S1").get("dialog_pending") is None
    click(env, rec, 1)  # "2. Yes, and always allow" on the message
    assert keys_sent(env) == []  # never pressed on the color question
    assert "Pick a color?" in last_text(updates_of(env, rec["message_ts"])[-1])


def test_keypad_click_when_the_screen_now_parses_sends_nothing(env):
    """N1: the user never saw the real dialog."""
    rec = block(env, "drawing…\n")
    assert rec["kind"] == "keypad"
    env.herdr.visible["w1:p1"] = PERMISSION
    click(env, rec, key="1")
    assert keys_sent(env) == []
    assert record(env)["kind"] == "permission"
    assert "changed on PC" in last_text(updates_of(env, rec["message_ts"])[-1])


def test_unparsed_screen_is_read_again_before_a_keypad(env):
    reads = iter(["", "still drawing", PERMISSION])
    original = env.herdr.read_agent_once

    def read(target, lines, source="recent_unwrapped"):
        env.herdr.visible[target] = next(reads, PERMISSION)
        return original(target, lines, source)
    env.herdr.read_agent_once = read
    env.herdr.add_agent("w1:p1", "blocked", name="coder", session="S1")
    env.bridge.handle_transition(transition(env, "w1:p1", "working", "blocked"))
    assert record(env)["kind"] == "permission"


def test_a_click_in_flight_does_not_block_sends_to_other_agents(env):
    """N3: the transition waits for the click's answer lock without holding the admission lock."""
    from herdr_slackbot.bridge import target_of
    rec = block(env, PERMISSION)
    env.herdr.add_agent("w1:p2", "idle", name="other", session="S2")
    entered, release = threading.Event(), threading.Event()

    def on_keys(target, keys):
        entered.set()
        release.wait(5)
        env.herdr.set_status("w1:p1", "working")
    env.herdr.on_keys = on_keys
    clicker = threading.Thread(target=env.bridge.answer_dialog, args=(rec["token"], ("opt", 0)))
    clicker.start()
    assert entered.wait(5)
    notifier = threading.Thread(target=env.bridge.handle_transition,
                                args=(transition(env, "w1:p1", "blocked", "working"),))
    notifier.start()
    time.sleep(0.1)  # the transition now waits for the answer lock
    sender = threading.Thread(target=env.bridge.send, args=(target_of(env.herdr.find_agent("w1:p2")), "hi"))
    sender.start()
    sender.join(3)
    stuck = sender.is_alive()
    release.set()
    for th in (clicker, notifier, sender):
        th.join(5)
    assert not stuck
    assert ("prompt_agent", "w1:p2", "hi") in env.herdr.prompts()
    assert "answered from Slack" in last_text(updates_of(env, rec["message_ts"])[-1])


def start_codex_sessionless_trust(env):
    env.herdr.sessionless_kinds = {"codex"}
    env.herdr.visible["w2:p50"] = CODEX_TRUST
    run(env, "new w2 kind=codex hi codex")
    key = env.state.find_provisional_by_terminal("term-w2:p50")
    assert key and key.startswith("pending:")
    return key, env.state.get_thread(key)["dialog"]


def test_answer_lock_is_stable_across_rekeying(env):
    """N4: the transition after the re-key takes the same lock as the click."""
    from herdr_slackbot.events import AgentTransition
    key, rec = start_codex_sessionless_trust(env)
    info = dict(env.herdr.find_agent("w2:p50"), agent_session={"value": "S9"})
    t = AgentTransition("w2:p50", "w2", "idle", "working", env.clock(), "S9", "slack-1", info)
    assert env.bridge._transition_answer_id(t) == env.bridge._answer_id(rec["terminal_id"], key)


def test_rekey_while_a_click_is_polling(env):
    key, rec = start_codex_sessionless_trust(env)

    def on_keys(target, keys):  # the session appears and the thread is re-keyed meanwhile
        env.herdr.agents["w2:p50"]["agent_session"] = {"value": "S9"}
        env.state.rekey_thread(key, "S9")
        env.herdr.visible["w2:p50"] = CODEX_IDLE
    env.herdr.on_keys = on_keys
    click(env, rec, 0)
    assert "answered from Slack" in last_text(updates_of(env, rec["message_ts"])[-1])
    entry = env.state.get_thread("S9")
    assert entry["dialog"] is None and entry["closed_dialogs"] == [rec["message_ts"]]
    assert env.herdr.prompts() == [("prompt_agent", "w2:p50", "hi codex")]


def test_thread_reply_after_a_rekey(env):
    from herdr_slackbot.bridge import Target
    key, rec = start_codex_sessionless_trust(env)
    target = Target("w2:p50", None, "term-w2:p50", "slack-1", key)
    env.state.rekey_thread(key, "S9")
    env.bridge.answer_thread_reply(key, target, "yes", rec["thread_ts"])
    assert env.transport.posts[-1]["text"] == DIALOG_BUTTONS_TEXT  # found the moved record
    assert keys_sent(env) == [] and env.herdr.prompts() == []


def test_codex_trust_drawn_during_the_prompt_delay(env):
    """N5: the trust check runs after the delay."""
    original = env.bridge.sleep

    def sleep(seconds):
        if seconds == env.cfg.codex_prompt_delay:
            env.herdr.visible["w2:p50"] = CODEX_TRUST
        original(seconds)
    env.bridge.sleep = sleep
    assert "waiting for an answer during startup" in replies(env, "new w2 kind=codex hi")
    assert env.herdr.prompts() == []


def test_startup_dialog_already_posted_by_the_notifier(env):
    """N6: the PC path saw the new agent blocked first; the startup flow keeps that dialog."""
    def start(name, kind, pane_id, args=(), timeout_ms=None):
        env.herdr.calls.append(("start_agent", name, kind, pane_id, list(args)))
        env.herdr.add_agent(pane_id, "blocked", name=name, kind=kind)
        env.herdr.visible[pane_id] = CLAUDE_TRUST
        env.bridge.handle_transition(transition(env, pane_id, "unknown", "blocked"))
        raise HerdrError("agent_not_ready", "blocked")
    env.herdr.start_agent = start
    assert "waiting for an answer during startup" in replies(env, "new Main hello")
    dialogs = [p for p in env.transport.posts if "waiting for your answer" in p["text"]]
    assert len(dialogs) == 1
    key = env.state.find_session_by_thread("D-OWNER", env.transport.posts[0]["ts"])
    entry = env.state.get_thread(key)
    assert entry["dialog"]["message_ts"] == dialogs[0]["ts"]
    assert entry["deferred_prompt"]["text"] == "hello"


def restart(env):
    """Stop the bridge and start a new one on the same state file, Herdr and Slack."""
    from fakes import FakeManager, SyncExecutor
    from herdr_slackbot.bridge import Bridge
    from herdr_slackbot.results import ResultStore
    from herdr_slackbot.state import StateStore
    env.bridge.stop()
    env.state = StateStore(env.tmp / "state.json")
    env.bridge = Bridge(env.cfg, env.herdr, env.state, env.transport, ResultStore(env.tmp / "results"),
                        clock=env.clock, sleep=env.clock.sleep, executor=SyncExecutor(), manager=FakeManager(),
                        claude_projects=env.tmp / "projects", codex_home_dir=env.tmp / "codex", ack_budget=0.3)
    env.bridge.start()
    assert env.bridge.wait_idle()


def test_record_survives_a_restart_and_can_be_clicked(env):
    rec = block(env, PERMISSION)
    restart(env)
    assert record(env)["token"] == rec["token"]  # still open: nothing changed
    env.herdr.on_keys = becomes(env, "working", "● go")
    click(env, rec, 0)
    assert keys_sent(env) == [["1"]]
    assert "answered from Slack" in last_text(updates_of(env, rec["message_ts"])[-1])


def test_dialog_answered_while_the_bridge_was_down(env):
    rec = block(env, PERMISSION)
    env.herdr.set_status("w1:p1", "idle")
    restart(env)
    assert record(env) is None
    assert "answered on PC" in last_text(updates_of(env, rec["message_ts"])[-1])


def test_deferred_prompt_sent_after_a_restart(env):
    start_blocked_on(env, CLAUDE_TRUST)
    run(env, "new Main hello")
    pane = next(c for c in env.herdr.calls if c[0] == "start_agent")[3]
    env.herdr.set_status(pane, "idle")
    env.herdr.visible[pane] = SHORT
    restart(env)
    assert env.herdr.prompts() == [("prompt_agent", pane, "hello")]


def test_idle_trust_answered_on_pc_is_rechecked(env):
    """N7: Codex stays idle, so no transition follows; the idle recheck closes it and prompts."""
    env.herdr.visible["w2:p50"] = CODEX_TRUST
    run(env, "new w2 kind=codex hi codex")
    env.herdr.visible["w2:p50"] = CODEX_IDLE
    env.bridge._recheck_idle_dialogs()
    assert env.bridge.wait_idle()
    assert env.herdr.prompts() == [("prompt_agent", "w2:p50", "hi codex")]
    assert any("answered on PC" in last_text(u) for u in env.transport.updates)


def test_modal_submit_for_a_closed_dialog_replies_in_the_thread(env):
    """N10."""
    rec = block(env, ASK_TYPE)
    env.bridge.dialog_action(f"{B.ACTION_DIALOG_TEXT}2", json.dumps({"t": rec["token"], "o": 2}), "TRIG",
                             "D-OWNER", message(rec))
    view = env.transport.views_opened[-1]["view"]
    env.herdr.set_status("w1:p1", "working")
    env.bridge.handle_transition(transition(env, "w1:p1", "blocked", "working"))  # answered on PC
    view["state"] = {"values": {B.BLOCK_DIALOG_TEXT: {B.ACTION_VALUE: {"value": "late"}}}}
    env.bridge.submit_dialog_text(view)
    assert env.herdr.inputs() == []
    assert env.transport.posts[-1]["text"] == DIALOG_GONE_TEXT
    assert env.transport.posts[-1]["thread_ts"] == rec["thread_ts"]
    assert "answered on PC" in last_text(updates_of(env, rec["message_ts"])[-1])  # outcome kept


def test_resume_with_an_open_dialog_posts_nothing_new(env):
    """N11: restart resume of a Slack task whose agent is still on the posted dialog."""
    env.herdr.add_agent("w1:p1", "idle", name="coder", session="S1")
    run(env, "send coder do it")
    env.herdr.set_status("w1:p1", "blocked")
    env.herdr.visible["w1:p1"] = PERMISSION
    env.bridge.handle_transition(transition(env, "w1:p1", "working", "blocked"))
    rec = record(env)
    env.herdr.set_status("w1:p1", "blocked")  # a newer seq, same dialog
    env.bridge.handle_resume("S1")
    assert sum(1 for p in env.transport.posts if "waiting for your answer" in p["text"]) == 1
    entry = env.state.get_thread("S1")
    assert entry["dialog"]["token"] == rec["token"]
    assert entry["last_blocked_seq"] == env.herdr.agents["w1:p1"]["state_change_seq"]


def test_multi_select_free_text_moves_the_cursor_and_types_without_enter(env):
    """Live fact: the digit only toggles; typing on the row checks it; Enter would uncheck it."""
    rec = block(env, ASK_MULTI)
    env.bridge.dialog_action(f"{B.ACTION_DIALOG_TEXT}3", json.dumps({"t": rec["token"], "o": 3}), "TRIG",
                             "D-OWNER", message(rec))
    view = env.transport.views_opened[-1]["view"]
    typed = ASK_MULTI.replace("❯ 1. [ ] Apple", "  1. [ ] Apple").replace(
        "  4. [ ] Type something", "❯ 4. [✔] kiwi")
    env.herdr.on_text = lambda pane, text: env.herdr.visible.__setitem__("w1:p1", typed)
    view["state"] = {"values": {B.BLOCK_DIALOG_TEXT: {B.ACTION_VALUE: {"value": "kiwi\nmango"}}}}
    env.bridge.submit_dialog_text(view)
    assert env.herdr.inputs() == [("send_keys", "w1:p1", ["down", "down", "down"]),
                                  ("send_text", "w1:p1", "kiwi\nmango")]  # no Enter
    rec2 = record(env)
    assert rec2["token"] != rec["token"]
    env.herdr.on_keys = shows(env, ASK_REVIEW)
    click(env, rec2, key="submit")  # [Next →]
    assert env.herdr.inputs()[-1] == ("send_keys", "w1:p1", ["down", "enter"])  # from the live cursor
    assert record(env)["kind"] == "question_review"


def test_multi_select_next_from_the_first_row(env):
    rec = block(env, ASK_MULTI)
    env.herdr.on_keys = shows(env, ASK_REVIEW)
    click(env, rec, key="submit")
    assert keys_sent(env) == [["down"] * 4 + ["enter"]]


# --- review round 2 ---------------------------------------------------------------------------------

ASK_MULTI_TYPED = load_transcript("dialog_ask_multi_typed.txt")
ASK_MULTI_UNTYPED = ASK_MULTI_TYPED.replace("❯ 3. [✔] hello a\n         b\n", "❯ 3. [ ] Type something\n")


def test_restart_rerenders_a_changed_dialog_that_is_still_waiting(env):
    """R1: question 1 answered on PC while the bridge was down; question 2 waits."""
    rec = block(env, ASK_SINGLE)
    posts = len(env.transport.posts)
    env.herdr.visible["w1:p1"] = ASK_MULTI  # still blocked, a different dialog
    restart(env)
    new = record(env)
    assert new is not None and new["token"] != rec["token"] and new["message_ts"] == rec["message_ts"]
    assert new["kind"] == "question"
    update = updates_of(env, rec["message_ts"])[-1]
    assert "Pick fruits?" in last_text(update) and "changed on PC" in last_text(update)
    assert any(b["type"] == "actions" for b in update["blocks"])
    assert len(env.transport.posts) == posts  # the same message, nothing closed / lost
    env.herdr.on_keys = shows(env, ASK_MULTI.replace("1. [ ] Apple", "1. [✔] Apple"))
    click(env, new, 0)
    assert keys_sent(env) == [["1"]]


def test_restart_rerenders_a_keypad_whose_screen_now_parses(env):
    rec = block(env, "drawing…\n")
    assert rec["kind"] == "keypad"
    env.herdr.visible["w1:p1"] = PERMISSION
    restart(env)
    new = record(env)
    assert new is not None and new["kind"] == "permission" and new["token"] != rec["token"]
    assert "Do you want to proceed?" in last_text(updates_of(env, rec["message_ts"])[-1])


def test_transition_still_closes_a_changed_dialog(env):
    """The transition path keeps closing: its `blocked` transition posts the new dialog."""
    rec = block(env, ASK_SINGLE)
    env.herdr.visible["w1:p1"] = ASK_MULTI
    env.bridge.handle_transition(transition(env, "w1:p1", "blocked", "working"))
    assert "answered on PC" in last_text(updates_of(env, rec["message_ts"])[-1])
    assert record(env) is None


def test_multi_line_text_into_a_multi_select_row_then_next(env):
    """R2 (live fact): LF in the multi-select row breaks the line; no Enter; Next goes to Submit."""
    rec = block(env, ASK_MULTI_UNTYPED)
    typed = ASK_MULTI_TYPED
    env.bridge.dialog_action(f"{B.ACTION_DIALOG_TEXT}2", json.dumps({"t": rec["token"], "o": 2}), "TRIG",
                             "D-OWNER", message(rec))
    view = env.transport.views_opened[-1]["view"]
    env.herdr.on_text = lambda pane, text: env.herdr.visible.__setitem__("w1:p1", typed)
    view["state"] = {"values": {B.BLOCK_DIALOG_TEXT: {B.ACTION_VALUE: {"value": "hello a\nb"}}}}
    env.bridge.submit_dialog_text(view)
    assert env.herdr.inputs() == [("send_text", "w1:p1", "hello a\nb")]  # cursor already on the row
    rec2 = record(env)
    shown = last_text(updates_of(env, rec["message_ts"])[-1])
    assert "hello a ↵ b" in shown
    assert rec2["options"][2]["free_text"] and rec2["options"][2]["label"] == "hello a\nb"
    env.herdr.on_keys = shows(env, ASK_REVIEW)
    click(env, rec2, key="submit")
    assert env.herdr.inputs()[-1] == ("send_keys", "w1:p1", ["down", "enter"])
    assert record(env)["kind"] == "question_review"


def test_ended_transition_of_a_provisional_thread_takes_the_click_lock(env):
    """R4: an ended transition has no info; the terminal id is found through the pane."""
    from herdr_slackbot.events import AgentTransition
    key, rec = start_codex_sessionless_trust(env)
    t = AgentTransition("w2:p50", "w2", "idle", "unknown", env.clock(), None, "slack-1", None, ended=True)
    assert env.bridge._transition_answer_id(t) == env.bridge._answer_id(rec["terminal_id"], key)


def test_idle_recheck_runs_under_steady_traffic(env, monkeypatch):
    """R5: the recheck is due by time, not only after 15 s without any notifier work."""
    import herdr_slackbot.bridge as bridge_mod
    from herdr_slackbot.bridge import _Reconcile
    monkeypatch.setattr(bridge_mod, "IDLE_DIALOG_RECHECK", 0.05)
    calls = []
    monkeypatch.setattr(env.bridge, "_recheck_idle_dialogs", lambda: calls.append(1))
    env.bridge.stop()  # restart the notifier loop with the short interval
    env.bridge._stopping.clear()
    env.bridge._notify_thread = threading.Thread(target=env.bridge._notify_loop, daemon=True)
    env.bridge._notify_thread.start()
    deadline = time.time() + 0.5
    while time.time() < deadline:
        env.bridge._notify_q.put(_Reconcile("nobody"))  # never idle for 0.05 s
        time.sleep(0.01)
    assert env.bridge.wait_idle()
    assert calls


# --- review round 3 ---------------------------------------------------------------------------------

def test_modal_says_text_is_appended_and_shows_the_current_text(env):
    """N-B."""
    rec = block(env, ASK_MULTI_UNTYPED)
    env.herdr.on_text = lambda pane, text: env.herdr.visible.__setitem__("w1:p1", ASK_MULTI_TYPED)
    env.bridge.answer_dialog(rec["token"], ("text", 2, "hello a\nb"))
    rec2 = record(env)
    assert rec2["options"][2]["typed"] == "hello a\nb"
    env.bridge.dialog_action(f"{B.ACTION_DIALOG_TEXT}2", json.dumps({"t": rec2["token"], "o": 2}), "TRIG",
                             "D-OWNER", message(rec2))
    text = json.dumps(env.transport.views_opened[-1]["view"], ensure_ascii=False)
    assert "added after it" in text and "hello a\\nb" in text
    first = B.dialog_text_view("t", 0, "coder", "Type something")
    assert "added after" not in json.dumps(first)  # nothing typed yet: no notice


def test_thread_reply_on_a_typed_row_reports_the_appended_text(env):
    """N-B: no clearing key is sent; the reply is appended and the thread says so."""
    rec = block(env, ASK_MULTI_UNTYPED)
    env.herdr.on_text = lambda pane, text: env.herdr.visible.__setitem__("w1:p1", ASK_MULTI_TYPED)
    env.bridge.answer_dialog(rec["token"], ("text", 2, "hello a\nb"))
    more = ASK_MULTI_TYPED.replace("         b\n", "         b\n         more\n")
    env.herdr.on_text = lambda pane, text: env.herdr.visible.__setitem__("w1:p1", more)
    env.bridge.handle_dm_message("D-OWNER", "UOWNER", "more", "9.9", rec["thread_ts"])
    assert env.herdr.inputs()[-1] == ("send_text", "w1:p1", "more")
    assert not any(c[0] == "send_keys" for c in env.herdr.inputs())  # no Enter, no clearing key
    notice = env.transport.posts[-1]
    assert notice["thread_ts"] == rec["thread_ts"]
    assert "added after" in notice["blocks"][0]["text"]["text"]
    assert "hello a\nb\nmore" in notice["blocks"][0]["text"]["text"]
    assert record(env)["options"][2]["typed"] == "hello a\nb\nmore"


def test_first_thread_reply_on_a_multi_select_row_has_no_append_notice(env):
    rec = block(env, ASK_MULTI_UNTYPED)
    env.herdr.on_text = lambda pane, text: env.herdr.visible.__setitem__("w1:p1", ASK_MULTI_TYPED)
    env.bridge.handle_dm_message("D-OWNER", "UOWNER", "hello a\nb", "9.9", rec["thread_ts"])
    assert env.herdr.inputs() == [("send_text", "w1:p1", "hello a\nb")]
    assert not any("added after" in json.dumps(p["blocks"] or []) for p in env.transport.posts)
