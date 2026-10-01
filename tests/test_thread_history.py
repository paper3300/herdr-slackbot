"""Completion messages show the conversation since the previous notification (THREAD-HISTORY)."""

import json
from datetime import datetime, timezone

import pytest

from herdr_slackbot import blocks as B
from herdr_slackbot.claude_session import Turn, cwd_slug
from herdr_slackbot.history import newer_cursor, range_markdown, select_range, skip_slack_prompt
from test_bridge import env, results_posted, transition  # noqa: F401 (env fixture)

SID = "abcdef12-0000-4000-8000-00000000aaaa"
CWD = "D:\\main"


class Transcript:
    """A synthetic session JSONL with a linear parentUuid chain; timestamps relative to the clock."""

    def __init__(self, env):
        self.path = env.tmp / "projects" / cwd_slug(CWD) / f"{SID}.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.clock, self.prev, self.n = env.clock, None, 0
        self.last = self.clock() - 1000

    def _add(self, record, uid):
        self.n += 1
        self.last = self.clock() - 1000 + self.n
        record.update(uuid=uid, parentUuid=self.prev, isSidechain=False, timestamp=datetime.fromtimestamp(
            self.clock() - 1000 + self.n, timezone.utc).isoformat().replace("+00:00", "Z"))
        self.prev = uid
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
        return record

    def prompt(self, uid, text, **extra):
        return self._add({"type": "user", "message": {"role": "user", "content": text}, **extra}, uid)

    def answer(self, uid, text):
        return self._add({"type": "assistant", "message": {"id": f"m-{uid}", "role": "assistant",
                                                           "stop_reason": "end_turn",
                                                           "content": [{"type": "text", "text": text}]}}, uid)

    def tool(self, uid):
        self._add({"type": "assistant", "message": {"id": f"m-{uid}", "role": "assistant", "stop_reason": "tool_use",
                                                    "content": [{"type": "tool_use", "id": f"t-{uid}",
                                                                 "name": "Bash", "input": {}}]}}, uid)
        return self._add({"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": f"t-{uid}", "content": "ok"}]}}, f"r-{uid}")

    def queued(self, uid, text):
        return self._add({"type": "attachment", "attachment": {"type": "queued_command", "commandMode": "prompt",
                                                               "prompt": text}}, uid)

    def event(self, uid, summary):
        return self.prompt(uid, f"<task-notification><summary>{summary}</summary></task-notification>",
                           origin={"kind": "task-notification"})


def texts(blocks):
    out = []
    for b in blocks:
        if b.get("text"):
            out.append(b["text"]["text"])
        out.extend(e.get("text", "") for e in b.get("elements") or [] if isinstance(e, dict) and e.get("type") == "mrkdwn")
    return out


def finish(env, pane="w1:p1"):
    env.herdr.set_status(pane, "done")
    env.bridge.handle_transition(transition(env, pane, "working", "done"))
    return results_posted(env)[-1]


@pytest.fixture
def agent(env):
    return env.herdr.add_agent("w1:p1", "done", name="coder", session=SID, cwd=CWD)


# --- range selection (pure) ---------------------------------------------------------------------

def _turns():
    return [Turn("user", "A", 1.0, "u1"), Turn("assistant", "Answer A", 2.0, "a1"),
            Turn("user", "B", 3.0, "u2"), Turn("assistant", "Answer B", 4.0, "a2"),
            Turn("user", "C", 5.0, "u3"), Turn("user", "D", 6.0, "q1"), Turn("assistant", "Answer CD", 7.0, "a3")]


def test_select_range_from_cursor_latest_turn_and_fallbacks():
    turns = _turns()
    rng = select_range(turns, "Answer CD", {"id": "a1", "at": 2.0})
    assert [t.text for t in rng.turns] == ["B", "Answer B", "C", "D", "Answer CD"] and rng.from_cursor
    assert rng.cursor == {"id": "a3", "at": 7.0}
    latest = select_range(turns, "Answer CD", None)
    assert [t.text for t in latest.turns] == ["C", "D", "Answer CD"] and not latest.from_cursor
    by_time = select_range(turns, "Answer CD", {"id": "gone", "at": 4.5})  # uuid not in the tail
    assert [t.text for t in by_time.turns] == ["C", "D", "Answer CD"] and by_time.from_cursor
    by_time = select_range(turns, "Answer CD", {"id": "gone", "at": 2.0})
    assert [t.text for t in by_time.turns][0] == "B"
    for cursor in ({"id": "gone"}, {"id": "gone", "at": 0.5}, {"at": "x"}, "junk"):  # no anchor -> latest turn
        assert [t.text for t in select_range(turns, "Answer CD", cursor).turns] == ["C", "D", "Answer CD"]
    assert [t.text for t in select_range(turns, "Answer CD", {"id": "a3", "at": 7.0}).turns] == ["C", "D", "Answer CD"]
    assert select_range(turns, "not in the transcript", None) is None


def test_skip_slack_prompt_and_cursor_order():
    items = [Turn("user", "pc  question", 1.0), Turn("assistant", "x", 2.0), Turn("user", "slack\n task", 3.0)]
    assert [t.text for t in skip_slack_prompt(items, "slack task", 300)] == ["pc  question", "x"]
    assert len(skip_slack_prompt(items, "other", 300)) == 3 and len(skip_slack_prompt(items, None, 300)) == 3
    long = Turn("user", "y " * 400, 1.0)
    assert skip_slack_prompt([long], ("y " * 400).strip()[:299] + "…", 300) == []  # the stored excerpt
    assert newer_cursor(None, {"at": 1.0}) and newer_cursor({"at": 1.0}, {"at": 2.0})
    assert not newer_cursor({"at": 3.0}, {"at": 2.0})


# --- bridge -----------------------------------------------------------------------------------

def test_next_message_shows_the_conversation_since_the_previous_notification(env, agent):
    tr = Transcript(env)
    tr.prompt("u-a", "PC prompt A")
    tr.answer("a-a", "Answer to A")
    first = finish(env)
    assert any("PC prompt A" in t for t in texts(first["blocks"]))  # first message: the latest turn
    assert env.state.get_thread(SID)["history_cursor"]["id"] == "a-a"
    tr.prompt("u-b", "PC prompt B")
    tr.answer("a-b", "Answer to B")
    tr.prompt("u-c", "PC prompt C")
    tr.tool("t1")
    tr.queued("q-d", "queued prompt D")
    tr.event("e1", "build finished")
    tr.answer("a-cd", "Final answer to C and D")
    env.herdr.set_status("w1:p1", "working")
    second = finish(env)
    body = texts(second["blocks"])
    joined = "\n".join(body)
    for want in ("PC prompt B", "Answer to B", "PC prompt C", "queued prompt D", "build finished"):
        assert want in joined
    assert "PC prompt A" not in joined and "Answer to A" not in joined
    assert body[-1] == "Final answer to C and D"  # the final answer is the main body, last
    assert second["blocks"][0]["text"]["text"].startswith("✅ *coder*")  # header unchanged
    assert joined.index("PC prompt B") < joined.index("Answer to B") < joined.index("queued prompt D")
    assert env.state.get_thread(SID)["history_cursor"]["id"] == "a-cd"
    assert all(b["type"] != "actions" for b in second["blocks"])  # nothing cut: no View full


def test_first_message_shows_only_the_latest_turn(env, agent):
    tr = Transcript(env)
    for i in range(3):
        tr.prompt(f"u{i}", f"old prompt {i}")
        tr.answer(f"a{i}", f"old answer {i}")
    tr.prompt("u9", "latest prompt")
    tr.answer("a9", "latest answer")
    joined = "\n".join(texts(finish(env)["blocks"]))
    assert "latest prompt" in joined and "latest answer" in joined and "old" not in joined


def test_slack_prompt_is_not_repeated_but_pc_prompts_are_shown(env, agent):
    tr = Transcript(env)
    tr.prompt("u-a", "earlier")
    tr.answer("a-a", "earlier answer")
    env.state.upsert_thread(SID, channel="D-OWNER", thread_ts="1.0", pane_id="w1:p1",
                            history_cursor={"id": "a-a", "at": 0})
    tr.prompt("u-pc", "typed on the PC meanwhile")
    tr.answer("a-pc", "PC answer")
    env.state.set_pending_task(SID, {"task_id": "T1", "started_at": tr.last + 0.5, "seq0": 0,
                                     "working_announced": True, "prompt": "please run the slack task"})
    tr.prompt("u-sl", "please  run the\nslack task")
    tr.answer("a-sl", "Slack task done")
    env.herdr.set_status("w1:p1", "working")
    msg = finish(env)
    joined = "\n".join(texts(msg["blocks"]))
    assert "typed on the PC meanwhile" in joined and "PC answer" in joined
    assert "slack task" not in joined.replace("Slack task done", "")  # the Slack prompt is not repeated
    entry = env.state.get_thread(SID)
    assert entry["pending_task"] is None and entry["history_cursor"]["id"] == "a-sl"


def test_slack_prompt_alone_gives_todays_result_message(env, agent):
    tr = Transcript(env)
    env.state.upsert_thread(SID, channel="D-OWNER", thread_ts="1.0", pane_id="w1:p1")
    env.state.set_pending_task(SID, {"task_id": "T1", "started_at": tr.last + 0.5, "seq0": 0,
                                     "working_announced": True, "prompt": "only this"})
    tr.prompt("u1", "only this")
    tr.answer("a1", "the answer")
    env.herdr.set_status("w1:p1", "working")
    msg = finish(env)
    assert [b["type"] for b in msg["blocks"]] == ["section", "context", "section"]  # header, ctx, body
    assert env.state.get_thread(SID)["history_cursor"]["id"] == "a1"


def test_muted_completion_does_not_advance_the_cursor(env, agent):
    tr = Transcript(env)
    tr.prompt("u1", "one")
    tr.answer("a1", "first")
    finish(env)
    env.state.set_muted(SID, True)
    tr.prompt("u2", "two")
    tr.answer("a2", "second")
    env.herdr.set_status("w1:p1", "working")
    env.herdr.set_status("w1:p1", "done")
    env.bridge.handle_transition(transition(env, "w1:p1", "working", "done"))
    assert len(results_posted(env)) == 1 and env.state.get_thread(SID)["history_cursor"]["id"] == "a1"
    env.state.set_muted(SID, False)
    tr.prompt("u3", "three")
    tr.answer("a3", "third")
    env.herdr.set_status("w1:p1", "working")
    joined = "\n".join(texts(finish(env)["blocks"]))
    assert "two" in joined and "second" in joined and "three" in joined  # the muted turn is covered later


def test_duplicate_op_posts_once_and_does_not_double_advance(env, agent):
    tr = Transcript(env)
    tr.prompt("u1", "one")
    tr.answer("a1", "first")
    finish(env)
    t = transition(env, "w1:p1", "working", "done")
    env.bridge.handle_transition(t)  # same seq again: already posted
    assert len(results_posted(env)) == 1
    tr.prompt("u2", "two")
    tr.answer("a2", "second")
    env.herdr.set_status("w1:p1", "working")
    env.herdr.set_status("w1:p1", "done")
    entry, info = env.state.get_thread(SID), env.herdr.find_agent("w1:p1")
    first = env.bridge.post_result(SID, entry, info, "main", None, op="result:dup")  # posted, commit "lost"
    again = env.bridge.post_result(SID, entry, info, "main", None, op="result:dup")  # retried attempt
    assert first == again == {"history_cursor": {"id": "a2", "at": first["history_cursor"]["at"]}}
    assert len(results_posted(env)) == 2  # the retry reused the accepted post
    env.state.upsert_thread(SID, history_cursor={"id": "a9", "at": 10 ** 10})
    assert env.bridge.post_result(SID, env.state.get_thread(SID), info, "main", None, op="result:dup") == {}


def test_budget_with_many_turns_and_a_huge_final_answer(env, agent):
    tr = Transcript(env)
    tr.prompt("u0", "start")
    tr.answer("a0", "started")
    env.state.upsert_thread(SID, channel="D-OWNER", thread_ts="1.0", pane_id="w1:p1",
                            history_cursor={"id": "a0", "at": 0})
    for i in range(60):
        tr.prompt(f"u{i + 1}", f"prompt {i} " + "p" * 400)
        tr.answer(f"a{i + 1}", f"answer {i} " + "<&>" * 300)
    final = "FINAL HEAD\n" + "\n".join(f"line {i} " + "z" * 80 for i in range(400)) + "\nFINAL END"
    tr.prompt("u-last", "last prompt")
    tr.answer("a-last", final)
    env.herdr.set_status("w1:p1", "working")
    msg = finish(env)
    blocks = msg["blocks"]
    assert len(blocks) <= B.MAX_BLOCKS
    sections = [b["text"]["text"] for b in blocks if b["type"] == "section"]
    assert all(len(s) <= B.SECTION_MAX for s in sections)
    assert sum(len(t) for t in texts(blocks)) <= B.RESULT_HISTORY_MAX_CHARS + 1000
    assert sections[-1].startswith("FINAL HEAD")  # the final answer is always there (today's truncation)
    assert any("earlier messages not shown — View full" in t for t in texts(blocks))
    button = blocks[-1]["elements"][0]
    assert button["action_id"] == B.ACTION_SHOW_FULL
    env.bridge.show_full("D-OWNER", {"ts": msg["ts"], "thread_ts": msg["thread_ts"]}, button["value"])
    content = env.transport.uploads[-1]["content"]
    assert all(f"prompt {i} " in content and f"answer {i} " in content for i in range(60))
    assert "last prompt" in content and "FINAL END" in content and "<&>" * 300 in content  # untruncated
    assert "**👤 You**" in content and "**🤖 coder**" in content


def test_result_history_blocks_limits_unit():
    history = [Turn("user" if i % 2 == 0 else "assistant", "x" * 5000, float(i)) for i in range(200)]
    history.append(Turn("event", "Background task finished: " + "s" * 1000, 300.0))
    blocks, needs_full = B.result_history_blocks("✅ *c*", ["ctx"], history, "final", "RID", "recap",
                                                 agent_label="c", answered_at=301.0, now=302.0)
    assert len(blocks) <= B.MAX_BLOCKS and needs_full and blocks[-1]["block_id"] == "result_ctl"
    assert blocks[-2]["text"]["text"] == "final" and blocks[-3]["elements"][0]["text"].startswith("※")
    assert blocks[-4]["elements"][0]["text"].startswith("🤖 c")
    small, needs = B.result_history_blocks("h", [], [Turn("user", "q", 1.0)], "a", None, agent_label="c", now=2.0)
    assert not needs and [b["type"] for b in small] == ["section", "context", "section", "context", "section"]


def test_resume_uses_the_same_history(env, agent):
    tr = Transcript(env)
    tr.prompt("u0", "before")
    tr.answer("a0", "before answer")
    env.state.upsert_thread(SID, channel="D-OWNER", thread_ts="1.0", pane_id="w1:p1",
                            history_cursor={"id": "a0", "at": 0})
    tr.prompt("u1", "typed while the bridge was down")
    tr.answer("a1", "answer 1")
    env.state.set_pending_task(SID, {"task_id": "T9", "started_at": tr.last + 0.5, "seq0": 0,
                                     "working_announced": True, "prompt": "slack prompt"})
    tr.prompt("u2", "slack prompt")
    tr.answer("a2", "slack answer")
    env.herdr.set_status("w1:p1", "done")
    env.bridge.handle_resume(SID, "T9")
    joined = "\n".join(texts(results_posted(env)[-1]["blocks"]))
    assert "typed while the bridge was down" in joined and "slack answer" in joined and "before" not in joined
    assert env.state.get_thread(SID)["history_cursor"]["id"] == "a2"


@pytest.mark.parametrize("kind,session", [("codex", None), ("claude", "abcdef12-no-transcript")])
def test_codex_and_missing_transcript_output_is_unchanged(env, kind, session):
    env.herdr.add_agent("w1:p1", "done", name="cx", kind=kind, session=session, cwd=CWD)
    env.herdr.screens["w1:p1"] = "• codex finished the job\n"
    msg = finish(env)
    entry = env.state.get_thread(msg.get("key") or next(iter(env.state.all_threads())))
    assert "history_cursor" not in entry
    info = env.herdr.find_agent("w1:p1")
    env.bridge._history_range = lambda *a, **k: None  # the pre-history path
    env.bridge.post_result("K2", entry, info, env.bridge._ws_labels().get("w1", "w1"), None)
    assert env.transport.posts[-1]["blocks"] == msg["blocks"]


def test_range_markdown_contains_everything():
    md = range_markdown([Turn("user", "q", None), Turn("event", "Background task finished", None),
                         Turn("assistant", "a\n", None)], "coder")
    assert md == "**👤 You**\n\nq\n\n---\n\n⚙️ _Background task finished_\n\n---\n\n**🤖 coder**\n\na\n"
