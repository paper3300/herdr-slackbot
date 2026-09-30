"""Pure Block Kit builders and modal-state parsers.

Slack limits enforced here: section text <= 3000 chars, option text <= 75,
option value <= 150, <= 100 options per select, modal title <= 24, <= 50 blocks.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Iterable, Mapping, Sequence

from .agents import DEFAULT_PERMISSION_MODE, KIND_CLAUDE, KIND_CODEX, KIND_SPECS, KINDS, PERMISSION_MODES
from .naming import agent_display_name, agent_option_label, status_emoji, truncate
from .parser import truncate_text

SECTION_MAX = 3000
OPTION_TEXT_MAX = 75
OPTION_VALUE_MAX = 150
MAX_OPTIONS = 100
MAX_BLOCKS = 50
HOME_MAX_BLOCKS = 100  # Slack's limit for a Home tab view
MODAL_TITLE_MAX = 24

# callback / action / block ids
NEW_CALLBACK = "herdr_new"
SEND_CALLBACK = "herdr_send"
ACTION_NEW_WS = "new_ws"
ACTION_NEW_KIND = "new_kind"
ACTION_MUTE = "mute_toggle"
ACTION_SHOW_FULL = "show_full"
ACTION_HOME_NEW = "home_new"
ACTION_HOME_SEND = "home_send"
ACTION_HOME_REFRESH = "home_refresh"
ACTION_HOME_SEND_AGENT = "home_send_agent"
HOME_ACTIONS = (ACTION_HOME_NEW, ACTION_HOME_SEND, ACTION_HOME_REFRESH, ACTION_HOME_SEND_AGENT)
SENDABLE_STATUSES = ("idle", "done")
ACTION_VALUE = "value"
ACTION_SEND_TARGET = "send_target"  # agent select in the send modal (dispatch_action -> preview)
BLOCK_PREVIEW_HEAD = "preview_head"
BLOCK_PREVIEW_BODY = "preview_body"
PREVIEW_CHARS = 2500
PREVIEW_CUT_MARK = "…(앞부분 생략)"
PREVIEW_LOADING = "불러오는 중…"
PREVIEW_EMPTY = "아직 응답이 없습니다"
PREVIEW_FAILED = "응답을 불러오지 못했습니다"
PREVIEW_BUSY = "작업 중입니다 — 아직 응답이 없습니다"
PREVIEW_PICK = "에이전트를 고르면 마지막 응답이 여기에 표시됩니다"
BLOCK_WS = "ws"
BLOCK_KIND = "kind"
BLOCK_PERM = "perm"
BLOCK_NAME = "name"
BLOCK_PROMPT = "prompt"
BLOCK_TARGET = "target"
BLOCK_THREAD_CTL = "thread_ctl"
BLOCK_CWD_PREFIX = "cwd:"
BLOCK_MODEL_PREFIX = "model:"
BLOCK_EFFORT_PREFIX = "effort:"

PERMISSION_LABELS = {
    "manual": "manual (ask for everything)",
    "acceptEdits": "acceptEdits",
    "auto": "auto",
    "plan": "plan (read-only planning)",
}
CLAUDE_MODELS = (("opus", "Opus"), ("sonnet", "Sonnet"), ("haiku", "Haiku"))


# --- text helpers ------------------------------------------------------------

def escape(text: str) -> str:
    """Escape the three characters Slack mrkdwn requires escaped."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_BOLD_UNDERSCORE_RE = re.compile(r"__(.+?)__")
_LINK_RE = re.compile(r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)")
_BULLET_RE = re.compile(r"^(\s*)[-*]\s+")


def to_mrkdwn(markdown: str) -> str:
    """Best-effort conversion of agent Markdown to Slack mrkdwn (code fences left as-is)."""
    out: list[str] = []
    in_fence = False
    for line in escape(markdown).split("\n"):
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            out.append(line)
            continue
        if in_fence:
            out.append(line)
            continue
        m = _HEADING_RE.match(line)
        if m:
            line = f"*{m.group(2)}*"
        else:
            line = _BULLET_RE.sub(lambda mm: mm.group(1) + "• ", line)
            line = _BOLD_RE.sub(r"*\1*", line)
            line = _BOLD_UNDERSCORE_RE.sub(r"*\1*", line)
        line = _LINK_RE.sub(r"<\2|\1>", line)
        out.append(line)
    if in_fence:
        out.append("```")  # a truncated body may cut a fence open
    return "\n".join(out)


def plain(text: str, emoji: bool = True) -> dict:
    return {"type": "plain_text", "text": text, "emoji": emoji}


def mrkdwn(text: str) -> dict:
    # verbatim: no automatic parsing of bare @here/@channel/#channel/URLs in (agent-controlled) text;
    # our own explicit markup (*bold*, <url|label>) still renders.
    return {"type": "mrkdwn", "text": text, "verbatim": True}


def defuse_fences(text: str) -> str:
    """Raw text shown inside our own ``` block must not contain a fence of its own."""
    return text.replace("```", "`\u200b``")


def section(text: str, block_id: str | None = None) -> dict:
    block = {"type": "section", "text": mrkdwn(text[:SECTION_MAX])}
    if block_id:
        block["block_id"] = block_id
    return block


def context(*texts: str) -> dict:
    return {"type": "context", "elements": [mrkdwn(t[:SECTION_MAX]) for t in texts if t][:10]}


def option(text: str, value: str) -> dict:
    return {"text": plain(truncate(text, OPTION_TEXT_MAX)), "value": value[:OPTION_VALUE_MAX]}


def chunk_lines(lines: Iterable[str], limit: int = SECTION_MAX) -> list[str]:
    """Join lines into chunks no longer than `limit` characters."""
    chunks: list[str] = []
    current = ""
    for line in lines:
        line = line[:limit]
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > limit:
            chunks.append(current)
            current = line
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


# --- usage / simple notices --------------------------------------------------------

def usage_text(cmd: str) -> str:
    return "\n".join([
        f"*Herdr bridge commands* (`{cmd}`)",
        f"• `{cmd} list`: list agents",
        f"• `{cmd} new`: start a new agent (form)",
        f"• `{cmd} new <workspace> [name=..] [kind=claude|codex] [model=..] [effort=..] [mode=..] [cwd=..] <prompt>`",
        f"• `{cmd} send`: send a prompt to an agent (form)",
        f"• `{cmd} send <agent name|pane id> <prompt>`",
        f"• `{cmd} status`: bridge status",
        "• Reply in an agent's thread to send it a prompt.",
    ])


def usage_blocks(cmd: str) -> list:
    return [section(usage_text(cmd))]


def notice_blocks(text: str) -> list:
    return [section(text)]


# --- agent list -------------------------------------------------------------------

def agent_line(agent: Mapping) -> str:
    status = agent.get("agent_status") or "unknown"
    title = (agent.get("terminal_title_stripped") or agent.get("terminal_title") or "").strip()
    kind = agent.get("agent") or "?"
    parts = [f"{status_emoji(status)} *{escape(agent_display_name(agent))}*", escape(kind), status]
    if title:
        parts.append(escape(truncate(title, 60)))
    return " · ".join(parts)


def agent_list_blocks(agents: Sequence[Mapping], workspace_labels: Mapping[str, str]) -> list:
    if not agents:
        return [section("No agents are running in Herdr.")]
    by_ws: dict[str, list[Mapping]] = {}
    for agent in agents:
        by_ws.setdefault(agent.get("workspace_id") or "?", []).append(agent)
    blocks: list = [section(f"*Herdr agents* ({len(agents)})")]
    for ws_id, items in by_ws.items():
        label = escape(workspace_labels.get(ws_id, ws_id))
        lines = [f"*{label}*"] + [agent_line(a) for a in items]
        for chunk in chunk_lines(lines):
            blocks.append(section(chunk))
    if len(blocks) > MAX_BLOCKS:
        blocks = blocks[: MAX_BLOCKS - 1] + [context("…more agents not shown")]
    return blocks


# --- App Home ---------------------------------------------------------------------------

def _button(text: str, action_id: str, value: str | None = None, style: str | None = None) -> dict:
    btn = {"type": "button", "text": plain(text), "action_id": action_id}
    if value is not None:
        btn["value"] = value[:2000]
    if style:
        btn["style"] = style
    return btn


def home_view(title: str, status_parts: Sequence[str], agents: Sequence[Mapping] | None,
              workspace_labels: Mapping[str, str], error: str | None = None) -> dict:
    """The App Home tab: status header, action buttons, agents grouped by workspace. Idle/done
    agents get a [보내기] button (opens the send modal with that agent preselected)."""
    blocks: list = [
        {"type": "header", "text": plain(truncate(title, 150))},
        context(" · ".join(escape(p) for p in status_parts if p)),
        {"type": "actions", "block_id": "home_actions", "elements": [
            _button("➕ 새 에이전트", ACTION_HOME_NEW, style="primary"),
            _button("📤 보내기", ACTION_HOME_SEND),
            _button("🔄 새로고침", ACTION_HOME_REFRESH),
        ]},
        {"type": "divider"},
    ]
    if error:
        blocks.append(section(f"❌ Herdr error: {escape(error)}"))
    elif not agents:
        blocks.append(section("No agents are running in Herdr."))
    else:
        rows: list = []
        by_ws: dict[str, list[Mapping]] = {}
        for agent in agents:
            by_ws.setdefault(agent.get("workspace_id") or "?", []).append(agent)
        for ws_id, items in by_ws.items():
            rows.append(("ws", section(f"*{escape(workspace_labels.get(ws_id, ws_id))}*")))
            for agent in items:
                block = section(truncate(agent_line(agent), SECTION_MAX))
                if (agent.get("agent_status") or "") in SENDABLE_STATUSES:
                    block["accessory"] = _button("보내기", ACTION_HOME_SEND_AGENT,
                                                 (agent.get("name") or agent.get("pane_id") or "")[:OPTION_VALUE_MAX])
                rows.append(("agent", block))
        room = HOME_MAX_BLOCKS - len(blocks)
        if len(rows) > room:
            keep = rows[:room - 1]
            hidden = sum(1 for kind, _ in rows[room - 1:] if kind == "agent")
            rows = keep + [("more", context(f"외 {hidden}개 에이전트 (목록: `list` 명령)"))]
        blocks.extend(block for _, block in rows)
    return {"type": "home", "blocks": blocks}


# --- status ---------------------------------------------------------------------------

def status_blocks(lines: Sequence[str]) -> list:
    return [section("*Herdr bridge status*\n" + "\n".join(lines))]


# --- modals ---------------------------------------------------------------------------

@dataclass
class NewModalState:
    workspace_id: str | None = None
    cwd: str = ""
    kind: str = KIND_CLAUDE
    model: str | None = None
    effort: str | None = None
    permission_mode: str = DEFAULT_PERMISSION_MODE
    name: str = ""
    prompt: str = ""
    metadata: dict = field(default_factory=dict)


def _select(action_id: str, options: list[dict], initial_value: str | None = None,
            placeholder: str = "Select") -> dict:
    element = {"type": "static_select", "action_id": action_id,
               "placeholder": plain(placeholder), "options": options[:MAX_OPTIONS]}
    initial = next((o for o in element["options"] if o["value"] == initial_value), None)
    if initial is not None:
        element["initial_option"] = initial
    return element


def _input(block_id: str, label: str, element: dict, optional: bool = False,
           dispatch: bool = False, hint: str | None = None) -> dict:
    block = {"type": "input", "block_id": block_id, "label": plain(label), "element": element,
             "optional": optional}
    if dispatch:
        block["dispatch_action"] = True
    if hint:
        block["hint"] = plain(hint)
    return block


def _text_input(action_id: str, initial: str = "", multiline: bool = False, max_length: int | None = None,
                placeholder: str | None = None) -> dict:
    element = {"type": "plain_text_input", "action_id": action_id, "multiline": multiline}
    if initial:
        element["initial_value"] = initial
    if max_length:
        element["max_length"] = max_length
    if placeholder:
        element["placeholder"] = plain(placeholder)
    return element


def model_options(kind: str, codex_models: Sequence[tuple[str, str]] = ()) -> list[dict]:
    if kind == KIND_CODEX:
        return [option(label, slug) for slug, label in codex_models][:MAX_OPTIONS]
    return [option(label, value) for value, label in CLAUDE_MODELS]


def new_agent_view(state: NewModalState, workspaces: Sequence[Mapping],
                   codex_models: Sequence[tuple[str, str]] = (), codex_default: str | None = None) -> dict:
    kind = state.kind if state.kind in KINDS else KIND_CLAUDE
    spec = KIND_SPECS[kind]
    ws_options = [option(w.get("label") or w["workspace_id"], w["workspace_id"]) for w in workspaces]
    default_model = state.model or (codex_default if kind == KIND_CODEX else spec.default_model)
    blocks = [
        _input(BLOCK_WS, "Workspace", _select(ACTION_NEW_WS, ws_options, state.workspace_id), dispatch=True),
        _input(f"{BLOCK_CWD_PREFIX}{state.workspace_id or ''}", "Working directory",
               _text_input(ACTION_VALUE, state.cwd, placeholder="blank = workspace default"), optional=True),
        _input(BLOCK_KIND, "Agent", _select(ACTION_NEW_KIND, [option(k, k) for k in KINDS], kind), dispatch=True),
        _input(f"{BLOCK_MODEL_PREFIX}{kind}", "Model",
               _select(ACTION_VALUE, model_options(kind, codex_models), default_model)),
        _input(f"{BLOCK_EFFORT_PREFIX}{kind}", "Effort",
               _select(ACTION_VALUE, [option(e, e) for e in spec.efforts], state.effort or spec.default_effort)),
    ]
    if spec.supports_permission_mode:
        blocks.append(_input(BLOCK_PERM, "Permission mode", _select(
            ACTION_VALUE, [option(PERMISSION_LABELS[m], m) for m in PERMISSION_MODES],
            state.permission_mode or DEFAULT_PERMISSION_MODE)))
    blocks += [
        _input(BLOCK_NAME, "Name", _text_input(ACTION_VALUE, state.name, max_length=32,
                                               placeholder="blank = slack-<N>"),
               optional=True, hint="lowercase letter first, then a-z 0-9 - _ (max 32)"),
        _input(BLOCK_PROMPT, "Prompt", _text_input(ACTION_VALUE, state.prompt, multiline=True)),
    ]
    return {
        "type": "modal",
        "callback_id": NEW_CALLBACK,
        "title": plain("New Herdr agent"[:MODAL_TITLE_MAX]),
        "submit": plain("Start"),
        "close": plain("Cancel"),
        "private_metadata": json.dumps(state.metadata)[:3000],
        "blocks": blocks,
    }


def loading_view(title: str, callback_id: str, text: str = "⏳ Loading Herdr data…") -> dict:
    """Placeholder opened immediately with the trigger id; replaced via views.update."""
    return {
        "type": "modal",
        "callback_id": callback_id,
        "title": plain(title[:MODAL_TITLE_MAX]),
        "close": plain("Cancel"),
        "blocks": [section(text)],
    }


def _value(entry: Mapping) -> str | None:
    if entry.get("selected_option"):
        return entry["selected_option"].get("value")
    value = entry.get("value")
    return value if value is None else str(value)


def parse_new_view_state(values: Mapping) -> NewModalState:
    """Read a `herdr_new` view's `state.values` (works for submissions and block_actions)."""
    st = NewModalState(kind="")
    for block_id, actions in (values or {}).items():
        for action_id, entry in actions.items():
            v = _value(entry)
            if block_id == BLOCK_WS:
                st.workspace_id = v
            elif block_id.startswith(BLOCK_CWD_PREFIX):
                st.cwd = (v or "").strip()
            elif block_id == BLOCK_KIND:
                st.kind = v or ""
            elif block_id.startswith(BLOCK_MODEL_PREFIX):
                st.model = v
            elif block_id.startswith(BLOCK_EFFORT_PREFIX):
                st.effort = v
            elif block_id == BLOCK_PERM:
                st.permission_mode = v or DEFAULT_PERMISSION_MODE
            elif block_id == BLOCK_NAME:
                st.name = (v or "").strip()
            elif block_id == BLOCK_PROMPT:
                st.prompt = v or ""
    st.kind = st.kind or KIND_CLAUDE
    return st


def tail_excerpt(text: str, limit: int = PREVIEW_CHARS) -> tuple[str, bool]:
    """The LAST ~`limit` chars of `text`, cut at a line boundary. If the cut lands inside a code
    fence, the excerpt reopens it (the caller's conversion closes it). Returns (excerpt, cut)."""
    if len(text) <= limit:
        return text, False
    start = len(text) - limit
    nl = text.find("\n", start)
    if nl != -1 and nl - start < limit * 0.2:
        start = nl + 1
    head, tail = text[:start], text[start:]
    fences = sum(1 for line in head.split("\n") if line.lstrip().startswith("```"))
    if fences % 2 == 1:
        tail = "```\n" + tail  # we are inside a code block: reopen it
    return tail, True


def last_response_blocks(text: str | None, when: str = "", duration: str = "", markdown: bool = True,
                         note: str | None = None) -> list:
    """Preview of an agent's last response for the send modal: a small context header and the
    text (tail-truncated to fit Slack's 3000-char section limit), or one italic note line."""
    if note or not (text or "").strip():
        return [context(f"_{escape(note or PREVIEW_EMPTY)}_") | {"block_id": BLOCK_PREVIEW_HEAD}]
    header = " · ".join(p for p in ("마지막 응답", when, duration) if p)
    limit = PREVIEW_CHARS
    while True:
        excerpt, cut = tail_excerpt(text.strip("\n"), limit)
        body = to_mrkdwn(excerpt) if markdown else "```\n" + defuse_fences(escape(excerpt)) + "\n```"
        if cut:
            body = PREVIEW_CUT_MARK + "\n" + body
        if len(body) <= SECTION_MAX or limit < 200:
            break
        limit = int(limit * 0.8)  # escaping made it longer: take less
    return [context(escape(header)) | {"block_id": BLOCK_PREVIEW_HEAD},
            section(body[-SECTION_MAX:], block_id=BLOCK_PREVIEW_BODY)]


def send_view(agents: Sequence[Mapping], workspace_labels: Mapping[str, str],
              initial_target: str | None = None, metadata: dict | None = None,
              preview: list | None = None, prompt: str = "") -> dict:
    """Send modal. The agent select dispatches (-> last-response preview between Agent and
    Prompt). Block ids stay fixed so Slack keeps what the user selected/typed across updates."""
    options = [option(agent_option_label(a, workspace_labels), a.get("name") or a["pane_id"]) for a in agents]
    if initial_target and all(o["value"] != initial_target for o in options[:MAX_OPTIONS]):
        # The select holds at most MAX_OPTIONS: never cut the preselected agent (it takes the last slot).
        chosen = [o for o in options if o["value"] == initial_target]
        if chosen:
            options = options[:MAX_OPTIONS - 1] + chosen
    return {
        "type": "modal",
        "callback_id": SEND_CALLBACK,
        "title": plain("Send to agent"),
        "submit": plain("Send"),
        "close": plain("Cancel"),
        "private_metadata": json.dumps(metadata or {})[:3000],
        "blocks": [
            _input(BLOCK_TARGET, "Agent", _select(ACTION_SEND_TARGET, options, initial_target, "Choose an agent"),
                   dispatch=True),
            *(preview if preview is not None else [context(f"_{PREVIEW_PICK}_") | {"block_id": BLOCK_PREVIEW_HEAD}]),
            _input(BLOCK_PROMPT, "Prompt", _text_input(ACTION_VALUE, initial=prompt, multiline=True)),
        ],
    }


def parse_send_view_state(values: Mapping) -> tuple[str | None, str]:
    target_block = (values or {}).get(BLOCK_TARGET) or {}
    target = _value(target_block.get(ACTION_SEND_TARGET) or target_block.get(ACTION_VALUE) or {})
    prompt = _value(((values or {}).get(BLOCK_PROMPT) or {}).get(ACTION_VALUE) or {}) or ""
    return target, prompt


# --- threads ---------------------------------------------------------------------------

def mute_actions(session: str, muted: bool) -> dict:
    return {
        "type": "actions",
        "block_id": BLOCK_THREAD_CTL,
        "elements": [{
            "type": "button",
            "action_id": ACTION_MUTE,
            "text": plain("🔔 Unmute" if muted else "🔕 Mute"),
            "value": json.dumps({"s": session, "m": not muted})[:2000],
        }],
    }


def thread_root_blocks(title: str, lines: Sequence[str], session: str, muted: bool) -> list:
    text = "\n".join([title, *lines])
    return [section(text), mute_actions(session, muted)]


def with_mute_button(blocks: Sequence[Mapping], session: str, muted: bool) -> list:
    """Replace (or append) the thread-control block with the given mute state."""
    out = [dict(b) for b in blocks if b.get("block_id") != BLOCK_THREAD_CTL]
    out.append(mute_actions(session, muted))
    return out


def started_blocks() -> list:
    return [context("⏳ started (working)")]


def sent_blocks(prompt: str) -> list:
    return [section("📨 " + escape(truncate(" ".join(prompt.split()), 500)))]


def blocked_blocks(name: str, workspace_label: str) -> list:
    return [section(f"⚠️ *{escape(name)}* · {escape(workspace_label)} needs confirmation on PC")]


def result_blocks(header: str, context_parts: Sequence[str], body: str, result_id: str | None,
                  recap: str | None = None, limit: int = SECTION_MAX, markdown: bool = True) -> tuple[list, bool]:
    """Result message. Returns (blocks, truncated). A truncated body gets a [전체 보기] button."""
    converted = to_mrkdwn(body) if markdown else "```\n" + defuse_fences(escape(body)) + "\n```"
    text, truncated = truncate_text(converted, min(limit, SECTION_MAX))
    if truncated and text.count("```") % 2 == 1:
        text, _ = truncate_text(converted, min(limit, SECTION_MAX) - 4)
        text += "\n```"
    blocks = [section(header)]
    ctx = [p for p in context_parts if p]
    if ctx:
        blocks.append(context(*ctx))
    if recap:
        blocks.append(context("※ " + escape(truncate(recap, 500))))
    blocks.append(section(text or "_(empty response)_"))
    if truncated and result_id:
        blocks.append({
            "type": "actions",
            "block_id": "result_ctl",
            "elements": [{"type": "button", "action_id": ACTION_SHOW_FULL, "text": plain("전체 보기"),
                          "value": result_id}],
        })
    return blocks, truncated
