"""Slack app manifest: scopes derived from the Web API calls the code makes."""

import json
import re
from pathlib import Path

from herdr_slackbot import slack_manifest as M
from herdr_slackbot.config import load_config

PKG = Path(M.__file__).parent
WEB_API_PREFIXES = ("chat_", "conversations_", "files_", "views_", "users_", "reactions_", "im_", "apps_")


def _transport_calls() -> set[str]:
    src = (PKG / "slack_transport.py").read_text(encoding="utf-8")
    return set(re.findall(r'_call\(\s*"(\w+)"', src))


def test_every_transport_call_has_a_scope_entry():
    calls = _transport_calls()
    assert calls, "expected WebClientTransport._call usages"
    assert calls == set(M.METHOD_SCOPES), "update METHOD_SCOPES when Slack API calls change"


def test_no_direct_web_api_calls_outside_transport():
    pattern = re.compile(r"\.(" + "|".join(WEB_API_PREFIXES) + r")\w*\(")
    offenders = []
    for path in PKG.glob("*.py"):
        if path.name in ("slack_transport.py", "slack_manifest.py"):
            continue
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            for m in pattern.finditer(line):
                # The setup wizard verifies tokens before the bridge exists: apps.connections.open
                # uses the app-level token (connections:write), so no bot scope is involved.
                if path.name == "wizard.py" and line[m.start():].startswith(".apps_connections_open("):
                    continue
                offenders.append(f"{path.name}:{lineno}: {line.strip()}")
    assert not offenders, offenders


def test_required_scopes():
    assert M.required_scopes() == ["chat:write", "commands", "files:write", "im:history", "im:write"]


def test_manifest_shape():
    m = M.build_manifest("/herdr-me", "Herdr (me)", "me")
    assert m["display_information"]["name"] == "Herdr (me)"
    assert m["features"]["bot_user"] == {"display_name": "Herdr (me)", "always_online": True}
    (cmd,) = m["features"]["slash_commands"]
    assert cmd["command"] == "/herdr-me"
    assert cmd["should_escape"] is False
    assert len(cmd["description"]) <= M.COMMAND_DESC_MAX
    home = m["features"]["app_home"]
    assert home["messages_tab_enabled"] and not home["messages_tab_read_only_enabled"]
    settings = m["settings"]
    assert settings["socket_mode_enabled"] is True
    assert settings["interactivity"] == {"is_enabled": True}
    assert "request_url" not in settings["interactivity"]  # Socket Mode: no public endpoint
    assert settings["event_subscriptions"] == {"bot_events": ["message.im", "app_home_opened"]}
    assert m["oauth_config"]["scopes"]["bot"] == M.required_scopes()


def test_long_display_name_is_cut_to_slack_limits():
    name = "Herdr (" + "x" * 90 + ")"
    m = M.build_manifest("/herdr-x", name, "x")
    assert len(m["display_information"]["name"]) == M.APP_NAME_MAX
    assert len(m["features"]["bot_user"]["display_name"]) == M.BOT_NAME_MAX


def test_manifest_follows_config(tmp_path):
    (tmp_path / ".env").write_text("SLASH_COMMAND=herdr-custom\nBOT_DISPLAY_NAME='My Herdr'\n", encoding="utf-8")
    cfg = load_config(env={"USERNAME": "Kim"}, config_dir=tmp_path)
    m = M.manifest_for_config(cfg)
    assert m["features"]["slash_commands"][0]["command"] == "/herdr-custom" == cfg.slash_command
    assert m["features"]["bot_user"]["display_name"] == "My Herdr"
    text = M.manifest_json(m)
    assert json.loads(text) == m and text.endswith("\n")


def test_default_manifest_uses_username(tmp_path):
    cfg = load_config(env={"USERNAME": "John.Doe"}, config_dir=tmp_path)
    m = M.manifest_for_config(cfg)
    assert m["features"]["slash_commands"][0]["command"] == "/herdr-john-doe"
    assert m["features"]["bot_user"]["display_name"] == "Herdr (John.Doe)"
