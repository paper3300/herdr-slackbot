"""`python -m herdr_slackbot setup`: config dir `.env` skeleton + Slack app manifest.

The venv / dependency install happens before this in `scripts/setup.ps1` (it needs a
Python with the dependencies to run this module at all). Existing `.env` values are never
overwritten: missing keys are appended from `.env.example`.

Every read-modify-write of `.env` (setup, the wizard, the bridge saving a paired owner) runs under
`env_lock` (an OS file lock next to it), so one writer can never replace the file with a stale copy
and drop another writer's key.
"""

from __future__ import annotations

import os
import re
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping

from .config import (
    ENV_FILE_NAME,
    PLUGIN_ID,
    REPO_ROOT,
    ConfigError,
    current_username,
    default_bot_display_name,
    default_slash_command,
    load_config,
    load_env_file,
    normalize_slash_command,
    parse_env_text,
)
from .slack_manifest import build_manifest, manifest_json
from .state import _lock_file, _unlock_file

TEMPLATE_PATH = REPO_ROOT / ".env.example"
MANIFEST_FILE_NAME = "slack-app-manifest.json"
ENV_LOCK_NAME = ".env.lock"
ENV_LOCK_WAIT = 10.0  # seconds to wait for another writer of .env
_KEY_LINE_RE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=")


def format_env_value(value: str) -> str:
    if value == "" or re.fullmatch(r"[A-Za-z0-9_./:@+-]+", value):
        return value
    quote = "'" if '"' in value else '"'
    return f"{quote}{value}{quote}"


def _active_key(line: str) -> str | None:
    """Key of an uncommented `KEY=...` line."""
    m = _KEY_LINE_RE.match(line)
    return m.group(1) if m and not line.lstrip().startswith("#") else None


def render_template(template: str, values: Mapping[str, str]) -> str:
    """Fill `KEY=` lines of the template with `values` (only non-empty ones)."""
    out = []
    for line in template.splitlines():
        key = _active_key(line)
        if key and values.get(key):
            line = f"{key}={format_env_value(values[key])}"
        out.append(line)
    return "\n".join(out) + "\n"


def _template_blocks(template: str) -> list[tuple[str, list[str]]]:
    """(key, lines) for each active key: the key line plus the comment lines right above it."""
    blocks: list[tuple[str, list[str]]] = []
    pending: list[str] = []
    for line in template.splitlines():
        key = _active_key(line)
        if key:
            blocks.append((key, [*pending, line]))
            pending = []
        elif line.strip().startswith("#") and not line.strip().startswith("# ---"):
            pending.append(line)
        else:
            pending = []
    return blocks


def merge_env(existing: str | None, template: str, values: Mapping[str, str]) -> tuple[str, list[str]]:
    """Return (new text, keys added). Existing lines and values are kept verbatim."""
    rendered = render_template(template, values)
    if existing is None or not existing.strip():
        return rendered, [key for key, _ in _template_blocks(rendered)]
    present = set(parse_env_text(existing))
    missing = [(key, lines) for key, lines in _template_blocks(rendered) if key not in present]
    if not missing:
        return existing, []
    text = existing if existing.endswith("\n") else existing + "\n"
    text += "\n# --- added by herdr-slackbot setup ---\n"
    for _, lines in missing:
        text += "\n".join(lines) + "\n"
    return text, [key for key, _ in missing]


def update_env_text(text: str | None, values: Mapping[str, str]) -> str:
    """Set `KEY=value` in place: each active `KEY=` line is replaced (keeping its line ending);
    comments, other keys and their order stay as they are. Keys without a line are appended."""
    out = (text or "").splitlines(keepends=True)
    seen: set[str] = set()
    for i, line in enumerate(out):
        key = _active_key(line)
        if key in values:
            ending = line[len(line.rstrip("\r\n")):] or "\n"
            out[i] = f"{key}={format_env_value(values[key])}{ending}"
            seen.add(key)
    rest = [key for key in values if key not in seen]
    if rest:
        if out and not out[-1].endswith("\n"):
            out[-1] += "\n"
        out += [f"{key}={format_env_value(values[key])}\n" for key in rest]
    return "".join(out)


class EnvLockTimeout(OSError):
    """Another process kept `.env` locked for longer than ENV_LOCK_WAIT."""


@contextmanager
def env_lock(config_dir: Path, timeout: float = ENV_LOCK_WAIT, sleep: Callable[[float], None] = time.sleep):
    """Cross-process lock for read-modify-write of `<config_dir>/.env` (also excludes other
    threads: the OS lock is per handle)."""
    config_dir = Path(config_dir)
    config_dir.mkdir(parents=True, exist_ok=True)
    fh = open(config_dir / ENV_LOCK_NAME, "a+b")
    try:
        deadline = time.monotonic() + timeout
        while True:
            try:
                _lock_file(fh)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise EnvLockTimeout(f"{config_dir / ENV_FILE_NAME} is locked by another writer") from None
                sleep(0.05)
        try:
            yield
        finally:
            _unlock_file(fh)
    finally:
        fh.close()


def update_env_file(path: Path, values: Mapping[str, str]) -> None:
    """`update_env_text` on the file: read and replace under `env_lock`, so the latest content is
    merged (atomic replace)."""
    path = Path(path)
    with env_lock(path.parent):
        existing = path.read_text(encoding="utf-8-sig") if path.is_file() else None
        _atomic_write(path, update_env_text(existing, values))


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


@dataclass
class SetupResult:
    config_dir: Path
    env_file: Path
    manifest_file: Path
    slash_command: str
    display_name: str
    env_created: bool
    keys_added: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    paired: bool = False  # SLACK_OWNER_USER_ID is set


def run_setup(config_dir: Path, *, username: str | None = None, slash_command: str | None = None,
              display_name: str | None = None, template_path: Path = TEMPLATE_PATH) -> SetupResult:
    config_dir = Path(config_dir)
    username = username or current_username()
    env_file = config_dir / ENV_FILE_NAME
    with env_lock(config_dir):
        return _run_setup_locked(config_dir, env_file, username, slash_command, display_name, template_path)


def _run_setup_locked(config_dir: Path, env_file: Path, username: str, slash_command: str | None,
                      display_name: str | None, template_path: Path) -> SetupResult:
    existing_text = env_file.read_text(encoding="utf-8-sig") if env_file.is_file() else None
    existing = load_env_file(env_file)
    notes: list[str] = []

    def pick(key: str, override: str | None, default: str) -> str:
        if key in existing:  # kept as is, even when blank (blank = runtime default)
            current = existing[key]
            if override and override != current:
                shown = repr(current) if current else "blank (so the default applies)"
                notes.append(f"{key} is already {shown} in .env; kept it, so {override!r} was not applied "
                             "(edit .env to change it, then run setup again)")
            return current
        return override or default

    slash = pick("SLASH_COMMAND", slash_command and normalize_slash_command(slash_command),
                 default_slash_command(username))
    if slash:
        normalize_slash_command(slash)  # reject an invalid existing value early
    name = pick("BOT_DISPLAY_NAME", display_name, default_bot_display_name(username))

    template = template_path.read_text(encoding="utf-8")
    new_text, added = merge_env(existing_text, template, {"SLASH_COMMAND": slash, "BOT_DISPLAY_NAME": name})
    if new_text != existing_text:
        _atomic_write(env_file, new_text)

    # The manifest must describe what the bridge will actually register: derive it from the
    # effective configuration after the merge (blank values fall back to the defaults there).
    effective = load_config(env={"USERNAME": username}, config_dir=config_dir)
    manifest_file = config_dir / MANIFEST_FILE_NAME
    _atomic_write(manifest_file, manifest_json(build_manifest(effective.slash_command, effective.bot_display_name,
                                                              username)))

    values = parse_env_text(new_text)
    missing = [k for k in ("SLACK_BOT_TOKEN", "SLACK_APP_TOKEN") if not values.get(k)]
    return SetupResult(config_dir, env_file, manifest_file, effective.slash_command, effective.bot_display_name,
                       existing_text is None, added, missing, notes, paired=bool(values.get("SLACK_OWNER_USER_ID")))


def next_steps(result: SetupResult) -> list[str]:
    lines = [
        f"config dir:      {result.config_dir}",
        f".env:            {result.env_file} ({'created' if result.env_created else 'kept'}"
        + (f"; added {', '.join(result.keys_added)}" if result.keys_added and not result.env_created else "") + ")",
        f"slash command:   {result.slash_command}",
        f"bot name:        {result.display_name}",
        f"Slack manifest:  {result.manifest_file}",
        *[f"note: {n}" for n in result.notes],
        "",
    ]
    if not result.missing:
        lines += ["Slack tokens are set. Restart the bridge:",
                  f"  herdr plugin action invoke restart --plugin {PLUGIN_ID}"]
        if not result.paired:
            lines.append("Then pair your Slack account: the bridge pane (workspace herdr-slack) shows a code; "
                         f"run `{result.slash_command} pair <code>` in Slack.")
        return lines
    lines += [
        "Easiest: the interactive setup wizard (opens a terminal tab in Herdr):",
        f"  herdr plugin action invoke setup --plugin {PLUGIN_ID}",
        "",
        "Or by hand:",
        "  1. https://api.slack.com/apps -> Create New App -> From a manifest -> pick the workspace,",
        f"     paste the contents of {result.manifest_file.name} (JSON tab) -> Create.",
        "  2. Basic Information -> App-Level Tokens -> Generate (scope: connections:write) -> xapp-... "
        "= SLACK_APP_TOKEN",
        "  3. Install App -> Install to Workspace -> Bot User OAuth Token xoxb-... = SLACK_BOT_TOKEN",
        f"  4. Fill those into {result.env_file}",
        f"  5. herdr plugin action invoke restart --plugin {PLUGIN_ID}   (or restart Herdr)",
        f"  6. Pairing: the bridge pane shows a code; run `{result.slash_command} pair <code>` in Slack",
        f"  missing now: {', '.join(result.missing)}",
    ]
    return lines


def main_setup(config_dir: Path, username: str | None, slash_command: str | None, display_name: str | None,
               out: Callable[[str], None] = print) -> int:
    try:
        result = run_setup(config_dir, username=username, slash_command=slash_command, display_name=display_name)
    except ConfigError as exc:
        out(f"setup error: {exc}")
        return 2
    for line in next_steps(result):
        out(line)
    return 0
