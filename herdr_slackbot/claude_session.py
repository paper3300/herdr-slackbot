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
#
# Extra record shapes used here (not by `evaluate_lines`):
# - queued prompt (typed while the agent works): {"type": "attachment", "attachment":
#   {"type": "queued_command", "commandMode": "prompt", "prompt": "<text>" | [blocks], "source_uuid"?}}
# - background task notification: a "user" record with origin.kind "task-notification" /
#   promptSource "system", text "<task-notification>...<summary>..</summary>...</task-notification>"
# - compact summary: a "user" record with isCompactSummary / isVisibleInTranscriptOnly
# - bash mode: "user" records whose text is <bash-input>/<bash-stdout>/<bash-stderr>
# - tree links: uuid, parentUuid (logicalParentUuid across a compact boundary)

CONVERSATION_TAIL_BYTES = 4 * 1024 * 1024
_REMINDER_RE = re.compile(r"<system-reminder>.*?</system-reminder>", re.S)  # closed blocks only
_BASH_PREFIXES = ("<bash-input>", "<bash-stdout>", "<bash-stderr>")
_TASK_NOTIFICATION = "<task-notification>"
_SUMMARY_RE = re.compile(r"<summary>(.*?)</summary>", re.S)
TASK_EVENT = "Background task finished"
SYSTEM_EVENT = "System message"
COMPACT_EVENT = "Conversation compacted (earlier messages summarized)"
EVENT_MAX = 200
_LINKED_TYPES = ("user", "assistant", "attachment")


@dataclass(frozen=True)
class Turn:
    role: str  # "user" | "assistant" | "event" (a turn not started by the owner: task notification ...)
    text: str
    at: float | None
    id: str | None = None  # uuid of the record: the prompt / event record, or the answer's last record


@dataclass(frozen=True)
class Conversation:
    turns: list[Turn]
    truncated: bool  # the tail read started mid-file: older messages are missing


@dataclass
class _Start:
    index: int
    role: str  # "user" | "event"
    text: str
    show_head: bool = True
    show_answer: bool = True


def _uuid(entry: dict) -> str | None:
    value = entry.get("uuid")
    return value if isinstance(value, str) and value else None


def _texts(entry: dict) -> list[str]:
    return [str(b.get("text", "")) for b in _blocks(entry) if b.get("type") == "text"]


def _prompt_text(blocks: list) -> str:
    """Visible text of prompt blocks: closed system-reminder blocks and command bookkeeping removed,
    images as `[image]`. If nothing is left, the text with only the reminders removed."""
    parts, raw = [], []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "image":
            parts.append("[image]")
            raw.append("[image]")
        elif kind == "text":
            text = _REMINDER_RE.sub("", str(block.get("text", ""))).strip()
            raw.append(text)
            if text and not text.startswith(_COMMAND_PREFIXES):
                parts.append(text)
    return "\n\n".join(parts) or "\n\n".join(r for r in raw if r)


def _bookkeeping_only(entry: dict) -> bool:
    """No image, and every text block is empty or command bookkeeping once closed <system-reminder>
    blocks are removed (an injected note, a reminder + `<command-...>` record)."""
    blocks = [b for b in _blocks(entry) if b.get("type") in ("text", "image")]
    if any(b.get("type") == "image" for b in blocks):
        return False
    texts = [_REMINDER_RE.sub("", str(b.get("text", ""))).strip() for b in blocks]
    return all(not x or x.startswith(_COMMAND_PREFIXES) for x in texts)


def _queued_prompt(entry: dict) -> list | None:
    """Blocks of a prompt queued while the agent was working, else None."""
    if entry.get("type") != "attachment" or entry.get("isMeta"):
        return None
    att = entry.get("attachment")
    if not isinstance(att, dict) or att.get("type") != "queued_command" or att.get("commandMode") != "prompt":
        return None
    prompt = att.get("prompt")
    if isinstance(prompt, str):
        return [{"type": "text", "text": prompt}] if prompt.strip() else None
    if isinstance(prompt, list):
        blocks = [b for b in prompt if isinstance(b, dict) and b.get("type") in ("text", "image")]
        return blocks or None
    return None


def _event_text(entry: dict) -> str | None:
    """Label for a "user" record that starts a turn but was not written by the owner, else None."""
    if entry.get("isCompactSummary") or entry.get("isVisibleInTranscriptOnly"):
        return COMPACT_EVENT
    origin = entry.get("origin")
    texts = _texts(entry)
    if (isinstance(origin, dict) and origin.get("kind") == "task-notification") or \
            any(t.lstrip().startswith(_TASK_NOTIFICATION) for t in texts):
        summary = next((" ".join(m.group(1).split()) for t in texts for m in [_SUMMARY_RE.search(t)] if m), "")
        if len(summary) > EVENT_MAX:
            summary = summary[:EVENT_MAX - 1] + "…"
        return f"{TASK_EVENT}: {summary}" if summary else TASK_EVENT
    if entry.get("promptSource") == "system":
        return SYSTEM_EVENT
    return None


def _is_bash_mode(entry: dict) -> bool:
    texts = [t.lstrip() for t in _texts(entry)]
    return bool(texts) and all(t.startswith(_BASH_PREFIXES) for t in texts)


def _turn_start(index: int, entry: dict) -> _Start | None:
    """A typed prompt, a queued prompt or a non-owner event (task notification, compact summary)."""
    queued = _queued_prompt(entry)
    if queued is not None:
        return _Start(index, "user", _prompt_text(queued))
    if not _is_prompt(entry) or _bookkeeping_only(entry) or _is_bash_mode(entry):
        return None
    event = _event_text(entry)
    if event is not None:
        return _Start(index, "event", event)
    return _Start(index, "user", _prompt_text(_blocks(entry)))


def _final_text(turn: list[dict], last: bool) -> tuple[str, float | None, str | None] | None:
    """(text, at, uuid) of a turn's answer, by the `evaluate_lines` rule: the text blocks of the turn's
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
    return (text, _ts(final[-1]), _uuid(final[-1])) if text else None


def _parent_of(entry: dict):
    parent = entry.get("parentUuid")
    return entry.get("logicalParentUuid") if parent is None else parent


def _live_branch(entries: list[dict], starts: list[_Start], tail_cut: bool) -> set[str] | None:
    """uuids on the parentUuid (logicalParentUuid where parentUuid is null) chain of the latest
    user/assistant record: the live branch after a rewind or an edited prompt. None (= linear order)
    whenever the chain cannot be trusted: a linked record without uuid, a cycle, a parent missing
    from the tail (accepted only when the tail was cut and no turn starts before that point)."""
    linked = [e for e in entries if e.get("type") in _LINKED_TYPES]
    if not linked or any(not isinstance(e.get("uuid"), str) or not e["uuid"] for e in linked):
        return None
    index = {e["uuid"]: i for i, e in enumerate(entries) if isinstance(e.get("uuid"), str)}
    tip = next((e for e in reversed(entries) if e.get("type") in ("user", "assistant")), None)
    if tip is None:
        return None
    on: set[str] = set()
    uuid = tip["uuid"]
    while True:
        if uuid in on:
            return None  # cycle
        on.add(uuid)
        parent = _parent_of(entries[index[uuid]])
        if parent is None:
            return on  # the root
        if not isinstance(parent, str) or parent not in index:
            if isinstance(parent, str) and tail_cut and all(s.index > index[uuid] for s in starts):
                return on  # the chain leaves the cut tail before its first turn
            return None  # broken inside the tail
        uuid = parent


def _forks_off(uuid: str, entries: list[dict], index: dict[str, int], live: set[str],
               memo: dict[str, bool]) -> bool:
    """True when the ancestors of `uuid` meet the live chain (a real fork: rewind / edited prompt).
    A separate parentless root, a parent missing from the tail or a cycle gives False."""
    path, seen = [], set()
    result = False
    while uuid not in live:
        if uuid in memo:
            result = memo[uuid]
            break
        if uuid in seen or uuid not in index:
            break  # cycle / outside the tail: unknown
        seen.add(uuid)
        path.append(uuid)
        parent = _parent_of(entries[index[uuid]])
        if not isinstance(parent, str):
            break  # another root
        uuid = parent
    else:
        result = True
    for x in path:
        memo[x] = result
    return result


def _mark_abandoned(entries: list[dict], starts: list[_Start], live: set[str]) -> None:
    """Typed prompts on a branch that forks off the live chain are hidden with their answer. Their
    records still end the previous turn, so an abandoned answer never shows up under a live prompt.
    Prompts on a separate root (or whose ancestry leaves the tail) stay in linear order. Queued
    prompts and events are always kept (their place in the tree is not relied on)."""
    index = {e["uuid"]: i for i, e in enumerate(entries) if isinstance(e.get("uuid"), str)}
    memo: dict[str, bool] = {}
    for start in starts:
        entry = entries[start.index]
        if start.role == "user" and entry.get("type") == "user" and entry["uuid"] not in live                 and _forks_off(entry["uuid"], entries, index, live, memo):
            start.show_head = start.show_answer = False


def _mark_duplicates(entries: list[dict], starts: list[_Start]) -> None:
    """A queued prompt that also exists as a "user" record (same text, linked by source_uuid): the
    later copy is not shown again; its answer still is.

    Speculative: in real transcripts `attachment.source_uuid` points at `queue-operation` records,
    not at any record's uuid, and no user record carries `source_uuid`/`sourceUuid`, so this link has
    never been seen to match (docs/review/history-en-recheck.md O1). Kept as a harmless guard."""
    seen: dict[str, str] = {}
    for start in starts:
        if start.role != "user" or not start.show_head:
            continue
        entry = entries[start.index]
        att = entry.get("attachment") if entry.get("type") == "attachment" else None
        ids = {x for x in ((att or {}).get("source_uuid"), entry.get("uuid"), entry.get("source_uuid"),
                           entry.get("sourceUuid")) if isinstance(x, str) and x}
        if any(seen.get(x) == start.text for x in ids):
            start.show_head = False
            continue
        for x in ids:
            seen.setdefault(x, start.text)


def conversation_from_lines(lines: Iterable[str], tail_cut: bool = False) -> list[Turn]:
    """Prompts (typed, or queued while the agent worked), turn events (background task finished,
    compaction) and the agent's final answers, oldest first. Each queued prompt starts its own
    sub-turn, so an interrupt only drops that sub-turn's answer. Tool calls/results, thinking,
    meta/command/bash-mode records, interrupts and sidechains are left out; an unanswered prompt has
    no assistant entry. Records before the first turn are dropped. Prompts on an abandoned branch
    are hidden when the parentUuid tree in the tail is intact (`tail_cut`: the tail starts mid-file)."""
    entries, _ = _decode(lines)
    entries = [e for e in entries if _well_formed(e)]
    starts = [s for s in (_turn_start(i, e) for i, e in enumerate(entries)) if s is not None]
    if not starts:
        return []
    live = _live_branch(entries, starts, tail_cut)
    if live is not None:
        _mark_abandoned(entries, starts, live)
    _mark_duplicates(entries, starts)
    turns: list[Turn] = []
    for n, start in enumerate(starts):
        if start.show_head:
            turns.append(Turn(start.role, start.text, _ts(entries[start.index]), _uuid(entries[start.index])))
        if not start.show_answer:
            continue
        end = starts[n + 1].index if n + 1 < len(starts) else len(entries)
        answer = _final_text(entries[start.index + 1:end], last=n + 1 == len(starts))
        if answer is not None:
            turns.append(Turn("assistant", *answer))
    return turns


def conversation(session_id: str, cwd: str | None, base: Path | None = None,
                 max_bytes: int = CONVERSATION_TAIL_BYTES) -> Conversation | None:
    """The session's conversation from its JSONL tail, or None without a usable transcript."""
    path = find_session_file(session_id, cwd, base)
    if path is None:
        return None
    try:
        lines, _, cut = _read_tail(path, max_bytes)
        return Conversation(conversation_from_lines(lines, tail_cut=cut), cut)
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
