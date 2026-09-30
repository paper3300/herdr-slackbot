"""Extract an agent's last response from a Herdr transcript (D10).

Input is `herdr agent read <target> --source recent-unwrapped` text. Claude Code
layout, bottom-up (verified on Claude Code 2.1.285 transcripts):

    ● <final response block>          <- last `●` block of the latest turn
      ...
    ✻ Crunched for 2m 9s · done ...   <- turn-end marker (also on 1s turns)
    ※ recap: <summary>                <- optional, may wrap onto indented lines
                  ✔ Update installed  <- optional right-aligned notice
    ──────────────────────────        <- prompt box
    ❯ <input or ghost suggestion>
    ──────────────────────────
      Opus 5.5 | Context 7%            <- status lines

Quirks handled:
- Herdr captures Claude's alternate-screen history by scrolling. Each captured
  page can start with Claude's sticky header, a copy of the current turn's
  `❯ <prompt>` line, which shows up in the middle of the response. Such repeats
  of the latest prompt within the same turn are dropped.
- A `❯` line typed while the agent works (queued message) is a real echo.
- An interrupted turn has no `●` after its prompt echo -> no parse (fallback).

Anything that does not fit returns None from `parse_claude`, and
`extract_result` falls back to the last N non-blank lines.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

RULE_RE = re.compile(r"^\s*─{8,}\s*$")
PROMPT_RE = re.compile(r"^❯(?:[\s ]|$)")
BLOCK_RE = re.compile(r"^●(?:\s|$)")
TURN_END_RE = re.compile(
    r"^✻\s+\S.*?\bfor\s+(?P<dur>\d+h(?:\s*\d+m)?(?:\s*\d+s)?|\d+m(?:\s*\d+s)?|\d+(?:\.\d+)?s)\b"
)
RECAP_RE = re.compile(r"^※\s*recap:\s*(?P<text>.*)$")
RECAP_TRAILER_RE = re.compile(r"\s*\(disable recaps in /config\)\s*$")
CODEX_INPUT_RE = re.compile(r"^›(?:[\s ]|$)")
# Right-aligned UI notice directly above the prompt box, e.g.
# "<~140 spaces>✔ Update installed · Restart to update": deep indent, then one of Claude's
# status glyphs and a space. Other punctuation-led lines (`)`, `}`, `//`, `-`) are content.
NOTICE_RE = re.compile(r"^ {20,}[✔✓✗✘⚠]️? ")

TRUNCATION_MARK = "\n…"


@dataclass(frozen=True)
class ParsedResponse:
    body: str
    recap: str | None = None
    duration: str | None = None
    turn_complete: bool = True


@dataclass(frozen=True)
class ResultBody:
    text: str
    recap: str | None
    duration: str | None
    parsed: bool  # True: structured result (jsonl or screen parse); False: raw-tail fallback
    source: str = "screen"  # "jsonl" | "screen" | "tail"
    at: float | None = None  # when the answer was written (JSONL only)


def _clean(line: str) -> str:
    return line.rstrip().rstrip(" ").rstrip()


def split_lines(text: str) -> list[str]:
    return [_clean(line) for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")]


def _prompt_text(line: str) -> str:
    return line[1:].strip("  ")


def find_prompt_box(lines: list[str]) -> int | None:
    """Index of the top rule of Claude's input box (rule followed by a `❯` line)."""
    for i in range(len(lines) - 1, 0, -1):
        if PROMPT_RE.match(lines[i]) and RULE_RE.match(lines[i - 1]):
            return i - 1
    return None


def strip_prompt_box(lines: list[str]) -> list[str]:
    """Drop Claude's input box and everything below it (status lines)."""
    cut = find_prompt_box(lines)
    if cut is None:
        # No `❯` input line (e.g. a permission dialog replaced it): cut at the last rule.
        rules = [i for i, line in enumerate(lines) if RULE_RE.match(line)]
        cut = rules[-1] if rules else len(lines)
    return lines[:cut]


def drop_trailing_notices(lines: list[str]) -> list[str]:
    """Remove right-aligned UI notices sitting just above Claude's prompt box.

    Only the trailing lines (after the last content, blanks aside) are considered, and
    only glyph-led ones, so deeply indented answer content (code) is never touched.
    """
    end = len(lines)
    while end > 0:
        line = lines[end - 1]
        if not line.strip() or NOTICE_RE.match(line):
            end -= 1
            continue
        break
    kept = [line for line in lines[end:] if not NOTICE_RE.match(line)]
    return lines[:end] + kept


def drop_sticky_headers(lines: list[str]) -> list[str]:
    """Remove repeated copies of the current turn's `❯ <prompt>` line (scroll-capture artifact)."""
    out: list[str] = []
    current_prompt: str | None = None
    turn_ended = True
    for line in lines:
        if PROMPT_RE.match(line):
            text = _prompt_text(line)
            if (current_prompt is not None and not turn_ended and text
                    and (current_prompt.startswith(text) or text.startswith(current_prompt))):
                continue
            current_prompt, turn_ended = text, False
        elif TURN_END_RE.match(line):
            turn_ended = True
        out.append(line)
    return out


def _collect_recap(lines: list[str], start: int) -> str | None:
    for i in range(start, len(lines)):
        m = RECAP_RE.match(lines[i])
        if not m:
            continue
        parts = [m.group("text").strip()]
        for cont in lines[i + 1:]:
            if not cont.startswith("  ") or not cont.strip():
                break
            parts.append(cont.strip())
        recap = RECAP_TRAILER_RE.sub("", " ".join(p for p in parts if p)).strip()
        return recap or None
    return None


def _block_text(block: list[str]) -> str:
    out: list[str] = []
    for idx, line in enumerate(block):
        if idx == 0:
            line = line[1:].lstrip(" ")  # drop the "●" marker
        elif line.startswith("  "):
            line = line[2:]
        if not line.strip():
            if out and out[-1] == "":
                continue
            line = ""
        out.append(line)
    while out and not out[-1]:
        out.pop()
    while out and not out[0]:
        out.pop(0)
    return "\n".join(out)


def parse_claude(text: str) -> ParsedResponse | None:
    lines = drop_trailing_notices(strip_prompt_box(split_lines(text)))
    lines = drop_sticky_headers(lines)

    prompt_idx = max((i for i, l in enumerate(lines) if PROMPT_RE.match(l)), default=-1)
    end_idx = max((i for i, l in enumerate(lines) if TURN_END_RE.match(l)), default=-1)

    if end_idx > prompt_idx:
        search_hi, complete = end_idx, True
    else:
        search_hi, complete = len(lines), False  # latest turn has no end marker (yet)
    block_idx = max((i for i in range(prompt_idx + 1, search_hi) if BLOCK_RE.match(lines[i])), default=-1)
    if block_idx < 0:
        return None

    block_hi = search_hi
    recap_start = end_idx + 1 if complete else block_idx + 1
    if not complete:
        recap_at = next((i for i in range(block_idx + 1, len(lines)) if RECAP_RE.match(lines[i])), None)
        if recap_at is not None:
            block_hi = recap_at
    body = _block_text(lines[block_idx:block_hi])
    if not body:
        return None
    duration = None
    if complete:
        m = TURN_END_RE.match(lines[end_idx])
        duration = m.group("dur") if m else None
    return ParsedResponse(body, _collect_recap(lines, recap_start), duration, complete)


def strip_codex_input(lines: list[str]) -> list[str]:
    """Drop Codex's input line (`› ...`) and the status lines below it."""
    for i in range(len(lines) - 1, -1, -1):
        if CODEX_INPUT_RE.match(lines[i]):
            return lines[:i]
    return lines


def fallback_tail(text: str, n: int = 40, agent_kind: str | None = None) -> str:
    """Last `n` non-blank lines, without the agent's input box/status lines."""
    lines = split_lines(text)
    if agent_kind == "codex":
        lines = strip_codex_input(lines)
    elif find_prompt_box(lines) is not None:
        lines = drop_trailing_notices(strip_prompt_box(lines))
    lines = [line for line in lines if line.strip()]
    return "\n".join(lines[-n:]) if n > 0 else ""


def extract_result(text: str, agent_kind: str | None, fallback_lines: int = 40) -> ResultBody:
    if agent_kind == "claude":
        parsed = parse_claude(text)
        if parsed is not None:
            return ResultBody(parsed.body, parsed.recap, parsed.duration, True, "screen")
    return ResultBody(fallback_tail(text, fallback_lines, agent_kind), None, None, False, "tail")


def truncate_text(text: str, limit: int) -> tuple[str, bool]:
    """Cut `text` to at most `limit` chars (preferring a line break), marking the cut with `…`."""
    if len(text) <= limit:
        return text, False
    budget = max(limit - len(TRUNCATION_MARK), 0)
    cut = text.rfind("\n", 0, budget + 1)
    if cut < budget * 0.8:
        cut = budget
    return text[:cut].rstrip() + TRUNCATION_MARK, True
