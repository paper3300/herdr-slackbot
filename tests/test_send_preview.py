"""Send modal: last-response preview (builder, ack-first flow, stale results, hash conflicts)."""

import dataclasses
import itertools

import pytest

from fakes import DeferredExecutor
from herdr_slackbot import blocks as B
from herdr_slackbot.config import load_config
from herdr_slackbot.slack_app import build_app
from herdr_slackbot.slack_transport import SlackPermanentError
from test_bridge import SHORT, env, make_env, run  # noqa: F401 (env fixture)
from test_slack_app import OWNER, STRANGER, RecordingBridge, authorize, block_action, dispatch


# --- builder ---------------------------------------------------------------------------------------

def test_tail_excerpt_keeps_the_end_at_a_line_boundary():
    text = "\n".join(f"line {i:04d} " + "x" * 40 for i in range(200))
    excerpt, cut = B.tail_excerpt(text, 2500)
    assert cut and len(excerpt) <= 2500
    assert excerpt.startswith("line ") and text.endswith(excerpt)  # whole lines, the end kept
    assert B.tail_excerpt("short", 2500) == ("short", False)


def test_tail_excerpt_reopens_a_cut_code_fence():
    text = "intro\n```python\n" + "\n".join(f"print({i})" for i in range(800)) + "\n```\nDone. Next?"
    excerpt, cut = B.tail_excerpt(text, 500)
    assert cut and excerpt.startswith("```\n")  # cut landed inside the fence: reopened


def _fences_balanced(text):
    return sum(1 for line in text.split("\n") if line.lstrip().startswith("```")) % 2 == 0


def test_last_response_blocks_long_text_is_tail_truncated_and_fence_safe():
    body = "# Plan\n" + "\n".join(f"- step {i} **bold** & <tag>" for i in range(400)) + \
           "\n```\n" + "\n".join(f"code {i}" for i in range(300)) + "\n```\n**Shall I continue?**"
    head, sec = B.last_response_blocks(body, "3분 전", "1m 20s")
    assert head["block_id"] == B.BLOCK_PREVIEW_HEAD and sec["block_id"] == B.BLOCK_PREVIEW_BODY
    assert head["elements"][0]["text"] == "마지막 응답 · 3분 전 · 1m 20s"
    text = sec["text"]["text"]
    assert text.startswith(B.PREVIEW_CUT_MARK) and len(text) <= B.SECTION_MAX
    assert text.rstrip().endswith("*Shall I continue?*")  # the end (conclusion) is kept
    assert _fences_balanced(text)
    assert "&amp;" in B.last_response_blocks("a & b")[1]["text"]["text"]  # mrkdwn-escaped


def test_last_response_blocks_escaping_heavy_text_still_fits():
    blocks = B.last_response_blocks("<&>" * 3000)
    assert len(blocks[1]["text"]["text"]) <= B.SECTION_MAX


def test_last_response_blocks_raw_tail_is_a_code_block():
    text = B.last_response_blocks("$ make\nok <done>", markdown=False)[1]["text"]["text"]
    assert text.startswith("```\n") and text.endswith("\n```") and "&lt;done&gt;" in text


@pytest.mark.parametrize("text,note,expected", [(None, None, B.PREVIEW_EMPTY), ("  \n", None, B.PREVIEW_EMPTY),
                                                (None, B.PREVIEW_FAILED, B.PREVIEW_FAILED)])
def test_last_response_blocks_single_italic_line(text, note, expected):
    (block,) = B.last_response_blocks(text, note=note)
    assert block["type"] == "context" and block["elements"][0]["text"] == f"_{expected}_"


def test_send_view_places_preview_between_agent_and_prompt():
    agents = [{"pane_id": "w1:p1", "name": "coder", "agent_status": "idle", "workspace_id": "w1"}]
    preview = B.last_response_blocks("hello")
    view = B.send_view(agents, {}, "coder", preview=preview, prompt="typed")
    kinds = [b.get("block_id") for b in view["blocks"]]
    assert kinds == [B.BLOCK_TARGET, B.BLOCK_PREVIEW_HEAD, B.BLOCK_PREVIEW_BODY, B.BLOCK_PROMPT]
    target = view["blocks"][0]
    assert target["dispatch_action"] is True and target["element"]["action_id"] == B.ACTION_SEND_TARGET
    assert view["blocks"][-1]["element"]["initial_value"] == "typed"
    hint = B.send_view(agents, {})["blocks"][1]
    assert B.PREVIEW_PICK in hint["elements"][0]["text"]
    new_state = {"target": {B.ACTION_SEND_TARGET: {"selected_option": {"value": "coder"}}},
                 "prompt": {"value": {"value": "hi"}}}
    assert B.parse_send_view_state(new_state) == ("coder", "hi")


# --- bridge flow ------------------------------------------------------------------------------------

def _hashing(transport):
    """update_view returns a new hash each time (like Slack) and records the hash it was given."""
    counter = itertools.count(1)
    orig = transport.update_view

    def update_view(view_id, view, view_hash=None):
        orig(view_id, view, view_hash)
        return f"h{next(counter)}"
    transport.update_view = update_view


def _preview_text(view):
    return " ".join(((b.get("text") or {}).get("text") or "") +
                    " ".join(e.get("text", "") for e in b.get("elements") or [] if isinstance(e, dict))
                    for b in view["blocks"] if str(b.get("block_id", "")).startswith("preview"))


def _payload(view_id, target, prompt="", view_hash="hp"):
    return {"id": view_id, "hash": view_hash, "private_metadata": "{}", "state": {"values": {
        "target": {B.ACTION_SEND_TARGET: {"selected_option": {"value": target} if target else None}},
        "prompt": {"value": {"value": prompt}}}}}


def test_open_with_preselected_agent_loads_the_preview_after_the_modal(env):
    env.herdr.add_agent("w1:p1", "idle", name="coder")
    env.herdr.screens["w1:p1"] = SHORT
    _hashing(env.transport)
    env.bridge.open_send_modal("TRIG", None, "coder")
    assert env.transport.events[0] == "open_view"  # trigger used first
    updates = env.transport.views_updated
    assert B.PREVIEW_LOADING in _preview_text(updates[0]["view"])  # modal usable right away
    final = updates[-1]
    assert "2+2" in _preview_text(final["view"]) or "4" in _preview_text(final["view"])
    assert final["hash"] == "h1"  # views.update with the hash of the previous update
    assert final["view"]["blocks"][0]["element"]["initial_option"]["value"] == "coder"


def test_send_without_args_opens_modal_with_pick_hint(env):
    env.herdr.add_agent("w1:p1", "idle", name="coder")
    run(env, "send")
    view = env.transport.views_updated[-1]["view"]
    assert B.PREVIEW_PICK in _preview_text(view) and len(env.transport.views_updated) == 1  # nothing to fetch


def test_changing_the_agent_ignores_the_stale_preview(tmp_path):
    env = make_env(tmp_path, executor=DeferredExecutor())
    env.herdr.add_agent("w1:p1", "idle", name="alpha")
    env.herdr.add_agent("w1:p2", "idle", name="beta", kind="codex")
    env.herdr.screens["w1:p1"] = SHORT
    env.herdr.screens["w1:p2"] = "• beta finished the job\n"
    _hashing(env.transport)
    env.bridge.open_send_modal("TRIG", None, "alpha")  # alpha's fetch is queued...
    env.bridge.update_send_modal(_payload("V1", "beta", "half typed"))  # ...user switches to beta
    before = len(env.transport.views_updated)
    env.bridge.executor.run_all()  # alpha's (stale) job, then beta's
    later = env.transport.views_updated[before:]
    assert len(later) == 1  # only beta's result was applied
    text = _preview_text(later[0]["view"])
    assert "beta finished" in text and "2+2" not in text
    assert later[0]["view"]["blocks"][-1]["element"]["initial_value"] == "half typed"  # typed prompt kept
    env.bridge.stop()


def test_dispatch_update_keeps_typed_prompt_and_block_ids(env):
    env.herdr.add_agent("w1:p1", "idle", name="coder")
    env.herdr.screens["w1:p1"] = SHORT
    env.bridge.open_send_modal("TRIG", None, None)
    env.bridge.update_send_modal(_payload("V1", "coder", "please also"))
    for upd in env.transport.views_updated[1:]:
        blocks = upd["view"]["blocks"]
        assert blocks[0]["block_id"] == B.BLOCK_TARGET and blocks[-1]["block_id"] == B.BLOCK_PROMPT
        assert blocks[-1]["element"]["initial_value"] == "please also"
    assert env.transport.views_updated[1]["hash"] == "hp"  # the payload's hash is used for the placeholder


def test_hash_conflict_on_preview_is_dropped_without_error(env, caplog):
    import logging
    caplog.set_level(logging.INFO)
    env.herdr.add_agent("w1:p1", "idle", name="coder")
    env.herdr.screens["w1:p1"] = SHORT
    calls = []
    orig = env.transport.update_view

    def update_view(view_id, view, view_hash=None):
        calls.append(view_hash)
        if len(calls) == 2:  # the preview update: the user changed the view meanwhile
            raise SlackPermanentError("hash_conflict")
        return orig(view_id, view, view_hash)
    env.transport.update_view = update_view
    env.bridge.open_send_modal("TRIG", None, "coder")  # must not raise
    assert len(calls) == 2 and "update for generation 1 dropped" in caplog.text


def test_dispatch_placeholder_retries_with_our_latest_hash_on_conflict(env):
    env.herdr.add_agent("w1:p1", "idle", name="coder")
    env.bridge.open_send_modal("TRIG", None, None)
    env.bridge._send_views["V1"]["hash"] = "h-ours"
    seen = []

    def update_view(view_id, view, view_hash=None):
        seen.append(view_hash)
        if view_hash == "hp":
            raise SlackPermanentError("hash_conflict")
        return "h-next"
    env.transport.update_view = update_view
    env.bridge.update_send_modal(_payload("V1", "coder"))
    assert seen[:2] == ["hp", "h-ours"]


def test_busy_agent_and_failed_read_give_a_note_and_never_block(env):
    env.herdr.add_agent("w1:p1", "working", name="busy")
    env.bridge.open_send_modal("TRIG", None, "busy")
    assert B.PREVIEW_BUSY in _preview_text(env.transport.views_updated[-1]["view"])
    assert env.bridge.preview_blocks(None)[0]["elements"][0]["text"] == f"_{B.PREVIEW_FAILED}_"

    def broken(*a, **k):
        raise RuntimeError("disk")
    import herdr_slackbot.bridge as bridge_mod
    orig = bridge_mod.agent_result
    bridge_mod.agent_result = broken
    try:
        blocks = env.bridge.preview_blocks({"pane_id": "w1:p1", "agent_status": "idle", "agent": "claude"})
    finally:
        bridge_mod.agent_result = orig
    assert B.PREVIEW_FAILED in blocks[0]["elements"][0]["text"]


def test_submit_forgets_the_modal(env):
    env.herdr.add_agent("w1:p1", "idle", name="coder")
    env.bridge.open_send_modal("TRIG", None, None)
    assert "V1" in env.bridge._send_views
    view = _payload("V1", "coder", "go")
    env.bridge.submit_send_view(view)
    assert "V1" not in env.bridge._send_views


# --- bolt: owner guard + ack first ----------------------------------------------------------------------

class PreviewBridge(RecordingBridge):
    def update_send_modal(self, view, action_ts=None):
        self.calls.append(("update_send_modal", view["id"], action_ts))


def test_agent_select_dispatch_is_acked_and_owner_guarded(tmp_path):
    cfg = dataclasses.replace(load_config(env={"USERNAME": "me"}, config_dir=tmp_path), slack_owner_user_id=OWNER)
    bridge = PreviewBridge()
    app = build_app(cfg, bridge, authorize=authorize, process_before_response=True)
    view = {"id": "V9", "type": "modal", "callback_id": B.SEND_CALLBACK, "hash": "h", "state": {"values": {}},
            "private_metadata": "{}"}
    assert dispatch(app, block_action(OWNER, B.ACTION_SEND_TARGET, view=view)).status == 200
    dispatch(app, block_action(STRANGER, B.ACTION_SEND_TARGET, view=view))
    assert bridge.calls == [("update_send_modal", "V9", "2.0")]  # the selection's action_ts is carried


# --- S1: submission never waits for preview work ---------------------------------------------------

def test_s1_submission_is_acked_while_a_preview_update_hangs(tmp_path):
    import threading
    import time

    env = make_env(tmp_path, executor=DeferredExecutor(), start=False)
    env.bridge.dm_channel = "D-OWNER"
    env.herdr.add_agent("w1:p1", "idle", name="coder")
    env.herdr.screens["w1:p1"] = SHORT
    cfg = dataclasses.replace(env.cfg, slack_owner_user_id=OWNER)
    app = build_app(cfg, env.bridge, authorize=authorize)  # default processing, like production
    env.bridge.open_send_modal("TRIG", None, "coder")
    (preview_job,) = env.bridge.executor.jobs
    env.bridge.executor.jobs = []
    entered, release = threading.Event(), threading.Event()
    orig = env.transport.update_view

    def update_view(view_id, view, view_hash=None):
        if threading.current_thread().name == "preview":
            entered.set()
            release.wait(10)  # Slack is slow for this update (>3s)
        return orig(view_id, view, view_hash)
    env.transport.update_view = update_view
    worker = threading.Thread(target=preview_job[0], args=preview_job[1], name="preview")
    worker.start()
    assert entered.wait(5)
    body = {"type": "view_submission", "team": {"id": "T1"}, "user": {"id": OWNER}, "api_app_id": "A1",
            "view": {"id": "V1", "type": "modal", "callback_id": B.SEND_CALLBACK, "hash": "h",
                     "private_metadata": "{}", "state": {"values": {
                         "target": {B.ACTION_SEND_TARGET: {"selected_option": {"value": "coder"}}},
                         "prompt": {"value": {"value": "go on"}}}}}}
    try:
        started = time.monotonic()
        resp = dispatch(app, body)
        elapsed = time.monotonic() - started
        assert resp.status == 200 and elapsed < 1.0, (resp.status, elapsed)
        assert "V1" not in env.bridge._send_views
    finally:
        release.set()
        worker.join(5)
    env.bridge.executor.run_all()  # the accepted send
    assert [p[2] for p in env.herdr.prompts()] == ["go on"]
    env.bridge.stop()


# --- S2: selection order, not processing order --------------------------------------------------------

class HashSlack:
    """views.update that enforces hashes like Slack."""

    def __init__(self, transport):
        self.current = None
        self.n = itertools.count(1)
        self.applied = []
        self.orig = transport.update_view
        transport.update_view = self.update_view

    def update_view(self, view_id, view, view_hash=None):
        if view_hash is not None and self.current is not None and view_hash != self.current:
            raise SlackPermanentError("hash_conflict")
        self.orig(view_id, view, view_hash)
        self.current = f"h{next(self.n)}"
        self.applied.append((view_hash, _preview_text(view)))
        return self.current


def test_s2_older_selection_processed_later_is_ignored(tmp_path):
    env = make_env(tmp_path, executor=DeferredExecutor())
    env.herdr.add_agent("w1:p1", "idle", name="alpha")
    env.herdr.add_agent("w1:p2", "idle", name="beta", kind="codex")
    env.herdr.screens["w1:p1"] = SHORT
    env.herdr.screens["w1:p2"] = "• beta finished the job\n"
    slack = HashSlack(env.transport)
    env.bridge.open_send_modal("TRIG", None, None)
    h1 = slack.current
    env.bridge.update_send_modal(_payload("V1", "beta", view_hash=h1), "100.1")  # newer, processed first
    env.bridge.executor.run_all()
    env.bridge.update_send_modal(_payload("V1", "alpha", view_hash=h1), "100.0")  # older, delayed
    env.bridge.update_send_modal(_payload("V1", "beta", view_hash=h1), "100.1")  # duplicate delivery
    env.bridge.executor.run_all()
    assert env.bridge._send_views["V1"]["target"] == "beta"
    assert "beta finished" in slack.applied[-1][1]
    assert not any("2+2" in text for _, text in slack.applied)  # alpha's answer never shown
    env.bridge.stop()


def test_s2_conflict_retry_only_for_the_latest_selection(env):
    env.herdr.add_agent("w1:p1", "idle", name="coder")
    env.bridge.open_send_modal("TRIG", None, None)
    rec = env.bridge._send_views["V1"]
    rec["gen"], rec["hash"] = 5, "h-new"
    calls = []

    def conflict(view_id, view, view_hash=None):
        calls.append(view_hash)
        raise SlackPermanentError("hash_conflict")
    env.transport.update_view = conflict
    assert env.bridge._apply_send_update("V1", 4, {"type": "modal"}, "h-old") is False
    assert calls == ["h-old"]  # generation 4 is no longer the latest: no retry with the newer hash
    calls.clear()
    assert env.bridge._apply_send_update("V1", 5, {"type": "modal"}, "h-old") is False
    assert calls == ["h-old", "h-new"]  # latest selection: one retry with our newest hash


# --- minors: raw fences, verbatim ----------------------------------------------------------------------

def test_raw_tail_with_an_embedded_fence_renders_one_code_block():
    text = B.last_response_blocks("$ cat notes.md\n```python\nprint(1)\n", markdown=False)[1]["text"]["text"]
    assert text.count("```") == 2 and text.startswith("```\n") and text.endswith("\n```")
    blocks, _ = B.result_blocks("h", [], "log\n```\nunfinished", None, markdown=False)
    assert blocks[-1]["text"]["text"].count("```") == 2


def test_agent_output_mrkdwn_is_verbatim():
    def mrkdwn_objects(blocks):
        for b in blocks:
            for obj in [b.get("text")] + list(b.get("elements") or []):
                if isinstance(obj, dict) and obj.get("type") == "mrkdwn":
                    yield obj
    agents = [{"pane_id": "w1:p1", "name": "x", "agent_status": "idle", "workspace_id": "w1",
               "terminal_title_stripped": "@here look"}]
    samples = [B.last_response_blocks("@channel hi"), B.result_blocks("h", ["c"], "@everyone", None, "recap")[0],
               B.home_view("H", ["x"], agents, {})["blocks"], B.agent_list_blocks(agents, {}),
               B.sent_blocks("@here prompt")]
    objs = [o for blocks in samples for o in mrkdwn_objects(blocks)]
    assert objs and all(o.get("verbatim") is True for o in objs)
