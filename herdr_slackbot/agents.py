"""Agent kinds offered by `/herdr new` and their launch arguments (D4)."""

from __future__ import annotations

import json
import logging
import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

KIND_CLAUDE = "claude"
KIND_CODEX = "codex"
KINDS = (KIND_CLAUDE, KIND_CODEX)

# D4-b: only these are exposed; never dontAsk / bypassPermissions.
PERMISSION_MODES = ("manual", "acceptEdits", "auto", "plan")


@dataclass(frozen=True)
class KindSpec:
    models: tuple[str, ...]
    efforts: tuple[str, ...]
    default_model: str | None
    default_effort: str | None
    supports_permission_mode: bool


KIND_SPECS = {
    KIND_CLAUDE: KindSpec(
        models=("opus", "sonnet", "haiku"),
        efforts=("low", "medium", "high", "xhigh", "max"),
        default_model="opus",
        default_effort="high",
        supports_permission_mode=True,
    ),
    KIND_CODEX: KindSpec(
        models=(),  # dynamic: see codex_model_options()
        efforts=("low", "medium", "high", "xhigh", "max"),
        default_model=None,
        default_effort="high",
        supports_permission_mode=False,
    ),
}
DEFAULT_PERMISSION_MODE = "auto"


class LaunchError(ValueError):
    pass


def build_agent_args(kind: str, model: str | None = None, effort: str | None = None,
                     permission_mode: str | None = None) -> list[str]:
    """Native CLI args passed after `--` to `herdr agent start`."""
    spec = KIND_SPECS.get(kind)
    if spec is None:
        raise LaunchError(f"unsupported agent kind {kind!r} (choose {', '.join(KINDS)})")
    model = (model or spec.default_model or "").strip() or None
    effort = (effort or spec.default_effort or "").strip() or None
    if effort and effort not in spec.efforts:
        raise LaunchError(f"unsupported effort {effort!r} for {kind}")
    if model and any(c.isspace() for c in model):
        raise LaunchError(f"invalid model name {model!r}")

    if kind == KIND_CLAUDE:
        mode = permission_mode or DEFAULT_PERMISSION_MODE
        if mode not in PERMISSION_MODES:
            raise LaunchError(f"unsupported permission mode {mode!r}")
        args = []
        if model:
            args += ["--model", model]
        if effort:
            args += ["--effort", effort]
        return args + ["--permission-mode", mode]

    args = []
    if model:
        args += ["-m", model]
    if effort:
        args += ["-c", f"model_reasoning_effort={effort}"]
    return args


# --- Codex model list (SPEC "Codex options") ------------------------------------------
CODEX_STATIC_MODELS = (("gpt-6-sol", "GPT-6-Sol"), ("gpt-6-astra", "GPT-6-Astra"), ("gpt-5.5", "GPT-5.5"))


def codex_home(env: dict | None = None) -> Path:
    env = os.environ if env is None else env
    return Path(env["CODEX_HOME"]) if env.get("CODEX_HOME") else Path.home() / ".codex"


def load_codex_models(home: Path) -> list[tuple[str, str]]:
    """(slug, display name) of models with `visibility: "list"` in models_cache.json."""
    try:
        data = json.loads((home / "models_cache.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        log.info("codex models cache unavailable (%s); using static list", exc)
        return list(CODEX_STATIC_MODELS)
    models = data.get("models") if isinstance(data, dict) else data
    out = []
    for m in models if isinstance(models, list) else []:
        if isinstance(m, dict) and m.get("visibility") == "list" and m.get("slug"):
            out.append((str(m["slug"]), str(m.get("display_name") or m["slug"])))
    return out or list(CODEX_STATIC_MODELS)


def load_codex_default_model(home: Path) -> str | None:
    try:
        with open(home / "config.toml", "rb") as f:
            model = tomllib.load(f).get("model")
    except (OSError, tomllib.TOMLDecodeError):
        return None
    return str(model) if model else None


def codex_model_options(home: Path | None = None) -> tuple[list[tuple[str, str]], str]:
    """Model dropdown for codex: (options, default). Default = config.toml `model`, else first listed.

    A configured default missing from the list is prepended so it stays selectable.
    """
    home = home or codex_home()
    models = load_codex_models(home)
    default = load_codex_default_model(home)
    if default and default not in {slug for slug, _ in models}:
        models = [(default, default)] + models
    return models, default or models[0][0]
