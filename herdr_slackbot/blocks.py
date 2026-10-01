"""Pure Block Kit builders and modal-state parsers.

Slack limits enforced here: section text <= 3000 chars, option text <= 75,
option value <= 150, <= 100 options per select, modal title <= 24, <= 50 blocks.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Iterable, Mapping, Sequence

from .agents import DEFAULT_PERMISSION_MODE, KIND_CLAUDE, KIND_CODEX, KIND_SPECS, KINDS, PERMISSION_MODES
from .dialog import KIND_PLAN, KIND_QUESTION_REVIEW, KIND_TRUST, KIND_UNKNOWN, Dialog, Option
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
PREVIEW_CUT_MARK = "…(earlier part omitted)"
PREVIEW_LOADING = "Loading…"
PREVIEW_EMPTY = "No messages yet"
PREVIEW_FAILED = "Couldn't load the conversation"
PREVIEW_BUSY = "Working — no answer yet"
PREVIEW_PICK = "Pick an agent to see the conversation here"
PREVIEW_WORKING = "Working — the current turn is not finished yet"
PREVIEW_BLOCKED = "Waiting for an answer on a dialog"
PREVIEW_OMITTED = "… earlier messages omitted"
PREVIEW_HEAD_CUT_MARK = "…(rest omitted)"
PREVIEW_USER_CHARS = 1500  # a prompt keeps its start, an answer its end (PREVIEW_CHARS)
PREVIEW_MAX_BLOCKS = 40  # conversation blocks in the send modal (Slack: <= 100 per view)
PREVIEW_MAX_TOTAL_CHARS = 12000
BLOCK_PREVIEW_PREFIX = "preview_"  # conversation blocks: preview_0, preview_1, ...
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
# blocked dialogs: action ids carry the option index / key id after the prefix (unique per block)
ACTION_DIALOG_PREFIX = "dlg:"
ACTION_DIALOG_OPTION = "dlg:opt:"
ACTION_DIALOG_TEXT = "dlg:text:"
ACTION_DIALOG_KEY = "dlg:key:"
ACTION_DIALOG_SCREEN = "dlg:screen"
DIALOG_TEXT_CALLBACK = "herdr_dialog_text"
BLOCK_DIALOG_OPTIONS = "dialog_options"
BLOCK_DIALOG_CONTROLS = "dialog_ctl"
BLOCK_DIALOG_TEXT = "dialog_text"
DIALOG_BODY_MAX = 1800
PLAN_MAX_CHARS = 2500
KEYPAD_TAIL_LINES = 15
KEYPAD_KEYS = (("1", "1"), ("2", "2"), ("3", "3"), ("4", "4"), ("up", "↑"), ("down", "↓"),
               ("enter", "Enter"), ("esc", "Esc"))

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
        "• When an agent is waiting for an answer, use the buttons in its thread (or reply there with text).",
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
    agents get a [Send] button (opens the send modal with that agent preselected)."""
    blocks: list = [
        {"type": "header", "text": plain(truncate(title, 150))},
        context(" · ".join(escape(p) for p in status_parts if p)),
        {"type": "actions", "block_id": "home_actions", "elements": [
            _button("➕ New Agent", ACTION_HOME_NEW, style="primary"),
            _button("📤 Send", ACTION_HOME_SEND),
            _button("🔄 Refresh", ACTION_HOME_REFRESH),
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
                    block["accessory"] = _button("Send", ACTION_HOME_SEND_AGENT,
                                                 (agent.get("name") or agent.get("pane_id") or "")[:OPTION_VALUE_MAX])
                rows.append(("agent", block))
        room = HOME_MAX_BLOCKS - len(blocks)
        if len(rows) > room:
            keep = rows[:room - 1]
            hidden = sum(1 for kind, _ in rows[room - 1:] if kind == "agent")
            rows = keep + [("more", context(f"{hidden} more agents (see the `list` command)"))]
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
    header = " · ".join(p for p in ("Last response", when, duration) if p)
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


def head_excerpt(text: str, limit: int) -> tuple[str, bool]:
    """The FIRST ~`limit` chars of `text`, cut at a line boundary when one is near. Returns
    (excerpt, cut); a fence left open is closed by `to_mrkdwn`."""
    if len(text) <= limit:
        return text, False
    end = text.rfind("\n", 0, limit)
    if end == -1 or limit - end > limit * 0.2:
        end = limit
    return text[:end], True


def relative_time(at: float | None, now: float) -> str:
    if not at:
        return ""
    ago = max(0.0, now - at)
    if ago < 60:
        return "just now"
    if ago < 3600:
        return f"{int(ago // 60)} min ago"
    return time.strftime("%m-%d %H:%M", time.localtime(at))


def _message_body(text: str, keep_tail: bool, limit: int) -> str:
    """mrkdwn of one conversation message within SECTION_MAX: answers keep their end (the
    conclusion), prompts their start; a cut is marked."""
    text = text.strip("\n")
    while True:
        excerpt, cut = tail_excerpt(text, limit) if keep_tail else head_excerpt(text, limit)
        body = to_mrkdwn(excerpt)
        if cut:
            body = PREVIEW_CUT_MARK + "\n" + body if keep_tail else body + "\n" + PREVIEW_HEAD_CUT_MARK
        if len(body) <= SECTION_MAX or limit < 200:
            return body[-SECTION_MAX:] if keep_tail else body[:SECTION_MAX]
        limit = int(limit * 0.8)  # escaping made it longer: take less


def conversation_blocks(turns: Sequence, agent_label: str, now: float, *, omitted: bool = False,
                        note: str | None = None) -> list:
    """The conversation for the send modal, oldest first (newest right above the Prompt input).
    Filled newest-first within PREVIEW_MAX_BLOCKS / PREVIEW_MAX_TOTAL_CHARS; older messages that
    do not fit become one line at the top. `turns` have `.role`, `.text`, `.at`; `omitted` = the
    source itself lacks older messages; `note` (working/blocked) goes below the newest message."""
    room = PREVIEW_MAX_BLOCKS - 1 - (1 if note else 0)  # one line kept for "earlier messages"
    chars = 0
    shown: list[list] = []
    for turn in reversed(turns):
        user = turn.role == "user"
        body = _message_body(turn.text, keep_tail=not user,
                             limit=PREVIEW_USER_CHARS if user else PREVIEW_CHARS)
        if shown and (2 * (len(shown) + 1) > room or chars + len(body) > PREVIEW_MAX_TOTAL_CHARS):
            break
        who = "👤 You" if user else f"🤖 {escape(agent_label)}"
        shown.append([context(" · ".join(p for p in (who, relative_time(turn.at, now)) if p)), section(body)])
        chars += len(body)
    hidden = len(turns) - len(shown)
    blocks: list = []
    if hidden:
        blocks.append(context(f"_… {hidden}{'+' if omitted else ''} earlier "
                              f"message{'s' if hidden != 1 or omitted else ''} not shown_"))
    elif omitted:
        blocks.append(context(f"_{PREVIEW_OMITTED}_"))
    for pair in reversed(shown):
        blocks.extend(pair)
    if note:
        blocks.append(context(f"_{escape(note)}_"))
    if not blocks:
        blocks.append(context(f"_{PREVIEW_EMPTY}_"))
    for i, block in enumerate(blocks):
        block["block_id"] = f"{BLOCK_PREVIEW_PREFIX}{i}"
    return blocks


def send_view(agents: Sequence[Mapping], workspace_labels: Mapping[str, str],
              initial_target: str | None = None, metadata: dict | None = None,
              preview: list | None = None, prompt: str = "") -> dict:
    """Send modal. The agent select dispatches (-> conversation preview between Agent and
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


# --- blocked dialogs (answered from Slack) -------------------------------------------------

def dialog_header(name: str, workspace_label: str) -> str:
    return f"⚠️ *{escape(name)}* · {escape(workspace_label)} is waiting for your answer"


def _dialog_value(token: str, choice) -> str:
    return json.dumps({"t": token, "o": choice})


def parse_dialog_value(value: str) -> tuple[str | None, object]:
    """(token, option index or key id) from a dialog button value."""
    try:
        data = json.loads(value or "")
        return str(data["t"]), data.get("o")
    except (ValueError, KeyError, TypeError):
        return None, None


def _code(lines: Sequence[str], limit: int) -> str:
    """Lines as a ``` block, keeping the tail when too long for `limit` characters."""
    text = defuse_fences(escape("\n".join(lines)))
    if len(text) > limit - 8:
        text = "…" + text[-(limit - 9):]
    return "```\n" + text + "\n```"


def _one_line(text: str) -> str:
    return " ↵ ".join(part.strip() for part in text.split("\n"))


def option_button_text(option: Option, multi: bool = False) -> str:
    box = ("☑ " if option.checked else "☐ ") if multi and option.checked is not None else ""
    number = f"{option.number}. " if option.number is not None else ""
    return truncate(f"{box}{number}{_one_line(option.label)}", OPTION_TEXT_MAX)


def _option_line(option: Option, multi: bool) -> str:
    box = ("☑ " if option.checked else "☐ ") if multi and option.checked is not None else ""
    number = f"*{option.number}.* " if option.number is not None else "• "
    line = f"{box}{number}{escape(_one_line(option.label))}"
    if option.description:
        line += f" — _{escape(truncate(option.description, 300))}_"
    return line


def _actions(block_id: str, elements: list) -> list:
    """Actions blocks of at most 25 elements (Slack's limit)."""
    return [{"type": "actions", "block_id": f"{block_id}{i // 25 or ''}", "elements": elements[i:i + 25]}
            for i in range(0, len(elements), 25)]


def dialog_blocks(name: str, workspace_label: str, dialog: Dialog | None, token: str, *,
                  plan_text: str | None = None, full_id: str | None = None,
                  screen_tail: Sequence[str] = (), note: str | None = None) -> list:
    """A blocked agent's dialog with one button per option, or a keypad when it was not parsed.
    Button values stay small (`{"t": token, "o": ...}`); the token names the pending record."""
    blocks: list = [section(dialog_header(name, workspace_label))]
    if note:
        blocks.append(context(escape(note)))
    if dialog is None or (dialog.kind == KIND_UNKNOWN and not dialog.options):
        if screen_tail:
            blocks.append(section(_code(list(screen_tail)[-KEYPAD_TAIL_LINES:], SECTION_MAX)))
        keys = [_button(label, f"{ACTION_DIALOG_KEY}{key}", _dialog_value(token, key)) for key, label in KEYPAD_KEYS]
        keys.append(_button("Show screen", ACTION_DIALOG_SCREEN, _dialog_value(token, "screen")))
        blocks.extend(_actions(BLOCK_DIALOG_OPTIONS, keys))
        return blocks
    if dialog.tabs:
        blocks.append(context(" · ".join(f"{'☒' if done else '☐'} {escape(tab)}" for tab, done in dialog.tabs)))
    parts = [f"*{escape(dialog.title)}*"] if dialog.title else []
    if dialog.kind == KIND_PLAN:
        text = plan_text if plan_text is not None else "\n".join(dialog.body)
        body, truncated = truncate_text(to_mrkdwn(text), PLAN_MAX_CHARS)
        if body.count("```") % 2 == 1:
            body += "\n```"
        blocks.append(section("\n".join(parts + [body or "_(empty plan)_"])))
        if truncated and full_id:
            blocks.append({"type": "actions", "block_id": "dialog_full", "elements": [
                _button("View full", ACTION_SHOW_FULL, full_id)]})
        parts = []
    elif dialog.body:
        if dialog.kind in (KIND_TRUST, KIND_QUESTION_REVIEW):
            body, _ = truncate_text(escape("\n".join(dialog.body)), DIALOG_BODY_MAX)
        else:
            body = _code(dialog.body, DIALOG_BODY_MAX)
        parts.append(body)
    if dialog.question:
        parts.append(f"*{escape(truncate(dialog.question, 1500))}*")
    for chunk in chunk_lines(parts):
        blocks.append(section(chunk))
    for chunk in chunk_lines([_option_line(o, dialog.multi_select) for o in dialog.options]):
        blocks.append(section(chunk))
    buttons = []
    for i, option in enumerate(dialog.options):
        action = ACTION_DIALOG_TEXT if option.free_text else ACTION_DIALOG_OPTION
        buttons.append(_button(option_button_text(option, dialog.multi_select), f"{action}{i}",
                               _dialog_value(token, i)))
    blocks.extend(_actions(BLOCK_DIALOG_OPTIONS, buttons))
    controls = []
    if dialog.multi_select:
        # Submit the question from its Submit row; `right` only when that row was not found.
        key = "submit" if dialog.submit_after is not None else "right"
        controls.append(_button("Next →", f"{ACTION_DIALOG_KEY}{key}", _dialog_value(token, key)))
    controls.append(_button("Esc", f"{ACTION_DIALOG_KEY}esc", _dialog_value(token, "esc")))
    controls.append(_button("Show screen", ACTION_DIALOG_SCREEN, _dialog_value(token, "screen")))
    blocks.extend(_actions(BLOCK_DIALOG_CONTROLS, controls))
    return blocks[:MAX_BLOCKS]


def dialog_summary(dialog: Dialog | None) -> str:
    """One short line naming the dialog (kept in the pending record for the closed message)."""
    if dialog is None:
        return "Waiting for input"
    text = " — ".join(p for p in (dialog.title, dialog.question) if p)
    return truncate(" ".join(text.split()), 300) or dialog.kind


def dialog_closed_blocks(name: str, workspace_label: str, summary: str, outcome: str) -> list:
    """The dialog message once it is no longer open: no buttons, just what happened."""
    return [section(f"💬 *{escape(name)}* · {escape(workspace_label)}"),
            context(escape(summary)),
            section(outcome)]


def without_actions(blocks: Sequence[Mapping], note: str) -> list:
    """A message's blocks with its buttons removed and a note appended."""
    out = [dict(b) for b in blocks if b.get("type") != "actions"]
    out.append(context(escape(note)))
    return out


def screen_blocks(lines: Sequence[str]) -> list:
    return [section(_code(list(lines), SECTION_MAX))] if lines else [section("_(the screen is empty)_")]


def dialog_unconfirmed_blocks(token: str) -> list:
    return [section("⚠️ Could not confirm the answer; check the screen."),
            {"type": "actions", "block_id": BLOCK_DIALOG_CONTROLS, "elements": [
                _button("Show screen", ACTION_DIALOG_SCREEN, _dialog_value(token, "screen"))]}]


def typed_notice(typed: str, sent: bool = False) -> str:
    """Says that text is appended to what a multi-select row already holds, and shows the row's text:
    before sending (modal) what it holds, after a thread reply what it holds now."""
    if sent:
        head = "✏️ Your reply was *added after* the text this row already held (nothing was cleared). Current text:"
    else:
        head = ("✏️ This row already holds typed text. What you send is *added after it* (nothing is cleared). "
                "Current text:")
    return head + "\n" + _code(typed.split("\n"), 1500)


def dialog_text_view(token: str, index: int, name: str, label: str, question: str = "",
                     where: Mapping | None = None, typed: str | None = None) -> dict:
    """Modal for a free-text option ("Type something." / "Tell Claude what to change"). `where`
    ({"c": channel, "m": message ts, "th": thread ts}) travels in the metadata so replies after the
    submission land in the dialog's thread. `typed`: text already in the row (appended to)."""
    shown = "Type something" if typed else _one_line(label)
    blocks = [section(f"*{escape(name)}* · {escape(truncate(shown, 200))}")]
    if question:
        blocks.append(context(escape(truncate(question, 500))))
    if typed:
        blocks.append(section(typed_notice(typed)))
    blocks.append(_input(BLOCK_DIALOG_TEXT, "Answer", _text_input(ACTION_VALUE, multiline=True)))
    return {
        "type": "modal",
        "callback_id": DIALOG_TEXT_CALLBACK,
        "title": plain("Answer agent"[:MODAL_TITLE_MAX]),
        "submit": plain("Send"),
        "close": plain("Cancel"),
        "private_metadata": json.dumps({"t": token, "o": index, **dict(where or {})})[:3000],
        "blocks": blocks,
    }


def notice_view(title: str, text: str) -> dict:
    return {"type": "modal", "title": plain(title[:MODAL_TITLE_MAX]), "close": plain("OK"),
            "blocks": [section(text)]}


def parse_dialog_text_view(view: Mapping) -> tuple[str | None, int | None, str, dict]:
    """(token, option index, text, where) from a submitted free-text modal."""
    try:
        meta = json.loads(view.get("private_metadata") or "{}")
        token, index = meta.get("t"), int(meta.get("o"))
    except (ValueError, TypeError, AttributeError):
        meta, token, index = {}, None, None
    where = {k: meta.get(k) for k in ("c", "m", "th") if isinstance(meta, dict) and meta.get(k)}
    values = ((view.get("state") or {}).get("values") or {})
    text = _value((values.get(BLOCK_DIALOG_TEXT) or {}).get(ACTION_VALUE) or {}) or ""
    return token, index, text, where


def result_blocks(header: str, context_parts: Sequence[str], body: str, result_id: str | None,
                  recap: str | None = None, limit: int = SECTION_MAX, markdown: bool = True) -> tuple[list, bool]:
    """Result message. Returns (blocks, truncated). A truncated body gets a [View full] button."""
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
            "elements": [{"type": "button", "action_id": ACTION_SHOW_FULL, "text": plain("View full"),
                          "value": result_id}],
        })
    return blocks, truncated
