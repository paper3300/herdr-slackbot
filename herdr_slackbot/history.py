"""Thread history: the conversation since the previous notification, for completion messages.

A thread entry keeps `history_cursor` = {"id": <record uuid>, "at": <epoch>} of the last
conversation item already covered by a posted result. The next result covers the turns after it
up to and including the finished turn's answer. Without a usable cursor (first message, older
state, cursor gone from the tail and no timestamp anchor) only the latest turn is covered, never
the whole session.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Mapping, Sequence

from .claude_session import Turn
from .naming import truncate


@dataclass(frozen=True)
class HistoryRange:
    turns: list[Turn]  # oldest first; the last one is the finished turn's answer
    cursor: dict  # the cursor to persist once the result was posted
    from_cursor: bool  # False: no usable cursor, only the latest turn
    advance: bool = True  # False: the cursor already covers the answer (nothing new): keep it


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _final_index(turns: Sequence[Turn], final_text: str) -> int | None:
    """The answer the result shows: the newest assistant turn with that text."""
    want = (final_text or "").strip()
    if not want:
        return None
    return next((i for i in range(len(turns) - 1, -1, -1)
                 if turns[i].role == "assistant" and turns[i].text.strip() == want), None)


def _latest_turn_start(turns: Sequence[Turn], end: int) -> int:
    """Start of the finished turn: everything after the previous answer (its prompt(s), queued
    prompts and events)."""
    start = end
    while start > 0 and turns[start - 1].role != "assistant":
        start -= 1
    return start


def _number(value) -> float | None:
    return None if isinstance(value, bool) or not isinstance(value, (int, float)) else float(value)


def _after_cursor(turns: Sequence[Turn], end: int, cursor: Mapping | None) -> int | None:
    """Index of the first turn after the cursor, or None when the cursor cannot place it. A cursor
    at or after the finished answer gives `end + 1` (nothing new)."""
    if not isinstance(cursor, Mapping):
        return None
    cid = cursor.get("id")
    if isinstance(cid, str) and cid:
        for i, turn in enumerate(turns):
            if turn.id == cid:
                return min(i, end) + 1
    at = _number(cursor.get("at"))
    if at is None:
        return None
    final_at = turns[end].at
    if final_at is not None and at >= final_at:
        return end + 1  # the cursor already covers the answer
    # timestamp fallback: needs an anchor (a turn at/before the cursor) so that nothing older
    # than the tail is mistaken for "since the cursor"
    anchored = False
    for i, turn in enumerate(turns[:end + 1]):
        if turn.at is None:
            continue
        if turn.at <= at:
            anchored = True
        elif anchored:
            return i
        else:
            return None
    return None


def select_range(turns: Sequence[Turn], final_text: str, cursor: Mapping | None) -> HistoryRange | None:
    """Turns covered by the next result, or None if the final answer is not in the conversation
    (the caller then posts the plain result and keeps the cursor)."""
    end = _final_index(turns, final_text)
    if end is None:
        return None
    final = turns[end]
    start = _after_cursor(turns, end, cursor)
    if start is not None and start > end:  # no new turn since the cursor: today's plain result
        return HistoryRange([final], dict(cursor), True, advance=False)
    from_cursor = start is not None
    if start is None:
        start = _latest_turn_start(turns, end)
    new = {"id": final.id, "at": final.at}
    # placed by id or time, the answer is after the cursor; unplaced, only time can tell
    advance = from_cursor or newer_cursor(cursor, new)
    return HistoryRange(list(turns[start:end + 1]), new, from_cursor, advance)


def skip_slack_prompt(turns: Sequence[Turn], prompt: str | None, excerpt: int) -> list[Turn]:
    """Drop the last user turn equal to the Slack task's prompt (already visible in the thread).
    `prompt` is the stored excerpt (whitespace-normalized, truncated to `excerpt`)."""
    items = list(turns)
    if not prompt:
        return items
    for i in range(len(items) - 1, -1, -1):
        if items[i].role == "user" and truncate(_norm(items[i].text), excerpt) == prompt:
            return items[:i] + items[i + 1:]
    return items


def newer_cursor(old: Mapping | None, new: Mapping) -> bool:
    """A cursor only moves forward (a replayed post never moves it back). A timestamped cursor is
    never replaced by one without a timestamp (their order is unknown); `select_range` decides
    that case by the turn order instead."""
    if not isinstance(old, Mapping):
        return True
    old_at, new_at = _number(old.get("at")), _number(new.get("at"))
    if old_at is not None:
        return new_at is not None and new_at >= old_at
    return True


def range_markdown(turns: Sequence[Turn], agent_label: str) -> str:
    """The whole range, untruncated, for the [View full] upload."""
    out = []
    for turn in turns:
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(turn.at)) if turn.at else ""
        if turn.role == "event":
            out.append(f"⚙️ _{turn.text}_" + (f" · {when}" if when else ""))
            continue
        who = "👤 You" if turn.role == "user" else f"🤖 {agent_label}"
        out.append(f"**{who}**" + (f" · {when}" if when else "") + "\n\n" + turn.text.strip("\n"))
    return "\n\n---\n\n".join(out) + "\n"
