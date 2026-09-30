"""`setup`: .env skeleton merge (never overwrites) and manifest output."""

import json

import pytest

from herdr_slackbot import __main__ as main_mod
from herdr_slackbot.config import load_config, parse_env_text
from herdr_slackbot.setup_cmd import (
    TEMPLATE_PATH,
    format_env_value,
    merge_env,
    next_steps,
    render_template,
    run_setup,
)

TEMPLATE = TEMPLATE_PATH.read_text(encoding="utf-8")
DEFAULTS = {"SLASH_COMMAND": "/herdr-me", "BOT_DISPLAY_NAME": "Herdr (me)"}


@pytest.mark.parametrize("value,expected", [
    ("", ""),
    ("/herdr-me", "/herdr-me"),
    ("xoxb-1-2", "xoxb-1-2"),
    ("Herdr (me)", '"Herdr (me)"'),
    ('say "hi"', "'say \"hi\"'"),
    ("a #b", '"a #b"'),
])
def test_format_env_value(value, expected):
    assert format_env_value(value) == expected
    assert parse_env_text(f"K={format_env_value(value)}")["K"] == value


def test_template_has_every_documented_setting():
    active = parse_env_text(TEMPLATE)
    assert set(active) == {"SLACK_BOT_TOKEN", "SLACK_APP_TOKEN", "SLACK_OWNER_USER_ID", "SLASH_COMMAND",
                           "BOT_DISPLAY_NAME"}
    from herdr_slackbot.config import SETTING_KEYS
    for key in SETTING_KEYS:
        assert f"{key}=" in TEMPLATE, key


def test_new_env_is_rendered_template():
    text, added = merge_env(None, TEMPLATE, DEFAULTS)
    values = parse_env_text(text)
    assert values["SLASH_COMMAND"] == "/herdr-me"
    assert values["BOT_DISPLAY_NAME"] == "Herdr (me)"
    assert values["SLACK_BOT_TOKEN"] == ""
    assert added == ["SLACK_BOT_TOKEN", "SLACK_APP_TOKEN", "SLACK_OWNER_USER_ID", "SLASH_COMMAND", "BOT_DISPLAY_NAME"]
    assert text == render_template(TEMPLATE, DEFAULTS)


def test_merge_keeps_existing_values_and_adds_missing_keys():
    existing = "# mine\nSLACK_BOT_TOKEN=xoxb-real\nSLASH_COMMAND=/herdr-custom\nLOG_LEVEL=DEBUG"
    text, added = merge_env(existing, TEMPLATE, DEFAULTS)
    assert text.startswith(existing + "\n")  # original lines untouched
    values = parse_env_text(text)
    assert values["SLACK_BOT_TOKEN"] == "xoxb-real"
    assert values["SLASH_COMMAND"] == "/herdr-custom"
    assert values["LOG_LEVEL"] == "DEBUG"
    assert values["BOT_DISPLAY_NAME"] == "Herdr (me)"
    assert added == ["SLACK_APP_TOKEN", "SLACK_OWNER_USER_ID", "BOT_DISPLAY_NAME"]
    assert "# App-Level Token" in text  # the key's explanatory comment comes along
    assert text.count("SLASH_COMMAND=") == 1


def test_blank_existing_key_is_not_readded_and_merge_is_idempotent():
    existing = "SLACK_BOT_TOKEN=\nSLACK_APP_TOKEN=\nSLACK_OWNER_USER_ID=\nSLASH_COMMAND=\nBOT_DISPLAY_NAME=\n"
    text, added = merge_env(existing, TEMPLATE, DEFAULTS)
    assert (text, added) == (existing, [])
    once, _ = merge_env("SLACK_BOT_TOKEN=x\n", TEMPLATE, DEFAULTS)
    twice, added_again = merge_env(once, TEMPLATE, DEFAULTS)
    assert twice == once and added_again == []


def test_run_setup_creates_env_and_manifest(tmp_path):
    result = run_setup(tmp_path, username="Me")
    assert result.env_created
    env = load_config(env={"USERNAME": "Me"}, config_dir=tmp_path)
    assert env.slash_command == "/herdr-me" and env.bot_display_name == "Herdr (Me)"
    manifest = json.loads(result.manifest_file.read_text(encoding="utf-8"))
    assert manifest["features"]["slash_commands"][0]["command"] == "/herdr-me"
    assert result.missing == ["SLACK_BOT_TOKEN", "SLACK_APP_TOKEN"]
    assert not result.paired
    steps = "\n".join(next_steps(result))
    assert "From a manifest" in steps and "connections:write" in steps and "/herdr-me pair <code>" in steps


def test_run_setup_never_overwrites_existing_values(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("SLACK_BOT_TOKEN=xoxb-keep\nSLASH_COMMAND=/herdr-old\n", encoding="utf-8")
    result = run_setup(tmp_path, username="me", slash_command="/herdr-new", display_name="New Name")
    values = parse_env_text(env_file.read_text(encoding="utf-8"))
    assert values["SLACK_BOT_TOKEN"] == "xoxb-keep"
    assert values["SLASH_COMMAND"] == "/herdr-old"
    assert values["BOT_DISPLAY_NAME"] == "New Name"  # was missing, so the override fills it
    assert result.slash_command == "/herdr-old"
    assert any("SLASH_COMMAND" in n for n in result.notes)
    manifest = json.loads(result.manifest_file.read_text(encoding="utf-8"))
    assert manifest["features"]["slash_commands"][0]["command"] == "/herdr-old"


def test_run_setup_with_complete_settings_says_restart(tmp_path):
    (tmp_path / ".env").write_text("SLACK_BOT_TOKEN=xoxb-1\nSLACK_APP_TOKEN=xapp-1\nSLACK_OWNER_USER_ID=U1\n",
                                   encoding="utf-8")
    result = run_setup(tmp_path, username="me")
    assert result.missing == []
    assert "restart" in "\n".join(next_steps(result))


def test_setup_cli(tmp_path, capsys):
    assert main_mod.main(["setup", "--config-dir", str(tmp_path), "--username", "Lee",
                          "--slash-command", "herdr-lee2"]) == 0
    out = capsys.readouterr().out
    assert "/herdr-lee2" in out
    assert (tmp_path / "slack-app-manifest.json").is_file()
    assert parse_env_text((tmp_path / ".env").read_text(encoding="utf-8"))["SLASH_COMMAND"] == "/herdr-lee2"


def test_setup_cli_rejects_bad_slash_command(tmp_path, capsys):
    assert main_mod.main(["setup", "--config-dir", str(tmp_path), "--slash-command", "bad name!"]) == 2
    assert "setup error" in capsys.readouterr().out
    assert not (tmp_path / ".env").exists()


def test_m3_review4_blank_keys_with_overrides_manifest_matches_runtime(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("SLASH_COMMAND=\nBOT_DISPLAY_NAME=\n", encoding="utf-8")
    result = run_setup(tmp_path, username="me", slash_command="/herdr-custom", display_name="Custom")
    values = parse_env_text(env_file.read_text(encoding="utf-8"))
    assert values["SLASH_COMMAND"] == "" and values["BOT_DISPLAY_NAME"] == ""  # blanks preserved as promised
    runtime = load_config(env={"USERNAME": "me"}, config_dir=tmp_path)
    manifest = json.loads(result.manifest_file.read_text(encoding="utf-8"))
    assert manifest["features"]["slash_commands"][0]["command"] == runtime.slash_command == "/herdr-me"
    assert manifest["features"]["bot_user"]["display_name"] == runtime.bot_display_name == "Herdr (me)"
    assert (result.slash_command, result.display_name) == (runtime.slash_command, runtime.bot_display_name)
    notes = " ".join(result.notes)
    assert "'/herdr-custom' was not applied" in notes and "'Custom' was not applied" in notes
    steps = "\n".join(next_steps(result))
    assert "/herdr-me" in steps and "/herdr-custom" in steps  # shown value + explanation


def test_m3_review4_manifest_always_follows_effective_config(tmp_path):
    (tmp_path / ".env").write_text("SLASH_COMMAND=/herdr-kept\n", encoding="utf-8")
    result = run_setup(tmp_path, username="me", slash_command="/herdr-other", display_name="Given")
    runtime = load_config(env={"USERNAME": "me"}, config_dir=tmp_path)
    manifest = json.loads(result.manifest_file.read_text(encoding="utf-8"))
    assert manifest["features"]["slash_commands"][0]["command"] == runtime.slash_command == "/herdr-kept"
    assert manifest["features"]["bot_user"]["display_name"] == runtime.bot_display_name == "Given"


# --- in-place .env update (wizard tokens, pairing owner) --------------------------------------

def test_update_env_text_replaces_in_place_and_keeps_everything_else():
    from herdr_slackbot.setup_cmd import update_env_text

    text = ("# header\r\nSLACK_BOT_TOKEN=\r\n# owner comment\r\nSLACK_OWNER_USER_ID=  # inline\r\n"
            "# SLACK_APP_TOKEN=commented\r\nLOG_LEVEL=DEBUG\r\n")
    out = update_env_text(text, {"SLACK_BOT_TOKEN": "xoxb-1-2", "SLACK_OWNER_USER_ID": "U42"})
    assert out == ("# header\r\nSLACK_BOT_TOKEN=xoxb-1-2\r\n# owner comment\r\nSLACK_OWNER_USER_ID=U42\r\n"
                   "# SLACK_APP_TOKEN=commented\r\nLOG_LEVEL=DEBUG\r\n")


def test_update_env_text_appends_missing_keys_and_quotes():
    from herdr_slackbot.setup_cmd import update_env_text

    out = update_env_text("A=1", {"BOT_DISPLAY_NAME": "Herdr (kim)", "A": "2"})
    assert out == 'A=2\nBOT_DISPLAY_NAME="Herdr (kim)"\n'
    assert parse_env_text(out) == {"A": "2", "BOT_DISPLAY_NAME": "Herdr (kim)"}
    assert update_env_text(None, {"K": "v"}) == "K=v\n"


def test_update_env_text_rewrites_every_active_duplicate():
    from herdr_slackbot.setup_cmd import update_env_text

    out = update_env_text("K=old\nexport K=older\n", {"K": "new"})
    assert out == "K=new\nK=new\n" and parse_env_text(out)["K"] == "new"


def test_update_env_file_is_atomic_and_keeps_other_values(tmp_path):
    from herdr_slackbot.setup_cmd import update_env_file

    run_setup(tmp_path, username="me")
    env_file = tmp_path / ".env"
    before = env_file.read_text(encoding="utf-8")
    update_env_file(env_file, {"SLACK_APP_TOKEN": "xapp-1-A-2"})
    after = env_file.read_text(encoding="utf-8")
    assert parse_env_text(after)["SLACK_APP_TOKEN"] == "xapp-1-A-2"
    assert parse_env_text(after)["SLASH_COMMAND"] == "/herdr-me"
    assert after.replace("SLACK_APP_TOKEN=xapp-1-A-2", "SLACK_APP_TOKEN=") == before
    assert [p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp")] == []


def test_run_setup_with_tokens_but_no_owner_explains_pairing(tmp_path):
    (tmp_path / ".env").write_text("SLACK_BOT_TOKEN=xoxb-1\nSLACK_APP_TOKEN=xapp-1\n", encoding="utf-8")
    result = run_setup(tmp_path, username="me")
    assert result.missing == [] and not result.paired
    assert "/herdr-me pair <code>" in "\n".join(next_steps(result))


# --- .env writers are serialized (release review 3) ------------------------------------------------

def test_env_writer_waits_for_the_lock_and_merges_the_latest_file(tmp_path):
    """A wizard write that starts while the bridge is saving the paired owner must not write back a
    stale copy without the owner."""
    import threading

    from herdr_slackbot.setup_cmd import env_lock, update_env_file

    env_file = tmp_path / ".env"
    env_file.write_text("SLACK_BOT_TOKEN=\nSLACK_APP_TOKEN=\nSLACK_OWNER_USER_ID=\n", encoding="utf-8")
    done = threading.Event()
    with env_lock(tmp_path):  # "the bridge" is mid-write
        worker = threading.Thread(target=lambda: (update_env_file(env_file, {"SLACK_BOT_TOKEN": "xoxb-new"}),
                                                  done.set()))
        worker.start()
        assert not done.wait(0.3)  # the wizard's write waits
        env_file.write_text("SLACK_BOT_TOKEN=\nSLACK_APP_TOKEN=\nSLACK_OWNER_USER_ID=UKIM\n", encoding="utf-8")
    worker.join(5)
    assert done.is_set()
    values = parse_env_text(env_file.read_text(encoding="utf-8"))
    assert values["SLACK_OWNER_USER_ID"] == "UKIM" and values["SLACK_BOT_TOKEN"] == "xoxb-new"


def test_concurrent_env_writers_keep_every_key(tmp_path):
    import threading

    from herdr_slackbot.setup_cmd import update_env_file

    env_file = tmp_path / ".env"
    env_file.write_text("# keep me\n", encoding="utf-8")
    keys = [f"KEY_{i}" for i in range(12)]
    threads = [threading.Thread(target=update_env_file, args=(env_file, {k: "v"})) for k in keys]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    values = parse_env_text(env_file.read_text(encoding="utf-8"))
    assert all(values.get(k) == "v" for k in keys)
    assert env_file.read_text(encoding="utf-8").startswith("# keep me\n")


def test_env_lock_times_out_instead_of_hanging(tmp_path):
    from herdr_slackbot.setup_cmd import EnvLockTimeout, env_lock

    with env_lock(tmp_path):
        with pytest.raises(EnvLockTimeout):
            with env_lock(tmp_path, timeout=0.2):
                pass


def test_run_setup_holds_the_env_lock(tmp_path, monkeypatch):
    import herdr_slackbot.setup_cmd as S

    held = []
    real = S._run_setup_locked
    monkeypatch.setattr(S, "_run_setup_locked", lambda *a: held.append(_lock_is_held(tmp_path)) or real(*a))
    run_setup(tmp_path, username="me")
    assert held == [True]


def _lock_is_held(config_dir):
    from herdr_slackbot.setup_cmd import EnvLockTimeout, env_lock

    try:
        with env_lock(config_dir, timeout=0.05):
            return False
    except EnvLockTimeout:
        return True
