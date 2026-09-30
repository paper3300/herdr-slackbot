from pathlib import Path

import pytest

from herdr_slackbot import config as cfgmod
from herdr_slackbot.config import (
    ConfigError,
    default_bot_display_name,
    default_slash_command,
    load_config,
    normalize_slash_command,
    parse_env_text,
    sanitize_username,
)


def test_parse_env_text():
    text = """
# comment
SLACK_BOT_TOKEN=xoxb-1
export SLACK_APP_TOKEN = "xapp-1 # not a comment"
BOT_DISPLAY_NAME='Herdr (me)'
LOG_LEVEL=debug # trailing comment
not a line
=novalue
EMPTY=
"""
    values = parse_env_text(text)
    assert values == {
        "SLACK_BOT_TOKEN": "xoxb-1",
        "SLACK_APP_TOKEN": "xapp-1 # not a comment",
        "BOT_DISPLAY_NAME": "Herdr (me)",
        "LOG_LEVEL": "debug",
        "EMPTY": "",
    }


@pytest.mark.parametrize("raw,expected", [
    ("kim2024", "kim2024"),
    ("John.Doe", "john-doe"),
    ("  Kim  Min ", "kim-min"),
    ("홍길동", "user"),
    ("a__b--c", "a-b-c"),
    ("", "user"),
])
def test_sanitize_username(raw, expected):
    assert sanitize_username(raw) == expected


def test_default_slash_command_is_capped():
    assert default_slash_command("kim2024") == "/herdr-kim2024"
    long = default_slash_command("a-very-long-windows-user-name-here")
    assert len(long) <= 32
    assert long.startswith("/herdr-a-very-long")
    assert not long.endswith("-")


def test_normalize_slash_command():
    assert normalize_slash_command("herdr-me") == "/herdr-me"
    assert normalize_slash_command("/Herdr-Me") == "/herdr-me"
    with pytest.raises(ConfigError):
        normalize_slash_command("/herdr me")
    with pytest.raises(ConfigError):
        normalize_slash_command("/" + "x" * 40)


def test_default_bot_display_name():
    assert default_bot_display_name("kim2024") == "Herdr (kim2024)"


def test_load_config_defaults_to_repo_root(tmp_path, monkeypatch):
    monkeypatch.setattr(cfgmod, "REPO_ROOT", tmp_path)
    cfg = load_config(env={"USERNAME": "Kim2024"})
    assert cfg.config_dir == tmp_path
    assert cfg.state_dir == tmp_path / "state"
    assert cfg.slash_command == "/herdr-kim2024"
    assert cfg.bot_display_name == "Herdr (Kim2024)"
    assert cfg.missing_slack_settings() == ["SLACK_BOT_TOKEN", "SLACK_APP_TOKEN"]  # the owner comes from pairing
    assert cfg.needs_pairing
    assert cfg.result_max_chars == 3000


def test_load_config_from_plugin_config_dir(tmp_path):
    (tmp_path / ".env").write_text(
        "SLACK_BOT_TOKEN=xoxb-secret\nSLACK_APP_TOKEN=xapp-secret\nSLACK_OWNER_USER_ID=U123\n"
        "SLASH_COMMAND=/herdr-x\nBOT_DISPLAY_NAME=Bot X\nSTATE_DIR=st\nRESULT_MAX_CHARS=2500\n",
        encoding="utf-8",
    )
    env = {"HERDR_PLUGIN_CONFIG_DIR": str(tmp_path), "USERNAME": "u", "SLACK_OWNER_USER_ID": "U999"}
    cfg = load_config(env=env)
    assert cfg.config_dir == tmp_path
    assert cfg.state_dir == tmp_path / "st"
    assert cfg.slash_command == "/herdr-x"
    assert cfg.bot_display_name == "Bot X"
    assert cfg.slack_owner_user_id == "U999"  # process env wins over .env
    assert cfg.result_max_chars == 2500
    assert cfg.missing_slack_settings() == []
    assert "secret" not in repr(cfg)  # tokens are kept out of repr / logs


def test_load_config_bad_int(tmp_path):
    (tmp_path / ".env").write_text("READ_LINES=lots\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(env={"USERNAME": "u"}, config_dir=Path(tmp_path))


def test_env_file_with_bom(tmp_path):
    (tmp_path / ".env").write_bytes("﻿SLASH_COMMAND=/herdr-bom\n".encode("utf-8"))
    assert load_config(env={"USERNAME": "u"}, config_dir=tmp_path).slash_command == "/herdr-bom"
