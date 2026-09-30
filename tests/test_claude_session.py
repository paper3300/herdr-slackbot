"""Claude JSONL result source. Fixture: real session (5 turns) with tool payloads trimmed.

Turns in tests/fixtures/claude_session.jsonl:
 1. "what is 2+2?"            -> "2+2 equals 4."
 2. Read/Grep/Glob + summary  -> "# Summary ..." (markdown preserved)
 3. Bash blocked, user pressed esc -> "[Request interrupted by user for tool use]"
 4. text / tools / long answer (final message starts "I'll check the git configuration")
 5. Glob + one sentence       -> "The docs directory contains a single markdown file ..."
"""

import json
from pathlib import Path

import pytest

from herdr_slackbot.claude_session import (
    agent_result,
    cwd_slug,
    find_session_file,
    last_answer,
    last_answer_from_lines,
    read_tail_lines,
)

FIXTURE = Path(__file__).parent / "fixtures" / "claude_session.jsonl"
SESSION = "c6565ec4-87cd-4855-b9ee-4a940f248743"
CWD = "D:\\Git\\herdr-slackbot"


@pytest.fixture
def lines():
    return FIXTURE.read_text(encoding="utf-8").splitlines()


def prompt_indices(lines):
    out = []
    for i, line in enumerate(lines):
        e = json.loads(line)
        if e.get("type") == "user" and isinstance(e["message"]["content"], str):
            out.append(i)
    return out


def upto_turn_end(lines, turn):
    """Lines through the end of `turn` (1-based), i.e. before the next prompt."""
    idx = prompt_indices(lines)
    return lines[: idx[turn]] if turn < len(idx) else lines


def test_latest_turn_answer_and_duration(lines):
    a = last_answer_from_lines(lines)
    assert a.text.startswith("The docs directory contains a single markdown file")
    assert a.duration == pytest.approx(3.34, abs=0.01)  # from turn_duration.durationMs
    assert a.answered_at > a.prompt_at


def test_markdown_is_preserved(lines):
    a = last_answer_from_lines(upto_turn_end(lines, 2))
    assert a.text.startswith("# Summary")
    assert "**" in a.text


def test_only_final_message_of_turn(lines):
    a = last_answer_from_lines(upto_turn_end(lines, 4))
    assert a.text.startswith("I'll check the git configuration")
    assert "I'll examine the specification files" not in a.text  # earlier message of the turn


def test_interrupted_turn_has_no_answer(lines):
    assert last_answer_from_lines(upto_turn_end(lines, 3)) is None


def test_turn_still_at_tool_call_has_no_answer(lines):
    # Cut turn 5 right after its tool_use (before the final message).
    cut = next(i for i in range(len(lines) - 1, -1, -1)
               if json.loads(lines[i]).get("message", {}).get("stop_reason") == "tool_use")
    assert last_answer_from_lines(lines[: cut + 1]) is None


def test_prompt_without_answer_yet(lines):
    assert last_answer_from_lines(lines[: prompt_indices(lines)[-1] + 1]) is None


def test_since_requires_answer_newer_than_prompt(lines):
    a = last_answer_from_lines(lines)
    assert last_answer_from_lines(lines, since=a.prompt_at) is not None
    assert last_answer_from_lines(lines, since=a.answered_at) is None
    assert last_answer_from_lines(lines, since=a.answered_at + 60) is None


def test_sidechain_meta_garbage_and_commands_ignored(lines):
    extra = [
        '{"type": "assistant", "isSidechain": true, "timestamp": "2026-09-30T03:00:00Z", '
        '"message": {"id": "msg_side", "stop_reason": "end_turn", "content": [{"type": "text", "text": "subagent"}]}}',
        'not json {',  # garbage in the middle is skipped
        '{"type": "user", "isMeta": true, "timestamp": "2026-09-30T03:00:01Z", "message": {"content": "meta"}}',
        '{"type": "user", "timestamp": "2026-09-30T03:00:02Z", '
        '"message": {"content": "<command-name>/model</command-name>"}}',
        '',
    ]
    a = last_answer_from_lines(lines + extra)
    assert a.text.startswith("The docs directory contains")
    # ...but an undecodable LAST record means Claude is mid-append: don't trust the file.
    assert last_answer_from_lines(lines + ['{"type": "assistant", "mess']) is None


def test_cwd_slug():
    assert cwd_slug(r"D:\Git\herdr-slackbot") == "D--Git-herdr-slackbot"
    assert cwd_slug("D:\\Demo_Main\\Program\\") == "D--Demo-Main-Program"


def test_find_session_file(tmp_path):
    proj = tmp_path / cwd_slug(CWD)
    proj.mkdir()
    (proj / f"{SESSION}.jsonl").write_text("", encoding="utf-8")
    assert find_session_file(SESSION, CWD, tmp_path) == proj / f"{SESSION}.jsonl"
    assert find_session_file(SESSION, CWD + "\\", tmp_path) == proj / f"{SESSION}.jsonl"
    # cwd changed since launch -> glob fallback
    assert find_session_file(SESSION, r"D:\elsewhere", tmp_path) == proj / f"{SESSION}.jsonl"
    assert find_session_file("../../etc/passwd", CWD, tmp_path) is None
    assert find_session_file("", CWD, tmp_path) is None
    assert find_session_file("00000000-0000-0000-0000-000000000000", CWD, tmp_path) is None


def test_read_tail_lines_drops_partial_first_line(tmp_path):
    p = tmp_path / "x.jsonl"
    p.write_text("A" * 50 + "\n" + '{"k": 1}\n', encoding="utf-8")
    assert read_tail_lines(p, max_bytes=20) == ['{"k": 1}']
    assert read_tail_lines(p)[1] == '{"k": 1}'


@pytest.fixture
def projects(tmp_path):
    proj = tmp_path / cwd_slug(CWD)
    proj.mkdir()
    (proj / f"{SESSION}.jsonl").write_bytes(FIXTURE.read_bytes())
    return tmp_path


def test_last_answer_from_file(projects):
    assert last_answer(SESSION, CWD, base=projects).text.startswith("The docs directory")


def test_agent_result_prefers_jsonl(projects):
    screen_reads = []
    r = agent_result("claude", SESSION, CWD, lambda: screen_reads.append(1) or "", base=projects)
    assert r.source == "jsonl" and r.parsed
    assert r.text.startswith("The docs directory")
    assert r.duration == "3s"
    assert screen_reads == []  # the screen is not read when the jsonl answers


def test_agent_result_falls_back_to_screen(projects, transcript):
    screen = transcript("claude_short.txt")
    missing = agent_result("claude", "11111111-2222-3333-4444-555555555555", CWD, lambda: screen, base=projects)
    assert missing.source == "screen" and missing.text == "2+2 equals 4."
    stale = agent_result("claude", SESSION, CWD, lambda: screen, since=4102444800.0, base=projects,
                         retry_delay=0)
    assert stale.source == "screen"
    codex = agent_result("codex", SESSION, CWD, lambda: transcript("codex_answer.txt"), base=projects)
    assert codex.source == "tail" and not codex.parsed


def test_agent_result_unparseable_file_falls_back(tmp_path, transcript):
    proj = tmp_path / cwd_slug(CWD)
    proj.mkdir()
    (proj / f"{SESSION}.jsonl").write_text("garbage\n{\n", encoding="utf-8")
    r = agent_result("claude", SESSION, CWD, lambda: transcript("claude_short.txt"), base=tmp_path,
                     retry_delay=0)
    assert r.source == "screen" and r.text == "2+2 equals 4."


# --- recheck N3/N4: completeness and malformed records -------------------------------------------

def _final_message_id(lines):
    for line in reversed(lines):
        e = json.loads(line)
        if e.get("type") == "assistant":
            return e["message"]["id"]


def _write(projects, text):
    (projects / cwd_slug(CWD) / f"{SESSION}.jsonl").write_text(text, encoding="utf-8")


def _screen_counter():
    reads = []
    return reads, (lambda: reads.append(1) or "❯ q\n\n● screen answer\n\n✻ Worked for 1s\n")


def _upto_answer(lines):
    """Fixture up to and including the final assistant text record (drop turn_duration etc.)."""
    idx = max(i for i, l in enumerate(lines) if json.loads(l).get("type") == "assistant")
    return lines[: idx + 1]


def test_recheck_n3_partial_next_record_falls_back(projects, lines):
    partial = '{"type": "user", "timestamp": "2026-09-30T03:10:00Z", "message": {"content": "next pro'
    _write(projects, "\n".join(lines) + "\n" + partial)  # no trailing newline: append in progress
    reads, screen = _screen_counter()
    r = agent_result("claude", SESSION, CWD, screen, base=projects, retry_delay=0)
    assert r.source == "screen" and r.text == "screen answer" and reads == [1]


def test_recheck_n3_partial_second_block_of_final_message(projects, lines):
    head = _upto_answer(lines)
    mid = _final_message_id(head)
    second = json.dumps({"type": "assistant", "timestamp": "2026-09-30T02:45:34Z",
                         "message": {"id": mid, "stop_reason": "end_turn",
                                     "content": [{"type": "text", "text": "second block"}]}})
    _write(projects, "\n".join(head) + "\n" + second[: len(second) // 2])
    reads, screen = _screen_counter()
    r = agent_result("claude", SESSION, CWD, screen, base=projects, retry_delay=0)
    assert r.source == "screen" and reads == [1]


def test_recheck_n3_unterminated_message_is_not_trusted(projects, lines):
    head = _upto_answer(lines)
    last = json.loads(head[-1])
    last["message"]["stop_reason"] = None
    _write(projects, "\n".join(head[:-1] + [json.dumps(last)]) + "\n")
    reads, screen = _screen_counter()
    r = agent_result("claude", SESSION, CWD, screen, base=projects, retry_delay=0)
    assert r.source == "screen" and reads == [1]


def test_recheck_n3_append_finishing_during_retry_is_used(projects, lines):
    text = "\n".join(lines) + "\n"
    cut = len(text) - 40
    _write(projects, text[:cut])
    sleeps = []

    def finish_append(delay):
        sleeps.append(delay)
        _write(projects, text)

    a = last_answer(SESSION, CWD, base=projects, retry_delay=0.01, sleep=finish_append)
    assert a is not None and a.text.startswith("The docs directory")
    assert sleeps == [0.01]


def test_recheck_n3_retries_are_bounded(projects, lines):
    _write(projects, "\n".join(lines) + '\n{"type": "assist')
    sleeps = []
    assert last_answer(SESSION, CWD, base=projects, retries=2, retry_delay=0.5, sleep=sleeps.append) is None
    assert sleeps == [0.5, 0.5]


def test_recheck_n4_bad_duration_keeps_answer(projects, lines):
    bad = json.dumps({"type": "system", "subtype": "turn_duration", "durationMs": "bad",
                      "timestamp": "2026-09-30T02:45:34Z"})
    _write(projects, "\n".join(lines + [bad]) + "\n")
    reads, screen = _screen_counter()
    r = agent_result("claude", SESSION, CWD, screen, base=projects, retry_delay=0)
    assert r.source == "jsonl" and r.text.startswith("The docs directory") and reads == []
    assert r.duration  # falls back to real turn_duration / timestamps


@pytest.mark.parametrize("record", [
    {"type": "assistant", "timestamp": "2026-09-30T02:45:34Z", "message": "oops"},
    {"type": "assistant", "timestamp": "2026-09-30T02:45:34Z", "message": {"id": 5, "content": 7}},
    {"type": "user", "timestamp": "2026-09-30T02:45:35Z", "message": ["not", "a", "dict"]},
    {"type": "assistant", "timestamp": "2026-09-30T02:45:34Z",
     "message": {"id": "m", "stop_reason": "end_turn", "content": ["str-block", {"type": "text", "text": 3}]}},
])
def test_recheck_n4_malformed_records_never_raise(projects, lines, record):
    _write(projects, "\n".join(lines + [json.dumps(record)]) + "\n")
    reads, screen = _screen_counter()
    r = agent_result("claude", SESSION, CWD, screen, base=projects, retry_delay=0)
    # A malformed record after the last prompt may be a newer turn: never report the older answer.
    assert r.source == "screen" and reads == [1] and r.text == "screen answer"


def test_recheck_n4_unexpected_exception_falls_back(projects, monkeypatch):
    from herdr_slackbot import claude_session as cs

    def boom(*a, **k):
        raise RuntimeError("surprise")
    monkeypatch.setattr(cs, "evaluate_lines", boom)
    reads, screen = _screen_counter()
    r = agent_result("claude", SESSION, CWD, screen, base=projects, retry_delay=0)
    assert r.source == "screen" and reads == [1]



# --- recheck2 N4/N5 ----------------------------------------------------------------------------------

NL = chr(10)


@pytest.mark.parametrize("raw_duration", ["1e309", "-1e309", "1e300", "-5", '"NaN"'])
def test_recheck2_n4_non_finite_or_absurd_durations_are_ignored(projects, lines, raw_duration):
    bad = '{"type": "system", "subtype": "turn_duration", "durationMs": %s, "timestamp": "2026-09-30T02:45:34Z"}' % raw_duration
    _write(projects, NL.join(lines + [bad]) + NL)
    reads, screen = _screen_counter()
    r = agent_result("claude", SESSION, CWD, screen, base=projects, retry_delay=0)
    assert r.source == "jsonl" and r.text.startswith("The docs directory") and reads == []
    assert r.duration and "inf" not in r.duration


def test_recheck2_n4_formatting_errors_still_fall_back(projects, monkeypatch):
    from herdr_slackbot import claude_session as cs

    def overflow(_):
        raise OverflowError("cannot convert float infinity to integer")
    monkeypatch.setattr(cs, "format_duration", overflow)
    reads, screen = _screen_counter()
    r = agent_result("claude", SESSION, CWD, screen, base=projects, retry_delay=0)
    assert r.source == "screen" and reads == [1]


@pytest.mark.parametrize("record", [
    {"type": "user", "message": ["malformed new prompt"]},
    {"type": "user", "message": {"content": 5}},
    {"type": "assistant", "message": {"id": "m9", "stop_reason": "end_turn",
                                      "content": [{"type": "text", "text": ["not", "text"]}]}},
])
def test_recheck2_n5_malformed_newer_record_never_returns_old_answer(tmp_path, record):
    proj = tmp_path / cwd_slug(CWD)
    proj.mkdir()
    old = [
        {"type": "user", "timestamp": "2026-09-30T01:00:00Z", "message": {"content": "old prompt"}},
        {"type": "assistant", "timestamp": "2026-09-30T01:00:05Z",
         "message": {"id": "m1", "stop_reason": "end_turn", "content": [{"type": "text", "text": "OLD ANSWER"}]}},
    ]
    record = dict(record, timestamp="2026-09-30T01:05:00Z")
    (proj / f"{SESSION}.jsonl").write_text(NL.join(json.dumps(r) for r in old + [record]) + NL,
                                           encoding="utf-8")
    reads, screen = _screen_counter()
    r = agent_result("claude", SESSION, CWD, screen, base=tmp_path, retry_delay=0)
    assert r.source == "screen" and "OLD ANSWER" not in r.text and reads == [1]


def test_recheck2_n5_malformed_record_before_last_prompt_is_harmless(projects, lines):
    idx = max(i for i, l in enumerate(lines) if json.loads(l).get("type") == "user"
              and isinstance(json.loads(l)["message"]["content"], str))
    junk = json.dumps({"type": "user", "message": ["old junk"]})
    _write(projects, NL.join(lines[:idx] + [junk] + lines[idx:]) + NL)
    reads, screen = _screen_counter()
    r = agent_result("claude", SESSION, CWD, screen, base=projects, retry_delay=0)
    assert r.source == "jsonl" and r.text.startswith("The docs directory")
