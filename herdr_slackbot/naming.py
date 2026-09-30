"""Agent / tab naming and display helpers (pure functions)."""

from __future__ import annotations

import re
from typing import Iterable, Mapping

AGENT_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
AUTO_NAME_PREFIX = "slack-"
TAB_LABEL_MAX = 60
PROMPT_SUMMARY_MAX = 40
SLACK_OPTION_TEXT_MAX = 75  # Slack plain_text option limit

STATUS_EMOJI = {
    "idle": "🟢",
    "done": "✅",
    "working": "⏳",
    "blocked": "⚠️",
    "unknown": "❔",
}


def auto_agent_name(counter: int) -> str:
    return f"{AUTO_NAME_PREFIX}{counter}"


def validate_agent_name(name: str, live_names: Iterable[str | None] = ()) -> str | None:
    """Return an error message, or None when `name` is usable for a new agent."""
    if not AGENT_NAME_RE.match(name or ""):
        return (f"Invalid name {name!r}: use a lowercase letter first, then up to 31 of "
                "a-z, 0-9, '-' or '_'.")
    if name in {n for n in live_names if n}:
        return f"An agent named {name!r} is already running."
    return None


def truncate(text: str, max_len: int) -> str:
    if len(text) <= max_len:
        return text
    if max_len <= 1:
        return "…"[:max_len]
    return text[: max_len - 1].rstrip() + "…"


def summarize_prompt(prompt: str, max_len: int = PROMPT_SUMMARY_MAX) -> str:
    """First non-empty line of the prompt, whitespace-collapsed and shortened."""
    for line in (prompt or "").splitlines():
        line = " ".join(line.split())
        if line:
            return truncate(line, max_len)
    return ""


def tab_label(name: str, prompt: str, auto_named: bool, max_len: int = TAB_LABEL_MAX) -> str:
    """Auto names get `slack-<N> <prompt summary>`; a user-given name is the label as-is."""
    if not auto_named:
        return name
    summary = summarize_prompt(prompt)
    label = f"{name} {summary}" if summary else name
    return truncate(label, max_len)


def status_emoji(status: str | None) -> str:
    return STATUS_EMOJI.get(status or "unknown", STATUS_EMOJI["unknown"])


def agent_display_name(agent: Mapping) -> str:
    return agent.get("name") or agent.get("pane_id") or "?"


def agent_option_label(agent: Mapping, workspace_labels: Mapping[str, str],
                       max_len: int = SLACK_OPTION_TEXT_MAX) -> str:
    """`<emoji> · <name or pane_id> · <workspace label> · <status> · <terminal title>` (D5)."""
    status = agent.get("agent_status") or "unknown"
    ws_id = agent.get("workspace_id") or ""
    parts = [status_emoji(status), agent_display_name(agent), workspace_labels.get(ws_id, ws_id), status]
    title = (agent.get("terminal_title_stripped") or agent.get("terminal_title") or "").strip()
    if title:
        parts.append(title)
    return truncate(" · ".join(p for p in parts if p), max_len)


def format_duration(seconds: float | None) -> str:
    if seconds is None or seconds < 0:
        return ""
    total = int(round(seconds))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"
