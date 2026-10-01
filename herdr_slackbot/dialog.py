"""Parse the interactive dialog at the bottom of an agent screen (pure, no I/O).

Herdr reports `blocked` while Claude Code / Codex wait for an answer: a permission prompt,
AskUserQuestion (single / multi-select / several questions / free text), plan approval,
the folder-trust screen at startup, a Codex command approval. `parse_dialog` turns the
`visible` screen text into a `Dialog` the Slack side can render as buttons, and
`keys_for` / `text_keys` / `submit_keys` give the key presses that answer it.

Screen facts (Herdr 0.8.2, Claude Code 2.1.x, Codex 0.159):
- The dialog sits below the last full-width `────` rule above its options (Claude); Codex
  draws no rule, so its dialog starts after a run of blank lines. A plan approval shows the
  plan above that rule, between `╌╌╌` rules.
- Numbered options (`1. Yes`) are chosen by pressing the digit; unnumbered menus (Claude's
  folder trust) need `up`/`down` from the cursor, then `enter`.
- The cursor marker is `❯` (Claude) or `›` (Codex); every real dialog shows one. Option text
  may wrap onto following lines indented to the option's text column; AskUserQuestion / plan
  options have an indented description line under the label instead.
- Multi-select options carry `[ ]` / `[✔]`; a `Submit` row follows the checkbox options.
  There a digit only toggles its box (the cursor stays), `enter` on the Submit row submits
  the question, and the "Type something" row takes typed text once the cursor is on it
  (typing checks it; `enter` there would uncheck it again).
- Question tabs look like `←  ☐ Color  ☒ Fruits  ✔ Submit  →` (☒ = answered).
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

KIND_PERMISSION = "permission"
KIND_QUESTION = "question"
KIND_QUESTION_REVIEW = "question_review"
KIND_PLAN = "plan"
KIND_TRUST = "trust"
KIND_CODEX_APPROVAL = "codex_approval"
KIND_UNKNOWN = "unknown"

REGION_MAX_LINES = 40  # non-blank lines looked at above the options when no rule marks the dialog
RULE_LOOKBACK = 60  # how far above the options the dialog's top rule may be
FOOTER_MAX_LINES = 8  # non-blank lines allowed below the options (hints, footer)
OPTION_GAP_MAX = 12  # lines allowed between two consecutive numbered options
SUBMIT_SCAN_MAX = 6  # lines below the last option searched for a multi-select Submit row
SUBMIT = "submit"  # row id of a multi-select question's Submit row

CURSORS = "❯›"
_RULE_RE = re.compile(r"^\s*─{8,}\s*$")
_DASH_RULE_RE = re.compile(r"^\s*╌{8,}\s*$")
_OPTION_RE = re.compile(r"^(?P<indent>\s*)(?P<cursor>[❯›]\s*)?(?P<num>\d{1,2})\.\s+(?P<text>\S.*)$")
_CURSOR_RE = re.compile(r"^(?P<indent>\s*)(?P<cursor>[❯›])\s+(?P<text>\S.*)$")
_CHECKBOX_RE = re.compile(r"^\[(?P<mark>[ ✔✓xX×])\]\s*(?P<label>.*)$", re.S)
_TAB_ITEM_RE = re.compile(r"([☐☒☑✔])\s+([^☐☒☑✔←→]+)")
_PLAN_FILE_RE = re.compile(r"(\S*[\\/]\.claude[\\/]plans[\\/]\S+?\.md)\s*$")
_FREE_TEXT_PREFIXES = ("type something", "tell claude what to change")
_CHAT_PREFIXES = ("chat about this",)


@dataclass(frozen=True)
class Option:
    number: int | None
    label: str
    description: str = ""
    checked: bool | None = None  # multi-select state; None for plain options
    free_text: bool = False  # choosing it opens a text input ("Type something.")
    chat: bool = False  # "Chat about this"
    cursor: bool = False


@dataclass(frozen=True)
class Dialog:
    kind: str
    title: str
    body: tuple[str, ...]
    question: str
    options: tuple[Option, ...]
    multi_select: bool = False
    tabs: tuple[tuple[str, bool], ...] = ()  # (question header, answered)
    plan_file: str | None = None
    fingerprint: str = ""
    submit_after: int | None = None  # multi-select: index of the option the Submit row follows
    cursor_on_submit: bool = False

    def free_text_option(self) -> Option | None:
        return next((o for o in self.options if o.free_text), None)


# --- helpers -------------------------------------------------------------------------------

def _is_rule(line: str) -> bool:
    return bool(_RULE_RE.match(line))


def _has_cursor(line: str) -> bool:
    return line.lstrip()[:1] in CURSORS and bool(line.strip())


def _width(lines: list[str]) -> int:
    """Screen width estimate: full-width rules span it; else the longest line."""
    rules = [len(line.rstrip()) for line in lines if _is_rule(line)]
    return max(rules) if rules else max((len(line.rstrip()) for line in lines), default=80)


def _wrapped(prev: str, nxt: str, width: int) -> bool:
    """Did `prev` wrap onto `nxt`? True when the next word would not have fit on `prev`."""
    words = nxt.split()
    return bool(words) and len(prev.rstrip()) + 1 + len(words[0]) > width - 1


def _uncursor(line: str) -> str:
    """The line with a leading cursor marker blanked out (cursor moves are not changes)."""
    stripped = line.lstrip()
    if stripped[:1] in CURSORS:
        return line[: len(line) - len(stripped)] + " " + stripped[1:]
    return line


def _dedent(lines: list[str]) -> tuple[str, ...]:
    """Strip common indentation, collapse blank runs, trim blank ends."""
    body = [line.rstrip() for line in lines]
    indents = [len(line) - len(line.lstrip()) for line in body if line.strip()]
    cut = min(indents, default=0)
    out: list[str] = []
    for line in body:
        text = line[cut:] if line.strip() else ""
        if not text and (not out or not out[-1]):
            continue
        out.append(text)
    while out and not out[-1]:
        out.pop()
    return tuple(out)


def _join(parts: list[str]) -> str:
    return " ".join(p.strip() for p in parts if p.strip())


def tail_lines(screen: str, n: int = 40) -> list[str]:
    """The last `n` non-blank lines of a screen (for [Show screen] / the keypad)."""
    lines = [line.rstrip() for line in (screen or "").replace("\r\n", "\n").split("\n")]
    return [line for line in lines if line.strip()][-n:]


def screen_fingerprint(screen: str, n: int = 15) -> str:
    """Fingerprint of a screen tail (used when no dialog could be parsed)."""
    return hashlib.sha1("\n".join(tail_lines(screen, n)).encode("utf-8")).hexdigest()


# --- option blocks ---------------------------------------------------------------------------

@dataclass
class _RawOption:
    index: int  # line index
    number: int | None
    text: str
    text_col: int
    cursor: bool
    extra: list  # following continuation lines


def _numbered_options(lines: list[str]) -> list[_RawOption] | None:
    """The bottom-most run of options numbered 1..N (same digit column), or None."""
    matches = [(i, m) for i, m in ((i, _OPTION_RE.match(line)) for i, line in enumerate(lines)) if m]
    if not matches:
        return None
    last_i, last_m = matches[-1]
    col = last_m.start("num")
    chain = [(last_i, last_m)]
    want = int(last_m.group("num")) - 1
    by_line = dict(matches)
    cur = last_i
    while want >= 1:
        found = None
        for j in range(cur - 1, max(-1, cur - 1 - OPTION_GAP_MAX), -1):
            m = by_line.get(j)
            if m is not None:
                if int(m.group("num")) == want and m.start("num") == col:
                    found = j
                break
        if found is None:
            return None
        chain.append((found, by_line[found]))
        cur, want = found, want - 1
    chain.reverse()
    return [_RawOption(i, int(m.group("num")), m.group("text").rstrip(), m.start("text"),
                       bool(m.group("cursor")), []) for i, m in chain]


def _cursor_menu(lines: list[str]) -> list[_RawOption] | None:
    """An unnumbered menu (`❯ No, exit` / `  Yes, I trust this folder`) near the bottom."""
    nonblank = [i for i, line in enumerate(lines) if line.strip()]
    for i in reversed(nonblank[-FOOTER_MAX_LINES - 1:]):
        m = _CURSOR_RE.match(lines[i])
        if not m or _OPTION_RE.match(lines[i]):
            continue
        col = m.start("text")

        def sibling(j: int) -> bool:
            line = lines[j]
            return bool(line.strip()) and not _is_rule(line) and len(line) - len(line.lstrip()) == col

        top = i
        while top - 1 >= 0 and sibling(top - 1):
            top -= 1
        bottom = i
        while bottom + 1 < len(lines) and sibling(bottom + 1):
            bottom += 1
        if bottom == top:
            return None
        return [_RawOption(j, None, (m.group("text") if j == i else lines[j].strip()).rstrip(), col, j == i, [])
                for j in range(top, bottom + 1)]
    return None


@dataclass
class _Block:
    raws: list
    end: int  # index after the options block
    submit_after: int | None = None
    cursor_on_submit: bool = False
    has_cursor: bool = False


def _attach_continuations(lines: list[str], raws: list[_RawOption]) -> _Block:
    """Give each option the indented lines that follow it; find a multi-select `Submit` row."""
    block = _Block(raws, raws[-1].index + 1)
    for k, raw in enumerate(raws):
        stop = raws[k + 1].index if k + 1 < len(raws) else len(lines)
        for j in range(raw.index + 1, stop):
            line = lines[j]
            if not line.strip():
                if k + 1 == len(raws):
                    break  # the last option's block ends at a blank line
                continue
            if _is_rule(line):
                continue
            plain = _uncursor(line)
            if len(plain) - len(plain.lstrip()) < raw.text_col:
                break
            if plain.strip() == "Submit":
                block.submit_after = k
                block.cursor_on_submit = _has_cursor(line)
            else:
                raw.extra.append(line)
            block.end = j + 1
    if block.submit_after is None:
        # A line drawn left of the text column (e.g. typed text) ends the continuation scan
        # above; the Submit row can still be below it: look for it by its text.
        stop = min(len(lines), raws[-1].index + 1 + SUBMIT_SCAN_MAX)
        starts = {r.index for r in raws}
        for j in range(raws[0].index + 1, stop):
            if j in starts or _uncursor(lines[j]).strip() != "Submit":
                continue
            block.submit_after = max(k for k, r in enumerate(raws) if r.index < j)
            block.cursor_on_submit = _has_cursor(lines[j])
            block.end = max(block.end, j + 1)
            break
    block.has_cursor = any(r.cursor for r in raws) or block.cursor_on_submit
    return block


def _typed_row(raw: _RawOption) -> Option:
    """A multi-select "Type something" row after text was typed into it: the row shows the text
    (continuation lines are its further lines, at the description indent)."""
    text = "\n".join([raw.text.strip()] + [line.strip() for line in raw.extra])
    m = _CHECKBOX_RE.match(text)
    checked = m.group("mark") != " " if m else None
    label = m.group("label").strip() if m else text
    return Option(raw.number, label, "", checked, free_text=True, cursor=raw.cursor)


def _build_option(raw: _RawOption, prev_line: str, width: int) -> Option:
    label_parts, desc_parts = [raw.text], []
    prev = prev_line
    for line in raw.extra:
        if _wrapped(prev, line, width):
            (desc_parts if desc_parts else label_parts).append(line)
        else:
            desc_parts.append(line)
        prev = line
    label = _join(label_parts)
    checked = None
    m = _CHECKBOX_RE.match(label)
    if m:
        checked = m.group("mark") != " "
        label = m.group("label").strip()
    low = label.lower()
    return Option(raw.number, label, _join(desc_parts), checked,
                  free_text=low.startswith(_FREE_TEXT_PREFIXES), chat=low.startswith(_CHAT_PREFIXES),
                  cursor=raw.cursor)


def _options_block(lines: list[str]) -> _Block | None:
    """The dialog's options: the numbered run or the unnumbered menu, whichever ends lower on the
    screen, that shows the cursor and is followed by no more than a footer."""
    candidates = []
    for raws in (_numbered_options(lines), _cursor_menu(lines)):
        if not raws:
            continue
        block = _attach_continuations(lines, raws)
        if not block.has_cursor:
            continue  # a numbered list in the transcript, not a dialog
        if sum(1 for line in lines[block.end:] if line.strip()) > FOOTER_MAX_LINES:
            continue  # options followed by more content
        candidates.append(block)
    return max(candidates, key=lambda b: b.end, default=None)


def _region_start(lines: list[str], first: int) -> int:
    """Index of the first line of the dialog (just below its top rule)."""
    for j in range(first - 1, max(-1, first - 1 - RULE_LOOKBACK), -1):
        if _is_rule(lines[j]):
            return j + 1
    blanks = seen = 0
    j = first - 1
    while j >= 0:
        if lines[j].strip():
            blanks = 0
            seen += 1
            if seen > REGION_MAX_LINES:
                return j + 1
        else:
            blanks += 1
            if blanks >= 3:
                return j + blanks
        j -= 1
    return 0


def _parse_tabs(line: str) -> tuple[tuple[str, bool], ...] | None:
    stripped = line.strip()
    if not stripped or stripped[0] not in "←☐☒☑":
        return None
    items = [(name.strip(), mark != "☐") for mark, name in _TAB_ITEM_RE.findall(stripped)]
    tabs = tuple((name, done) for name, done in items if name and name != "Submit")
    return tabs or None


def _plan_excerpt(above: list[str]) -> tuple[str, ...]:
    """The plan text between the last two `╌╌╌` rules above the approval question."""
    rules = [i for i, line in enumerate(above) if _DASH_RULE_RE.match(line)]
    if len(rules) < 2:
        return ()
    return _dedent(above[rules[-2] + 1: rules[-1]])


def _fingerprint(kind: str, title: str, body: tuple[str, ...], plan_file: str | None, lines: list[str]) -> str:
    """Everything the Slack message shows (title, body incl. a plan above the dialog, the dialog
    region with checkbox states), minus the cursor position and the footer hints."""
    norm = [" ".join(_uncursor(line).split()) for line in lines]
    payload = "\n".join([kind, title, *body, plan_file or "", "--", *(line for line in norm if line)])
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()


# --- classification ------------------------------------------------------------------------

def _classify(pre_text: str, above_text: str, options: list[Option], tabs, agent_kind: str | None) -> str:
    labels = [o.label.lower() for o in options]
    if "review your answers" in pre_text or any(label.startswith("submit answers") for label in labels):
        return KIND_QUESTION_REVIEW
    if tabs or any(o.chat for o in options) or any(label.startswith("type something") for label in labels):
        return KIND_QUESTION
    if (any(label.startswith("tell claude what to change") or "auto-accept edits" in label for label in labels)
            or ("plan" in pre_text and "here is claude's plan" in above_text)):
        return KIND_PLAN
    # Anchored on the options: answer text that merely mentions trusting a folder is not this screen.
    if any("i trust this folder" in label or label.startswith("trust and continue") for label in labels):
        return KIND_TRUST
    numbered = any(o.number is not None for o in options)
    if (agent_kind == "codex" and numbered) or "would you like to run the following command" in pre_text:
        return KIND_CODEX_APPROVAL
    if "do you want to" in pre_text:
        return KIND_PERMISSION
    return KIND_UNKNOWN


def _split_generic(kind: str, pre: list[str]) -> tuple[str, tuple[str, ...], str]:
    """(title, body, question) for permission / trust / codex approval / unknown dialogs."""
    body = list(_dedent(pre))
    if not body:
        return "", (), ""
    if kind == KIND_CODEX_APPROVAL:
        question = body.pop(0) if body[0].rstrip().endswith("?") else ""
        return "", _dedent(body), question
    title = body.pop(0).strip()
    question = ""
    if kind in (KIND_PERMISSION, KIND_UNKNOWN) and body and body[-1].rstrip().endswith("?"):
        question = body.pop().strip()
    return title, _dedent(body), question


# --- public API ------------------------------------------------------------------------------

def parse_dialog(screen_text: str, agent_kind: str | None = None, typed_row: int | None = None) -> Dialog | None:
    """The dialog at the bottom of `screen_text`, or None when nothing dialog-like is there.

    `typed_row`: evidence from the caller that option `typed_row` of this multi-select question is its
    "Type something" row (the previous render of the same dialog had a free-text row there). Once text
    is typed the row shows that text instead of its label, so without this evidence it cannot be told
    from a plain option and stays a plain toggle."""
    lines = [line.rstrip() for line in (screen_text or "").replace("\r\n", "\n").split("\n")]
    while lines and not lines[-1].strip():
        lines.pop()
    if not lines:
        return None
    block = _options_block(lines)
    if block is None:
        return None
    raws, end = block.raws, block.end
    width = _width(lines)
    options = [_build_option(raw, lines[raw.index], width) for raw in raws]
    first = raws[0].index
    start = _region_start(lines, first)
    pre = lines[start:first]
    footer = lines[end:]
    above = lines[max(0, start - RULE_LOOKBACK):start]

    tabs: tuple = ()
    pre_rest = pre
    for j, line in enumerate(pre):
        parsed = _parse_tabs(line)
        if parsed:
            tabs, pre_rest = parsed, pre[j + 1:]
            break
    pre_text = " ".join(" ".join(pre).split()).lower()
    kind = _classify(pre_text, " ".join(" ".join(above).split()).lower(), options, tabs, agent_kind)
    multi = any(o.checked is not None for o in options)
    if kind == KIND_QUESTION and multi and block.submit_after is not None:
        # Only on positive evidence: the caller saw a free-text row at this very position.
        k = block.submit_after
        if (typed_row == k and options[k].checked is not None and not options[k].chat
                and not options[k].free_text):
            options[k] = _typed_row(raws[k])
    plan_file = None
    for line in footer:
        m = _PLAN_FILE_RE.search(line)
        if m:
            plan_file = m.group(1)

    if kind == KIND_QUESTION:
        title = next((name for name, done in tabs if not done), tabs[0][0] if tabs else "")
        question_lines = [line.strip().lstrip("│").strip() for line in pre_rest if line.strip()]
        body: tuple[str, ...] = ()
        question = _join(question_lines)
    elif kind == KIND_QUESTION_REVIEW:
        rest = list(_dedent(pre_rest))
        title = "Review your answers"
        if rest and rest[0].strip().lower() == title.lower():
            rest.pop(0)
        question = rest.pop().strip() if rest and rest[-1].rstrip().endswith("?") else ""
        body = _dedent(rest)
    elif kind == KIND_PLAN:
        title = "Ready to code?" if "ready to code?" in " ".join(above).lower() else "Plan"
        body = _plan_excerpt(above)
        question = _join(pre)
    else:
        title, body, question = _split_generic(kind, pre)

    fingerprint = _fingerprint(kind, title, body, plan_file, lines[start:end])
    submit_after = block.submit_after if multi else None
    return Dialog(kind, title, body, question, tuple(options), multi, tabs, plan_file, fingerprint,
                  submit_after, block.cursor_on_submit and submit_after is not None)


def typed_text(option: Option) -> str | None:
    """The text already typed into a multi-select free-text row, or None (not typed / not such a row)."""
    if not option.free_text or option.checked is None or option.label.lower().startswith(_FREE_TEXT_PREFIXES):
        return None
    return option.label


# --- keys --------------------------------------------------------------------------------------

def _rows(dialog: Dialog) -> list:
    """Cursor order of the rows: option indices, with the Submit row after `submit_after`."""
    rows: list = []
    for i in range(len(dialog.options)):
        rows.append(i)
        if dialog.submit_after == i:
            rows.append(SUBMIT)
    return rows


def _moves(dialog: Dialog, target) -> list[str]:
    """`up`/`down` presses from the live cursor to row `target` (an option index or SUBMIT)."""
    rows = _rows(dialog)
    if dialog.cursor_on_submit:
        cursor = rows.index(SUBMIT)
    else:
        cursor = next((rows.index(i) for i, o in enumerate(dialog.options) if o.cursor), 0)
    delta = rows.index(target) - cursor
    return ["down"] * delta if delta > 0 else ["up"] * -delta


def _index(dialog: Dialog, option: Option | int) -> int:
    if isinstance(option, int):
        return option
    index = next((i for i, o in enumerate(dialog.options) if o is option), None)
    return dialog.options.index(option) if index is None else index


def keys_for(dialog: Dialog, option: Option | int) -> list[str]:
    """Key presses that choose `option` (an Option of `dialog` or its index). In a multi-select
    question this toggles the box."""
    index = _index(dialog, option)
    chosen = dialog.options[index]
    if chosen.number is not None and 1 <= chosen.number <= 9:
        return [str(chosen.number)]
    return _moves(dialog, index) + ["enter"]


def text_keys(dialog: Dialog, option: Option | int) -> tuple[list[str], bool]:
    """(keys before typing into a free-text option, whether Enter submits after the text).
    Single-select / plan: the digit turns the option into an input, Enter submits. Multi-select:
    the digit would only toggle the box, so the cursor is moved onto the row; typing checks it
    and Enter would uncheck it again, so none is pressed."""
    index = _index(dialog, option)
    if dialog.multi_select:
        return _moves(dialog, index), False
    return keys_for(dialog, index), True


def submit_keys(dialog: Dialog) -> list[str] | None:
    """Keys that submit a multi-select question (cursor to the Submit row, Enter), or None."""
    if not dialog.multi_select or dialog.submit_after is None:
        return None
    return _moves(dialog, SUBMIT) + ["enter"]
