"""Pure parsing for slash-command arguments and target resolution."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Mapping, Sequence

from .agents import KINDS

NEW_KEYS = ("name", "kind", "model", "effort", "mode", "cwd")


def parse_command(text: str) -> tuple[str, str]:
    """`"send coder fix it"` -> ("send", "coder fix it"). Keeps newlines in the remainder."""
    text = (text or "").strip()
    if not text:
        return "", ""
    parts = text.split(None, 1)
    return parts[0].lower(), (parts[1] if len(parts) > 1 else "")


class ParseError(ValueError):
    pass


def _scan(text: str, pos: int) -> tuple[str, int] | None:
    """Next whitespace-delimited token from `pos` -> (token, end) or None at the end.

    Double quotes group text anywhere in a token (`"My WS"`, `cwd="D:\\My Project"`) and
    are removed; backslashes are literal (Windows paths). An unmatched quote raises.
    """
    n = len(text)
    while pos < n and text[pos].isspace():
        pos += 1
    if pos >= n:
        return None
    out = []
    while pos < n and not text[pos].isspace():
        if text[pos] == '"':
            close = text.find('"', pos + 1)
            if close < 0:
                raise ParseError("Unmatched double quote.")
            out.append(text[pos + 1:close])
            pos = close + 1
        else:
            out.append(text[pos])
            pos += 1
    return "".join(out), pos


@dataclass(frozen=True)
class NewArgs:
    workspace: str
    prompt: str
    name: str | None = None
    kind: str = "claude"
    model: str | None = None
    effort: str | None = None
    mode: str | None = None
    cwd: str | None = None


def parse_new_args(rest: str) -> NewArgs | str:
    """`<workspace> [key=value ...] <prompt...>`; returns an error string on bad input."""
    try:
        first = _scan(rest, 0)
        if first is None:
            return "Missing workspace. Usage: new <workspace> [name=..] [kind=..] <prompt>"
        workspace, prompt_start = first
        options: dict[str, str] = {}
        while True:
            # Only recognized key=value options are scanned (and may be quoted);
            # the prompt that follows is taken verbatim.
            m = re.match(r"\s*(" + "|".join(NEW_KEYS) + r")=", rest[prompt_start:])
            if not m:
                break
            item = _scan(rest, prompt_start)
            key, _, value = item[0].partition("=")
            options[key] = value
            prompt_start = item[1]
    except ParseError as exc:
        return str(exc)
    prompt = rest[prompt_start:].strip()
    if not prompt:
        return "Missing prompt."
    kind = options.get("kind", "claude")
    if kind not in KINDS:
        return f"Unknown kind {kind!r} (choose {', '.join(KINDS)})."
    return NewArgs(workspace, prompt, options.get("name") or None, kind, options.get("model") or None,
                   options.get("effort") or None, options.get("mode") or None, options.get("cwd") or None)


def parse_send_args(rest: str) -> tuple[str, str] | str:
    try:
        first = _scan(rest, 0)
    except ParseError as exc:
        return str(exc)
    if first is None:
        return "Missing target. Usage: send <agent name|pane id> <prompt>"
    target, pos = first
    text = rest[pos:].strip()
    if not text:
        return "Missing prompt."
    return target, text


def resolve_workspace(workspaces: Sequence[Mapping], token: str) -> Mapping | None:
    """Match a workspace by id, then by label (case-insensitive, must be unique)."""
    for ws in workspaces:
        if ws.get("workspace_id") == token:
            return ws
    matches = [ws for ws in workspaces if (ws.get("label") or "").lower() == token.lower()]
    return matches[0] if len(matches) == 1 else None


def resolve_agent(agents: Sequence[Mapping], token: str) -> Mapping | None:
    """Match an agent by name, then by pane id."""
    for agent in agents:
        if agent.get("name") and agent["name"] == token:
            return agent
    for agent in agents:
        if agent.get("pane_id") == token:
            return agent
    return None


def workspace_cwd(panes: Sequence[Mapping]) -> str:
    """cwd prefill: first pane with a cwd (agent panes first). Trailing separators removed."""
    ordered = sorted(panes, key=lambda p: 0 if p.get("agent") else 1)
    for pane in ordered:
        cwd = pane.get("cwd") or pane.get("foreground_cwd")
        if cwd:
            stripped = cwd.rstrip("\\/")
            return stripped if not stripped.endswith(":") else stripped + "\\"
    return ""
