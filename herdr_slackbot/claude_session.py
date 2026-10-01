"""Primary result source for Claude agents: Claude Code's session transcript (JSONL).

Claude Code appends every turn to
`~/.claude/projects/<cwd slug>/<session id>.jsonl` (or `$CLAUDE_CONFIG_DIR/projects`),
where the session id equals Herdr's `agent_session.value` and the slug is the cwd
with every non-alphanumeric character replaced by `-` (`D:\\Git\\x` -> `D--Git-x`).

Entry shapes relied on (Claude Code 2.1.285):
- prompt:    {"type": "user", "message": {"content": "<text>" | [text/image blocks]}, "timestamp": ISO}
- tool result: {"type": "user", "message": {"content": [{"type": "tool_result", ...}]}}
- interrupt: {"type": "user", "message": {"content": [{"type": "text", "text": "[Request interrupted ..."}]}}
- assistant: {"type": "assistant", "message": {"id": "msg_..", "stop_reason": .., "content": [one block]}}
  (one line per content block; all lines of one API message share `message.id`)
- turn end:  {"type": "system", "subtype": "turn_duration", "durationMs": 1437}
Entries with `isSidechain: true` (subagents) or `isMeta: true` are ignored.

`last_answer()` returns None whenever the file cannot give a trustworthy answer for
the latest turn; callers then fall back to the screen parser. An answer is trusted
only when the file ends on a complete record and the final message has a terminal
`stop_reason` (`end_turn` ...). A read that catches Claude mid-append (unterminated
or undecodable last record, final message without a stop reason) is retried a few
times before giving up. Malformed records are skipped; they never raise.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable

from .naming import format_duration
from .parser import ResultBody, extract_result

log = logging.getLogger(__name__)

TAIL_BYTES = 8 * 1024 * 1024
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{7,127}$")
_INTERRUPT_PREFIX = "[Request interrupted"
_COMMAND_PREFIXES = ("<command-", "<local-command")
TERMINAL_STOP_REASONS = frozenset({"end_turn", "stop_sequence", "max_tokens"})
READ_RETRIES = 3
READ_RETRY_DELAY = 0.3


@dataclass(frozen=True)
class ClaudeAnswer:
    text: str
    prompt_at: float | None
    answered_at: float | None
    duration: float | None  # seconds


def projects_dir(env: dict | None = None) -> Path:
    env = os.environ if env is None else env
    base = env.get("CLAUDE_CONFIG_DIR")
    return (Path(base) if base else Path.home() / ".claude") / "projects"


def cwd_slug(cwd: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "-", cwd.rstrip("\\/"))


def find_session_file(session_id: str, cwd: str | None, base: Path | None = None) -> Path | None:
    if not session_id or not _SESSION_ID_RE.match(session_id):
        return None
    base = base or projects_dir()
    if cwd:
        candidate = base / cwd_slug(cwd) / f"{session_id}.jsonl"
        if candidate.is_file():
            return candidate
    try:
        return next(iter(sorted(base.glob(f"*/{session_id}.jsonl"))), None)
    except OSError:
        return None


def _ts(entry: dict) -> float | None:
    raw = entry.get("timestamp")
    if not isinstance(raw, str):
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _message(entry: dict) -> dict:
    message = entry.get("message")
    return message if isinstance(message, dict) else {}


def _blocks(entry: dict) -> list:
    content = _message(entry).get("content")
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []


MAX_DURATION_MS = 30 * 24 * 3600 * 1000  # anything longer is treated as invalid metadata


def _duration_ms(entry: dict) -> float | None:
    value = entry.get("durationMs")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    if not math.isfinite(value) or value < 0 or value > MAX_DURATION_MS:
        return None
    return value


def _well_formed(entry: dict) -> bool:
    """Shape check for records that can start a turn or carry an answer."""
    if entry.get("type") not in ("user", "assistant"):
        return True
    message = entry.get("message")
    if not isinstance(message, dict):
        return False
    content = message.get("content")
    if isinstance(content, str):
        return True
    if not isinstance(content, list):
        return False
    for block in content:
        if not isinstance(block, dict):
            return False
        if block.get("type") == "text" and not isinstance(block.get("text"), str):
            return False
    return True


def _is_interrupt(entry: dict) -> bool:
    return entry.get("type") == "user" and any(
        b.get("type") == "text" and str(b.get("text", "")).startswith(_INTERRUPT_PREFIX)
        for b in _blocks(entry) if isinstance(b, dict))


def _is_prompt(entry: dict) -> bool:
    if entry.get("type") != "user" or entry.get("isMeta") or entry.get("isSidechain"):
        return False
    blocks = [b for b in _blocks(entry) if isinstance(b, dict)]
    if not blocks or any(b.get("type") == "tool_result" for b in blocks):
        return False
    texts = [str(b.get("text", "")) for b in blocks if b.get("type") == "text"]
    if any(t.startswith(_INTERRUPT_PREFIX) for t in texts):
        return False
    if texts and all(t.lstrip().startswith(_COMMAND_PREFIXES) for t in texts):
        return False  # slash-command bookkeeping, not a prompt
    return any(b.get("type") in ("text", "image") for b in blocks)


def _decode(lines: Iterable[str]) -> tuple[list[dict], bool]:
    """(entries, last_record_ok). A garbled line in the middle is skipped; a garbled last
    record means the file is being appended to."""
    entries = []
    last_ok = True
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            last_ok = False  # partial line at the tail start, garbage, or an unfinished append
            continue
        last_ok = True
        if isinstance(entry, dict) and not entry.get("isSidechain"):
            entries.append(entry)
    return entries, last_ok


def evaluate_lines(lines: Iterable[str], since: float | None = None,
                   tail_complete: bool = True) -> tuple[ClaudeAnswer | None, bool]:
    """(answer, uncertain). `uncertain` = the transcript may still be being written
    (retrying later can change the outcome); otherwise None is a definite "no answer"."""
    entries, last_ok = _decode(lines)
    if not tail_complete or not last_ok:
        return None, True
    prompt_idx = max((i for i, e in enumerate(entries) if _is_prompt(e)), default=-1)
    if prompt_idx < 0:
        return None, False
    prompt = entries[prompt_idx]
    turn = entries[prompt_idx + 1:]
    if any(not _well_formed(e) for e in turn):
        # A malformed user/assistant record after the last prompt could be a newer prompt or
        # the real answer: never report the older answer as current. Use the screen instead.
        return None, False
    if any(_is_interrupt(e) for e in turn):
        return None, False
    assistants = [e for e in turn if e.get("type") == "assistant" and _message(e)]
    if not assistants:
        return None, True  # the answer may not be written yet
    last_id = _message(assistants[-1]).get("id")
    final = [e for e in assistants if _message(e).get("id") == last_id]
    stop = _message(final[-1]).get("stop_reason")
    if stop == "tool_use" or any(_message(e).get("stop_reason") == "tool_use" for e in final):
        return None, False  # turn stopped at a tool call (still running, interrupted or blocked)
    if stop not in TERMINAL_STOP_REASONS:
        return None, True  # message not finished (or unknown shape): don't trust it
    text = "\n\n".join(
        str(b.get("text", "")).strip()
        for e in final for b in _blocks(e)
        if b.get("type") == "text" and str(b.get("text", "")).strip()
    )
    if not text:
        return None, False
    prompt_at, answered_at = _ts(prompt), _ts(final[-1])
    if since is not None and (answered_at is None or answered_at <= since):
        return None, True  # the answer to the caller's prompt may not be written yet
    duration = None
    for e in turn:
        if e.get("type") == "system" and e.get("subtype") == "turn_duration":
            ms = _duration_ms(e)
            if ms is not None:
                duration = ms / 1000.0
    if duration is None and prompt_at is not None and answered_at is not None:
        duration = max(answered_at - prompt_at, 0.0)
    return ClaudeAnswer(text, prompt_at, answered_at, duration), False


def last_answer_from_lines(lines: Iterable[str], since: float | None = None,
                           tail_complete: bool = True) -> ClaudeAnswer | None:
    """Final assistant message of the latest turn, or None if the turn has no trustworthy answer.

    since: epoch seconds the caller sent its prompt; the answer must be newer than that.
    """
    return evaluate_lines(lines, since, tail_complete)[0]


def read_tail(path: Path, max_bytes: int = TAIL_BYTES) -> tuple[list[str], bool]:
    """(lines, tail_complete): tail_complete is False when the file does not end with a newline."""
    lines, complete, _ = _read_tail(path, max_bytes)
    return lines, complete


def _read_tail(path: Path, max_bytes: int) -> tuple[list[str], bool, bool]:
    """(lines, tail_complete, cut): cut is True when the read started after the file's start."""
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        start = max(size - max_bytes, 0)
        f.seek(start)
        data = f.read()
    lines = data.decode("utf-8", "replace").splitlines()
    if start > 0 and lines:
        lines = lines[1:]  # first line is probably partial
    return lines, (not data or data.endswith(b"\n")), start > 0


def read_tail_lines(path: Path, max_bytes: int = TAIL_BYTES) -> list[str]:
    return read_tail(path, max_bytes)[0]


def last_answer(session_id: str, cwd: str | None, since: float | None = None,
                base: Path | None = None, max_bytes: int = TAIL_BYTES, retries: int = READ_RETRIES,
                retry_delay: float = READ_RETRY_DELAY,
                sleep: Callable[[float], None] = time.sleep) -> ClaudeAnswer | None:
    path = find_session_file(session_id, cwd, base)
    if path is None:
        return None
    for attempt in range(retries + 1):
        try:
            lines, complete = read_tail(path, max_bytes)
            answer, uncertain = evaluate_lines(lines, since, complete)
        except OSError as exc:
            log.warning("cannot read %s: %s", path, exc)
            return None
        except (TypeError, ValueError, AttributeError, KeyError) as exc:
            log.warning("unusable transcript %s: %s", path, exc)
            return None
        if answer is not None or not uncertain:
            return answer
        if attempt < retries:
            sleep(retry_delay)
    log.info("transcript %s still incomplete after %d reads; using the screen", path, retries + 1)
    return None


# --- conversation history (send modal) ---------------------------------------------------

CONVERSATION_TAIL_BYTES = 4 * 1024 * 1024
_REMINDER_RE = re.compile(r"<system-reminder>.*?(</system-reminder>|\Z)", re.S)


@dataclass(frozen=True)
class Turn:
    role: str  # "user" | "assistant"
    text: str
    at: float | None


@dataclass(frozen=True)
class Conversation:
    turns: list[Turn]
    truncated: bool  # the tail read started mid-file: older messages are missing


def _prompt_text(entry: dict) -> str:
    """A prompt's visible text: text blocks (system reminders and command bookkeeping removed),
    images as `[image]`."""
    parts = []
    for block in _blocks(entry):
        kind = block.get("type")
        if kind == "image":
            parts.append("[image]")
        elif kind == "text":
            text = _REMINDER_RE.sub("", str(block.get("text", ""))).strip()
            if text and not text.startswith(_COMMAND_PREFIXES):
                parts.append(text)
    return "\n\n".join(parts)


def _final_text(turn: list[dict], last: bool) -> tuple[str, float | None] | None:
    """(text, at) of a turn's answer, by the `evaluate_lines` rule: the text blocks of the turn's
    last assistant message. None when the turn was interrupted, stopped at a tool call, or (for the
    latest turn) the message is not finished yet."""
    if any(_is_interrupt(e) for e in turn):
        return None
    assistants = [e for e in turn if e.get("type") == "assistant" and _message(e)]
    if not assistants:
        return None
    last_id = _message(assistants[-1]).get("id")
    final = [e for e in assistants if _message(e).get("id") == last_id]
    stops = {_message(e).get("stop_reason") for e in final}
    if "tool_use" in stops or any(b.get("type") == "tool_use" for e in final for b in _blocks(e)):
        return None
    if last and not stops & TERMINAL_STOP_REASONS:
        return None  # the latest answer may still be streaming
    text = "\n\n".join(
        str(b.get("text", "")).strip()
        for e in final for b in _blocks(e)
        if b.get("type") == "text" and str(b.get("text", "")).strip()
    )
    return (text, _ts(final[-1])) if text else None


def conversation_from_lines(lines: Iterable[str]) -> list[Turn]:
    """User prompts and the agent's final answers, oldest first. Tool calls/results, thinking,
    meta/command records, interrupts and sidechains are left out; an unanswered or interrupted
    prompt has no assistant entry. Records before the first prompt (a cut-off tail) are dropped."""
    entries, _ = _decode(lines)
    entries = [e for e in entries if _well_formed(e)]
    # a prompt record carrying only a system reminder / command output does not start a turn
    starts = [i for i, e in enumerate(entries) if _is_prompt(e) and _prompt_text(e)]
    turns: list[Turn] = []
    for n, start in enumerate(starts):
        turns.append(Turn("user", _prompt_text(entries[start]), _ts(entries[start])))
        end = starts[n + 1] if n + 1 < len(starts) else len(entries)
        answer = _final_text(entries[start + 1:end], last=n + 1 == len(starts))
        if answer is not None:
            turns.append(Turn("assistant", answer[0], answer[1]))
    return turns


def conversation(session_id: str, cwd: str | None, base: Path | None = None,
                 max_bytes: int = CONVERSATION_TAIL_BYTES) -> Conversation | None:
    """The session's conversation from its JSONL tail, or None without a usable transcript."""
    path = find_session_file(session_id, cwd, base)
    if path is None:
        return None
    try:
        lines, _, cut = _read_tail(path, max_bytes)
        return Conversation(conversation_from_lines(lines), cut)
    except OSError as exc:
        log.warning("cannot read %s: %s", path, exc)
    except (TypeError, ValueError, AttributeError, KeyError) as exc:
        log.warning("unusable transcript %s: %s", path, exc)
    return None


def agent_result(kind: str | None, session_id: str | None, cwd: str | None,
                 read_screen: Callable[[], str], since: float | None = None,
                 fallback_lines: int = 40, base: Path | None = None,
                 retry_delay: float = READ_RETRY_DELAY) -> ResultBody:
    """Result body for a finished turn: Claude JSONL first, then the screen parser / raw tail."""
    if kind == "claude" and session_id:
        try:
            answer = last_answer(session_id, cwd, since, base, retry_delay=retry_delay)
            if answer is not None:
                return ResultBody(answer.text, None, format_duration(answer.duration) or None, True, "jsonl",
                                  answer.answered_at)
        except Exception:  # never let a transcript problem block the screen fallback
            log.exception("reading the Claude transcript failed")
    return extract_result(read_screen(), kind, fallback_lines)
