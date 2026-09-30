"""Parser tests on real `herdr agent read --source recent-unwrapped` captures.

Fixtures (tests/fixtures/transcripts), captured from live agents on Herdr 0.8.2 (claude_long_table,
claude_multiturn_sticky_recap and claude_recap_multiline are synthetic transcripts of the same shape):
- claude_short: 1s turn, single-line answer.
- claude_multitool: Read/Grep/Glob collapsed into "Searched for ...", markdown answer.
- claude_long_multiblock: text/tool/text blocks, >4000-char answer with table + code; contains a
  sticky-header copy of the prompt inside the answer and a ghost suggestion in the input box.
- claude_long_viewport_only: same pane, but Herdr returned only the 50-line viewport.
- claude_long_table: long Korean answer with a box table; many progress `●` blocks before it.
- claude_multiturn_sticky_recap: several turns, queued message, sticky header inside a diff,
  multi-line `※ recap`, right-aligned "Update installed" notice.
- claude_recap_multiline: recap wrapping with "(disable recaps in /config)" trailer.
- claude_capture_gap: blank rows left by the scroll capture inside the final block.
- claude_interrupted: last turn interrupted at a permission prompt (no answer).
- claude_blocked_visible: `--source visible` while a permission dialog is open.
- codex_answer / codex_idle: Codex TUI (fallback path).
"""

import pytest

from herdr_slackbot.parser import (
    drop_sticky_headers,
    extract_result,
    fallback_tail,
    find_prompt_box,
    parse_claude,
    split_lines,
    strip_prompt_box,
    truncate_text,
)


def test_short_answer(transcript):
    p = parse_claude(transcript("claude_short.txt"))
    assert p is not None
    assert p.body == "2+2 equals 4."
    assert p.duration == "1s"
    assert p.recap is None
    assert p.turn_complete


def test_multitool_answer_is_last_block(transcript):
    p = parse_claude(transcript("claude_multitool.txt"))
    assert p.body.startswith("Summary\n\n- Herdr Slack plugin bridges")
    assert p.body.endswith("herdr tab create --workspace <ws> --label <label> --cwd <cwd> --no-focus")
    assert "Searched for 2 patterns" not in p.body
    assert "2+2 equals 4" not in p.body  # previous turn
    assert p.duration == "9s"
    # continuation lines are de-indented by the block's 2-space margin only
    assert "\n  bridge status." in p.body


def test_long_multiblock_takes_last_block_and_drops_sticky_header(transcript):
    p = parse_claude(transcript("claude_long_multiblock.txt"))
    assert p.body.startswith("I'll check the git configuration")
    assert "I'll examine the specification files" not in p.body
    assert "First write one sentence" not in p.body  # sticky header copy removed
    assert "│ Threading      │ Per agent session" in p.body
    assert p.body.rstrip().endswith('herdr agent prompt slack-42 "Analyze the repository structure"')
    assert "create the project structure" not in p.body  # ghost suggestion in input box
    assert p.duration == "23s"
    assert len(p.body) > 4000


def test_viewport_only_read_has_no_block_and_falls_back(transcript):
    text = transcript("claude_long_viewport_only.txt")
    assert parse_claude(text) is None
    r = extract_result(text, "claude", fallback_lines=40)
    assert not r.parsed
    assert r.text.splitlines()[-1].startswith("✻ Churned for 23s")
    assert r.text.splitlines()[0].startswith("  ├──")  # everything above the box, blanks dropped
    assert "" not in r.text.splitlines()
    assert "Haiku 4.5" not in r.text  # status lines below the prompt box are gone


def test_long_table(transcript):
    p = parse_claude(transcript("claude_long_table.txt"))
    assert p.body.startswith("확인을 마쳤습니다. 파일은 고치지 않았습니다.")
    assert "┌──" in p.body and "└──" in p.body
    assert p.body.endswith("원하시면 따로 정리하겠습니다.")
    assert "Skill(docs-review)" not in p.body
    assert p.duration == "7m 59s"


def test_multiturn_sticky_header_recap_and_notice(transcript):
    text = transcript("claude_multiturn_sticky_recap.txt")
    p = parse_claude(text)
    assert p.body.startswith("수정한 버전을 config.py에 반영했고")
    assert p.body.endswith("이 규칙으로 읽힙니다.")
    assert p.duration == "50s"
    assert p.recap.startswith("herdr-slackbot의 .env 읽기(load_env_file)")
    assert p.recap.endswith("테스트를 돌려 확인하는 것입니다.")
    assert "Update installed" not in p.body and "Update installed" not in p.recap


def test_sticky_header_is_dropped_but_real_echoes_kept(transcript):
    lines = split_lines(transcript("claude_multiturn_sticky_recap.txt"))
    prompt = "❯ 주석만 있는 .env도 정상으로 읽히는게 의도야"
    before = [l for l in lines if l.startswith(prompt)]
    after = [l for l in drop_sticky_headers(lines) if l.startswith(prompt)]
    assert len(before) == 2
    assert len(after) == 1


def test_recap_trailer_removed(transcript):
    p = parse_claude(transcript("claude_recap_multiline.txt"))
    assert p.body.startswith("에이전트가 마지막 답변을 쓰기 전에 턴이 끝났고")
    assert p.body.endswith("수정을 진행할까요?")
    assert p.recap.startswith("You asked why the slack-7 notification")
    assert p.recap.endswith("report that as a limit failure.")
    assert "disable recaps" not in p.recap
    assert p.duration == "52s"


def test_capture_gap_blank_rows_collapsed(transcript):
    p = parse_claude(transcript("claude_capture_gap.txt"))
    assert p.body.startswith("Both agents are up")
    assert "\n\n\n" not in p.body
    assert p.duration == "2m 9s"
    assert p.recap.startswith("I'm building the Herdr to Slack bridge plugin")


def test_interrupted_turn_does_not_return_previous_answer(transcript):
    text = transcript("claude_interrupted.txt")
    assert parse_claude(text) is None
    r = extract_result(text, "claude")
    assert not r.parsed
    assert "Interrupted" in r.text


def test_blocked_visible_is_incomplete(transcript):
    p = parse_claude(transcript("claude_blocked_visible.txt"))
    assert p is not None
    assert not p.turn_complete
    assert p.duration is None
    assert "Do you want to proceed?" not in p.body


def test_codex_uses_fallback_without_input_box(transcript):
    r = extract_result(transcript("codex_answer.txt"), "codex", fallback_lines=6)
    assert not r.parsed
    lines = r.text.splitlines()
    assert len(lines) == 6
    assert lines[-1].strip().startswith("Worked for 14s")
    assert "Ask Codex to do anything" not in r.text
    assert "GPT-6" not in r.text
    assert any("named pipe" in l for l in lines)


def test_unknown_kind_uses_fallback(transcript):
    r = extract_result(transcript("claude_short.txt"), None, fallback_lines=3)
    assert not r.parsed
    assert r.text.splitlines()[-1].startswith("✻ Crunched for 1s")


@pytest.mark.parametrize("name", [
    "claude_short.txt", "claude_multitool.txt", "claude_long_multiblock.txt",
    "claude_long_table.txt", "claude_multiturn_sticky_recap.txt", "claude_recap_multiline.txt",
    "claude_capture_gap.txt",
])
def test_prompt_box_found_and_status_lines_excluded(transcript, name):
    text = transcript(name)
    lines = split_lines(text)
    assert find_prompt_box(lines) is not None
    body = parse_claude(text).body
    assert "auto mode on" not in body and "manual mode on" not in body
    assert "Context" not in body.splitlines()[-1]


def test_strip_prompt_box_without_input_line_cuts_at_last_rule():
    lines = ["● hi", "", "─" * 20, " Do you want to proceed?", " ❯ 1. Yes"]
    assert strip_prompt_box(lines) == ["● hi", ""]


def test_crlf_and_nbsp_are_normalized():
    text = "❯ hello\r\n\r\n● answer \r\n\r\n✻ Worked for 3s\r\n\r\n" + "─" * 10 + "\r\n❯ \r\n" + "─" * 10
    p = parse_claude(text)
    assert p.body == "answer"
    assert p.duration == "3s"


def test_duration_formats():
    for marker, dur in [("✻ Cooked for 1h 2m 3s · done", "1h 2m 3s"), ("✻ Baked for 1m 50s", "1m 50s"),
                        ("✻ Worked for 12s · done 오전 11:28", "12s")]:
        p = parse_claude(f"❯ q\n\n● a\n\n{marker}\n")
        assert p.duration == dur


def test_turn_without_end_marker_takes_last_block():
    p = parse_claude("❯ q\n\n● first\n\n● Bash(ls)\n  ⎿  out\n\n● final words\n")
    assert p.body == "final words"
    assert not p.turn_complete


def test_no_block_after_latest_prompt_is_none():
    assert parse_claude("❯ q1\n\n● a1\n\n✻ Worked for 1s\n\n❯ q2\n") is None


RULE = "─" * 30
NOTICE = " " * 120 + "✔ Update installed · Restart to update"
DEEP_CODE = " " * 42 + "important_call()"


def test_fallback_tail_keeps_unknown_indented_lines_without_prompt_box():
    text = "a\n\nb\n" + NOTICE + "\nc\n"
    assert fallback_tail(text, 3) == "b\n" + NOTICE + "\nc"
    assert fallback_tail(text, 0) == ""


def test_review13_structured_parse_keeps_deeply_indented_code_and_drops_notice():
    text = "\n".join([
        "❯ show code", "", "● Here:", "", "  def f():", DEEP_CODE, "", "  done.", "",
        "✻ Worked for 2s", "", NOTICE, RULE, "❯ ", RULE, "  Opus 5.5 | Context 1%",
    ])
    p = parse_claude(text)
    assert p.body == "Here:\n\ndef f():\n" + DEEP_CODE[2:] + "\n\ndone."
    assert "Update installed" not in p.body


def test_review13_fallback_keeps_deeply_indented_code():
    claude_tail = "\n".join(["❯ q", "  ⎿  Interrupted", DEEP_CODE, "✻ Worked for 1s", NOTICE, RULE, "❯", RULE])
    out = fallback_tail(claude_tail, 10, "claude")
    assert DEEP_CODE in out.splitlines()
    assert "Update installed" not in out
    codex = "\n".join(["• Here is code:", DEEP_CODE, "  Worked for 3s • 11:33 AM", "", "› Ask Codex to do anything",
                         "  GPT-6-Sol high · weekly 80% left"])
    out = fallback_tail(codex, 10, "codex")
    assert out.splitlines() == ["• Here is code:", DEEP_CODE, "  Worked for 3s • 11:33 AM"]


def test_review13_notice_in_real_fixture_still_removed(transcript):
    for name in ("claude_multiturn_sticky_recap.txt", "claude_recap_multiline.txt", "claude_capture_gap.txt"):
        r = extract_result(transcript(name), "claude")
        assert "Update installed" not in r.text
        tail = fallback_tail(transcript(name), 5, "claude")
        assert "Update installed" not in tail


def test_truncate_text_prefers_line_break():
    text = "\n".join(f"line {i:03d} " + "x" * 40 for i in range(200))
    out, cut = truncate_text(text, 3000)
    assert cut
    assert len(out) <= 3000
    assert out.endswith("\n…")
    assert out[:-2].endswith("x" * 40)  # cut on a line boundary


def test_truncate_text_hard_cut_and_passthrough():
    out, cut = truncate_text("y" * 5000, 3000)
    assert cut and len(out) == 3000
    assert truncate_text("short", 3000) == ("short", False)


def test_truncate_real_long_answer(transcript):
    body = parse_claude(transcript("claude_long_multiblock.txt")).body
    out, cut = truncate_text(body, 3000)
    assert cut and len(out) <= 3000


@pytest.mark.parametrize("closing", [")", "}", "// end", "- item", "# note", "]);"])
def test_recheck_r13_punctuation_led_trailing_code_is_kept(closing):
    aligned = " " * 42 + closing
    claude_tail = "\n".join(["❯ q", "", "● call(", aligned, NOTICE, RULE, "❯", RULE])
    out = fallback_tail(claude_tail, 10, "claude")
    assert out.splitlines()[-1] == aligned
    assert "Update installed" not in out
    p = parse_claude(claude_tail)
    assert p.body.splitlines()[-1] == aligned[2:]
