import json

import pytest

from herdr_slackbot.agents import (
    CODEX_STATIC_MODELS,
    KIND_SPECS,
    LaunchError,
    build_agent_args,
    codex_model_options,
)
from herdr_slackbot.naming import (
    agent_option_label,
    auto_agent_name,
    format_duration,
    summarize_prompt,
    tab_label,
    validate_agent_name,
)


def test_auto_agent_name():
    assert auto_agent_name(7) == "slack-7"
    assert validate_agent_name(auto_agent_name(123)) is None


@pytest.mark.parametrize("name", ["a", "slack-1", "fix_bug-2", "a" * 32])
def test_valid_names(name):
    assert validate_agent_name(name) is None


@pytest.mark.parametrize("name", ["", "1abc", "Abc", "a b", "a.b", "a" * 33, "한글"])
def test_invalid_names(name):
    assert "Invalid name" in validate_agent_name(name)


def test_duplicate_name_rejected():
    assert "already running" in validate_agent_name("coder", ["coder", None, "reviewer"])


def test_summarize_prompt():
    assert summarize_prompt("\n\n  fix   the\tlogin bug  \nmore") == "fix the login bug"
    long = summarize_prompt("x" * 100, 40)
    assert len(long) == 40 and long.endswith("…")
    assert summarize_prompt("   \n") == ""


def test_tab_label():
    assert tab_label("slack-3", "리뷰 해줘\n자세히", auto_named=True) == "slack-3 리뷰 해줘"
    assert tab_label("my-agent", "whatever", auto_named=False) == "my-agent"
    assert tab_label("slack-3", "", auto_named=True) == "slack-3"
    assert len(tab_label("slack-3", "y" * 200, auto_named=True)) <= 60


def test_agent_option_label():
    agent = {"pane_id": "w3:p28", "agent_status": "done", "workspace_id": "w3",
             "terminal_title": "✳ 문서 오타 수정", "terminal_title_stripped": "문서 오타 수정"}
    assert agent_option_label(agent, {"w3": "DemoApp"}) == "✅ · w3:p28 · DemoApp · done · 문서 오타 수정"
    named = dict(agent, name="coder", terminal_title_stripped="t" * 200)
    label = agent_option_label(named, {})
    assert label.startswith("✅ · coder · w3 · done · ttt")
    assert len(label) <= 75


def test_agent_option_label_without_title_or_status():
    assert agent_option_label({"pane_id": "w1:p1", "workspace_id": "w1"}, {"w1": "Main"}) == \
        "❔ · w1:p1 · Main · unknown"


@pytest.mark.parametrize("secs,text", [(0, "0s"), (59.4, "59s"), (129, "2m 9s"), (3725, "1h 2m"), (None, "")])
def test_format_duration(secs, text):
    assert format_duration(secs) == text


def test_build_agent_args_claude_defaults():
    assert build_agent_args("claude") == ["--model", "opus", "--effort", "high", "--permission-mode", "auto"]
    assert build_agent_args("claude", "sonnet", "max", "plan") == \
        ["--model", "sonnet", "--effort", "max", "--permission-mode", "plan"]


def test_build_agent_args_codex():
    assert build_agent_args("codex") == ["-c", "model_reasoning_effort=high"]
    assert build_agent_args("codex", "gpt-6-astra", "low") == ["-m", "gpt-6-astra", "-c", "model_reasoning_effort=low"]
    assert build_agent_args("codex", effort="max") == ["-c", "model_reasoning_effort=max"]
    assert KIND_SPECS["codex"].efforts == ("low", "medium", "high", "xhigh", "max")
    assert KIND_SPECS["codex"].default_effort == "high"
    assert not KIND_SPECS["codex"].supports_permission_mode


@pytest.mark.parametrize("kwargs", [
    {"kind": "gemini"},
    {"kind": "claude", "permission_mode": "bypassPermissions"},
    {"kind": "claude", "permission_mode": "dontAsk"},
    {"kind": "claude", "effort": "ultra"},
    {"kind": "codex", "model": "a b"},
])
def test_build_agent_args_rejects(kwargs):
    with pytest.raises(LaunchError):
        build_agent_args(**kwargs)


def _codex_home(tmp_path, models=None, config=None):
    if models is not None:
        (tmp_path / "models_cache.json").write_text(json.dumps({"models": models}), encoding="utf-8")
    if config is not None:
        (tmp_path / "config.toml").write_text(config, encoding="utf-8")
    return tmp_path


def test_codex_model_options_from_cache_and_config(tmp_path):
    home = _codex_home(tmp_path, [
        {"slug": "gpt-6-astra", "display_name": "GPT-6-Astra", "visibility": "list"},
        {"slug": "gpt-reserve", "display_name": "GPT-Reserve", "visibility": "hide"},
        {"slug": "gpt-6-sol", "display_name": "GPT-6-Sol", "visibility": "list"},
    ], 'model = "gpt-6-sol"\nmodel_reasoning_effort = "high"\n')
    models, default = codex_model_options(home)
    assert models == [("gpt-6-astra", "GPT-6-Astra"), ("gpt-6-sol", "GPT-6-Sol")]
    assert default == "gpt-6-sol"


def test_codex_model_options_fallbacks(tmp_path):
    models, default = codex_model_options(_codex_home(tmp_path))  # no files at all
    assert models == list(CODEX_STATIC_MODELS) and default == CODEX_STATIC_MODELS[0][0]
    home = _codex_home(tmp_path, [{"slug": "a", "visibility": "list"}], 'model = "custom-x"\n')
    models, default = codex_model_options(home)
    assert models == [("custom-x", "custom-x"), ("a", "a")] and default == "custom-x"
    (tmp_path / "config.toml").write_text("not = [toml", encoding="utf-8")
    (tmp_path / "models_cache.json").write_text("{bad", encoding="utf-8")
    models, default = codex_model_options(tmp_path)
    assert models == list(CODEX_STATIC_MODELS) and default == CODEX_STATIC_MODELS[0][0]
