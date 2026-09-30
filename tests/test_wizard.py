"""Setup wizard (release R1) with fake input, a fake Slack WebClient and a fake bridge start."""

import json
import os
import urllib.parse

import pytest
from slack_sdk.errors import SlackApiError

from fakes import Clock
from herdr_slackbot import plugin as P
from herdr_slackbot.config import load_env_file
from herdr_slackbot.pairing import pairing_path
from herdr_slackbot.slack_manifest import build_manifest
from herdr_slackbot.wizard import (
    MAX_BROWSER_URL,
    NOT_INTERACTIVE_TEXT,
    SLACK_NEW_APP_URL,
    TokenError,
    Wizard,
    manifest_url,
    mask_token,
    stdin_is_interactive,
    verify_app_token,
    verify_bot_token,
)

GOOD_APP = "xapp-1-A111-222-aaaabbbbccccdddd"
GOOD_BOT = "xoxb-111-222-eeeeffffgggghhhh"


# --- pure helpers -----------------------------------------------------------------------------

def test_manifest_url_roundtrips_and_fits_the_browser():
    manifest = build_manifest("/herdr-kim", "Herdr (김)", "kim")
    url = manifest_url(manifest)
    assert url is not None and url.startswith(SLACK_NEW_APP_URL)
    assert len(url) < MAX_BROWSER_URL
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
    assert query["new_app"] == ["1"]
    assert json.loads(query["manifest_json"][0]) == manifest


def test_manifest_url_too_long_falls_back():
    manifest = build_manifest("/herdr-kim", "Herdr (kim)", "kim")
    assert manifest_url(manifest, limit=500) is None
    manifest["display_information"]["description"] = "x" * 3000
    assert manifest_url(manifest) is None


def test_mask_token_never_shows_the_whole_token():
    assert mask_token(GOOD_BOT) == "xoxb-…hhhh"
    assert mask_token("xapp-12") == "xapp-…"
    assert GOOD_APP not in mask_token(GOOD_APP)


def test_stdin_is_interactive_rejects_pipes_and_nul():
    class NotTty:
        def isatty(self):
            return False

    assert not stdin_is_interactive(NotTty())
    with open(os.devnull) as nul:  # Windows: isatty() is True for NUL, the console-mode check is not
        assert not stdin_is_interactive(nul)


class FakeResp(dict):
    pass


class FakeWebClient:
    """auth.test / apps.connections.open answers keyed by token."""

    good_bot = {GOOD_BOT}
    good_app = {GOOD_APP}
    calls: list = []

    def __init__(self, token):
        self.token = token

    def auth_test(self):
        FakeWebClient.calls.append(("auth.test", self.token))
        if self.token not in self.good_bot:
            raise SlackApiError("bad", FakeResp(ok=False, error="invalid_auth"))
        return FakeResp(ok=True, team="Example", user="herdr_kim", bot_id="B1")

    def apps_connections_open(self, app_token):
        FakeWebClient.calls.append(("apps.connections.open", app_token))
        if app_token not in self.good_app:
            raise SlackApiError("bad", FakeResp(ok=False, error="invalid_auth"))
        return FakeResp(ok=True, url="wss://example")


@pytest.fixture(autouse=True)
def _reset_calls():
    FakeWebClient.calls = []


def test_verify_helpers():
    assert "workspace Example" in verify_bot_token(GOOD_BOT, FakeWebClient)
    with pytest.raises(TokenError, match="invalid_auth"):
        verify_bot_token("xoxb-nope", FakeWebClient)
    verify_app_token(GOOD_APP, FakeWebClient)
    with pytest.raises(TokenError, match="invalid_auth"):
        verify_app_token("xapp-nope", FakeWebClient)

    class NoBot(FakeWebClient):
        def auth_test(self):
            return FakeResp(ok=True, user="someone")  # a user token

    with pytest.raises(TokenError, match="not a bot token"):
        verify_bot_token(GOOD_BOT, NoBot)

    class Offline(FakeWebClient):
        def auth_test(self):
            raise OSError("network down")

    with pytest.raises(TokenError, match="OSError: network down"):
        verify_bot_token(GOOD_BOT, Offline)


# --- the wizard -------------------------------------------------------------------------------------

class Script:
    """Answers prompts in order; records every prompt."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.prompts = []

    def __call__(self, prompt):
        self.prompts.append(prompt)
        if not self.answers:
            raise AssertionError(f"unexpected prompt {prompt!r}")
        answer = self.answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer


class FakeBridgeStart:
    """Plays the bridge: on start it issues a pairing code; `pair_after` sleeps later it writes the owner."""

    def __init__(self, clock, pair_after=2, code="424242", owner="UKIM"):
        self.clock, self.pair_after, self.code, self.owner = clock, pair_after, code, owner
        self.starts = []
        self.sleeps = 0
        self.config_dir = None
        self.ready = None  # the ready marker the bridge would write once owner mode runs
        self.serves = True  # False: the bridge never reaches owner mode

    def readiness(self, state_dir):
        return self.ready

    def _became_ready(self, owner):
        if self.serves:
            self.ready = {"owner": owner, "at": self.clock(), "pid": 1}

    def start(self, config_dir, out):
        self.starts.append(config_dir)
        self.config_dir = config_dir
        self.ready = None  # restarted: the old marker is gone
        owner = load_env_file(config_dir / ".env").get("SLACK_OWNER_USER_ID")
        if owner:
            self._became_ready(owner)
            return 0
        state = config_dir / "state"
        state.mkdir(exist_ok=True)
        pairing_path(state).write_text(json.dumps({"code": self.code, "created": self.clock(), "attempts": 0}),
                                       encoding="utf-8")
        return 0

    def sleep(self, seconds):
        self.clock.now += seconds
        self.sleeps += 1
        if self.owner and self.sleeps >= self.pair_after:
            from herdr_slackbot.setup_cmd import update_env_file

            update_env_file(self.config_dir / ".env", {"SLACK_OWNER_USER_ID": self.owner})
            self._became_ready(self.owner)


def make_wizard(tmp_path, answers, clock=None, bridge=None, **kw):
    clock = clock or Clock()
    bridge = bridge or FakeBridgeStart(clock)
    out, opened = [], []
    ask = Script(answers)
    wiz = Wizard(tmp_path, username="kim", ask=ask, out=out.append, open_browser=lambda url: opened.append(url) or True,
                 client_factory=FakeWebClient, start_bridge=bridge.start, interactive=lambda: True, clock=clock,
                 sleep=bridge.sleep, readiness=bridge.readiness, **kw)
    return wiz, ask, out, opened, bridge


def test_refuses_without_a_terminal(tmp_path):
    out = []
    wiz = Wizard(tmp_path, ask=lambda p: pytest.fail("must not prompt"), out=out.append, interactive=lambda: False)
    assert wiz.run() == 2
    assert out == [NOT_INTERACTIVE_TEXT] and not (tmp_path / ".env").exists()


def test_full_first_run(tmp_path):
    wiz, ask, out, opened, bridge = make_wizard(tmp_path, [
        "",                  # slash command: keep the default
        "Herdr (Kim PC)",    # bot name
        "",                  # create the app now? default yes
        "xoxb-wrong-kind",   # app token prompt: wrong prefix
        "xapp-1-bad-token",  # rejected by Slack
        f'  "{GOOD_APP}" ',  # quotes / spaces from copy-paste are fine
        "xoxb-not-valid-123",
        GOOD_BOT,
    ])
    assert wiz.run() == 0
    env = load_env_file(tmp_path / ".env")
    assert env["SLASH_COMMAND"] == "/herdr-kim" and env["BOT_DISPLAY_NAME"] == "Herdr (Kim PC)"
    assert env["SLACK_APP_TOKEN"] == GOOD_APP and env["SLACK_BOT_TOKEN"] == GOOD_BOT
    assert env["SLACK_OWNER_USER_ID"] == "UKIM"
    text = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "# App-Level Token" in text  # the skeleton's comments survive the in-place writes
    # the browser got the prefilled manifest for the answers given
    (url,) = opened
    manifest = json.loads(urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)["manifest_json"][0])
    assert manifest["features"]["slash_commands"][0]["command"] == "/herdr-kim"
    assert manifest["features"]["bot_user"]["display_name"] == "Herdr (Kim PC)"
    joined = "\n".join(out)
    assert url in joined and str(tmp_path / "slack-app-manifest.json") in joined
    assert "connections:write" in joined and "Install to Workspace" in joined
    assert "invalid_auth" in joined and "not a xapp- token" in joined
    assert GOOD_APP not in joined and GOOD_BOT not in joined  # tokens are never echoed in full
    assert "pairing code:  424242" in joined and "/herdr-kim pair 424242" in joined and "paired ✅" in joined
    assert "Done" in joined and "herdr plugin action invoke restart" in joined
    assert bridge.starts == [tmp_path]
    assert ("auth.test", GOOD_BOT) in FakeWebClient.calls and ("apps.connections.open", GOOD_APP) in FakeWebClient.calls


def test_existing_values_are_not_asked_again(tmp_path):
    (tmp_path / ".env").write_text(f"SLASH_COMMAND=/herdr-old\nBOT_DISPLAY_NAME=Old\nSLACK_APP_TOKEN={GOOD_APP}\n"
                                   f"SLACK_BOT_TOKEN={GOOD_BOT}\nSLACK_OWNER_USER_ID=UOLD\n", encoding="utf-8")
    wiz, ask, out, opened, bridge = make_wizard(tmp_path, ["n", "n"])  # keep both valid tokens
    assert wiz.run() == 0
    assert all("replace" in p for p in ask.prompts)
    assert opened == [] and bridge.starts == [tmp_path]
    env = load_env_file(tmp_path / ".env")
    assert (env["SLASH_COMMAND"], env["SLACK_OWNER_USER_ID"], env["SLACK_BOT_TOKEN"]) == ("/herdr-old", "UOLD", GOOD_BOT)
    assert "already paired" in "\n".join(out)


def test_invalid_stored_token_is_asked_again_and_the_other_kept(tmp_path):
    (tmp_path / ".env").write_text(f"SLASH_COMMAND=/herdr-kim\nBOT_DISPLAY_NAME=K\nSLACK_APP_TOKEN=xapp-revoked-1\n"
                                   f"SLACK_BOT_TOKEN={GOOD_BOT}\n", encoding="utf-8")
    wiz, ask, out, opened, _ = make_wizard(tmp_path, [
        "",        # keep the bot token (replace? default no)
        "",        # create the app? default no when only one token is needed
        GOOD_APP,
    ])
    assert wiz.run() == 0
    assert opened == []
    assert "does not work: invalid_auth" in "\n".join(out)
    env = load_env_file(tmp_path / ".env")
    assert env["SLACK_APP_TOKEN"] == GOOD_APP and env["SLACK_BOT_TOKEN"] == GOOD_BOT


def test_replace_a_valid_token(tmp_path):
    other = "xoxb-999-888-zzzzyyyyxxxxwwww"
    FakeWebClient.good_bot = {GOOD_BOT, other}
    try:
        (tmp_path / ".env").write_text(f"SLASH_COMMAND=/herdr-kim\nBOT_DISPLAY_NAME=K\nSLACK_APP_TOKEN={GOOD_APP}\n"
                                       f"SLACK_BOT_TOKEN={GOOD_BOT}\nSLACK_OWNER_USER_ID=U1\n", encoding="utf-8")
        wiz, *_ = make_wizard(tmp_path, ["n", "y", "n", other])
        assert wiz.run() == 0
        assert load_env_file(tmp_path / ".env")["SLACK_BOT_TOKEN"] == other
    finally:
        FakeWebClient.good_bot = {GOOD_BOT}


def test_invalid_slash_command_is_asked_again(tmp_path):
    (tmp_path / ".env").write_text(f"BOT_DISPLAY_NAME=K\nSLACK_APP_TOKEN={GOOD_APP}\nSLACK_BOT_TOKEN={GOOD_BOT}\n"
                                   "SLACK_OWNER_USER_ID=U1\n", encoding="utf-8")
    wiz, ask, out, *_ = make_wizard(tmp_path, ["bad name!", "herdr-kim2", "n", "n"])
    assert wiz.run() == 0
    assert load_env_file(tmp_path / ".env")["SLASH_COMMAND"] == "/herdr-kim2"
    manifest = json.loads((tmp_path / "slack-app-manifest.json").read_text(encoding="utf-8"))
    assert manifest["features"]["slash_commands"][0]["command"] == "/herdr-kim2"
    assert "may only contain" in "\n".join(out)


def test_manual_fallback_when_url_too_long(tmp_path, monkeypatch):
    import herdr_slackbot.wizard as W

    monkeypatch.setattr(W, "manifest_url", lambda manifest, limit=W.MAX_BROWSER_URL: None)
    wiz, ask, out, opened, _ = make_wizard(tmp_path, ["", "", "y", GOOD_APP, GOOD_BOT])
    assert wiz.run() == 0
    assert opened == []
    joined = "\n".join(out)
    assert "too long for a browser link" in joined and "From a manifest" in joined
    assert str(tmp_path / "slack-app-manifest.json") in joined


def test_pairing_timeout_and_stale_code(tmp_path):
    clock = Clock()
    bridge = FakeBridgeStart(clock, owner=None)
    (tmp_path / "state").mkdir()
    pairing_path(tmp_path / "state").write_text(json.dumps({"code": "111111", "created": clock.now - 3600}),
                                                encoding="utf-8")
    real_start = bridge.start

    def start_without_code(config_dir, out):
        bridge.config_dir = config_dir
        bridge.starts.append(config_dir)
        return 0  # the bridge never shows a code (e.g. it failed in its pane)

    wiz, ask, out, *_ = make_wizard(tmp_path, ["", "", "n", GOOD_APP, GOOD_BOT], clock=clock, bridge=bridge,
                                    pairing_wait=120)
    wiz.start_bridge = start_without_code
    assert wiz.run() == 1
    joined = "\n".join(out)
    assert "111111" not in joined  # the leftover code of an earlier bridge is not shown
    assert "no pairing code yet" in joined and "still not paired" in joined
    assert "Not finished" in joined and "pair <code>" in joined
    assert real_start  # (the default fake start is not used here)


def test_ctrl_c_while_waiting_for_pairing(tmp_path):
    clock = Clock()
    bridge = FakeBridgeStart(clock, owner=None)

    def interrupting_sleep(seconds):
        raise KeyboardInterrupt

    wiz, ask, out, *_ = make_wizard(tmp_path, ["", "", "n", GOOD_APP, GOOD_BOT], clock=clock, bridge=bridge)
    wiz.sleep = interrupting_sleep
    assert wiz.run() == 1
    joined = "\n".join(out)
    assert "stopped waiting" in joined and "424242" in joined


def test_ctrl_c_at_a_prompt_keeps_earlier_answers(tmp_path):
    wiz, ask, out, *_ = make_wizard(tmp_path, ["", "Herdr (K)", "n", KeyboardInterrupt()])
    assert wiz.run() == 130
    assert load_env_file(tmp_path / ".env")["BOT_DISPLAY_NAME"] == "Herdr (K)"
    assert "wizard stopped" in out[-1]


def test_bridge_start_failure(tmp_path):
    wiz, ask, out, _, bridge = make_wizard(tmp_path, ["", "", "n", GOOD_APP, GOOD_BOT])
    wiz.start_bridge = lambda config_dir, out: 1
    assert wiz.run() == 1
    assert "did not start" in "\n".join(out)


def test_main_wizard_refuses_under_pytest_stdin(tmp_path, capsys):
    from herdr_slackbot import __main__ as main_mod

    assert main_mod.main(["wizard", "--config-dir", str(tmp_path)]) == 2
    assert "needs a terminal" in capsys.readouterr().out
    assert not (tmp_path / ".env").exists()


# --- the `setup` plugin action -------------------------------------------------------------------------

class TabCli:
    def __init__(self):
        self.calls = []

    def json(self, *args):
        self.calls.append(args)
        if args[:2] == ("tab", "create"):
            return {"root_pane": {"pane_id": "w3:p9"}}
        if args[:2] == ("pane", "process-info"):
            return {"process_info": {"shell_pid": 1, "foreground_processes": [{"name": "powershell.exe"}]}}
        raise AssertionError(args)

    def run(self, *args, mutating=False):
        self.calls.append(args)
        return ""


def test_open_wizard_types_the_wizard_into_a_new_focused_tab(tmp_path):
    cli, out = TabCli(), []
    ops = P.PluginOps(cli, tmp_path, plugin_root=tmp_path / "root", python_exe=r"C:\p\.venv\Scripts\python.exe",
                      out=out.append, env={})
    assert ops.open_wizard() == 0
    create = cli.calls[0]
    assert create[:2] == ("tab", "create") and "--focus" in create and "--workspace" not in create  # current ws
    (run,) = [c for c in cli.calls if c[:2] == ("pane", "run")]
    assert run[2] == "w3:p9"
    assert run[3] == (f"Set-Location -LiteralPath '{tmp_path / 'root'}'; & 'C:\\p\\.venv\\Scripts\\python.exe' "
                      f"-m herdr_slackbot wizard --config-dir '{tmp_path}'")
    assert "w3:p9" in out[-1]


def test_bridge_command_unchanged_by_the_refactor():
    assert P.bridge_command("cmd", "py.exe", "R", "C", "abc") == 'cd /d "R" && "py.exe" -m herdr_slackbot run --config-dir "C" --launch-id abc'
    assert P.module_command("bash", "C:\\py", "C:\\R", "C:\\C", "wizard") == \
        "cd 'C:/R' && 'C:/py' -m herdr_slackbot wizard --config-dir 'C:/C'"


# --- release review fixes ------------------------------------------------------------------------------

def test_verification_errors_never_print_the_token(tmp_path):
    """Review 5: exception text from a proxy / the SDK may quote the token."""
    weird = "xapp-1-A_SECRET.part"  # characters outside the generic token pattern

    class Leaky(FakeWebClient):
        def apps_connections_open(self, app_token):
            raise RuntimeError(f"proxy rejected {app_token} (also saw xoxb-9-9-otherthing)")

        def auth_test(self):
            raise OSError(f"tunnel refused token={self.token}")

    with pytest.raises(TokenError) as err:
        verify_app_token(weird, Leaky)
    assert weird not in str(err.value) and "SECRET" not in str(err.value) and "otherthing" not in str(err.value)
    assert "[redacted]" in str(err.value)
    with pytest.raises(TokenError) as err:
        verify_bot_token(GOOD_BOT, Leaky)
    assert GOOD_BOT not in str(err.value)

    # stored tokens that fail and pasted tokens that fail: nothing of the token reaches the terminal
    (tmp_path / ".env").write_text(f"SLASH_COMMAND=/herdr-kim\nBOT_DISPLAY_NAME=K\nSLACK_APP_TOKEN={weird}\n"
                                   "SLACK_OWNER_USER_ID=U1\n", encoding="utf-8")
    wiz, ask, out, *_ = make_wizard(tmp_path, ["n", weird, KeyboardInterrupt()])
    wiz.client_factory = Leaky
    wiz.run()
    joined = "\n".join(out)
    assert "does not work" in joined and "Slack rejected" in joined
    assert weird not in joined and "SECRET" not in joined


def test_pairing_wait_follows_activation_until_done(tmp_path):
    """Review 4: the owner lands in .env before owner mode runs; wait for the bridge to finish."""
    clock = Clock()
    bridge = FakeBridgeStart(clock, owner=None)
    steps = {"n": 0}

    def sleep(seconds):
        clock.now += seconds
        steps["n"] += 1
        state = tmp_path / "state"
        if steps["n"] == 2:  # code accepted: owner saved, activation running
            from herdr_slackbot.setup_cmd import update_env_file

            update_env_file(tmp_path / ".env", {"SLACK_OWNER_USER_ID": "UKIM"})
            pairing_path(state).write_text(json.dumps({"state": "activating", "at": clock.now}), encoding="utf-8")
        if steps["n"] == 4:  # owner mode running: marker written, then pairing.json removed
            bridge.ready = {"owner": "UKIM", "at": clock.now, "pid": 1}
            pairing_path(state).unlink()

    wiz, ask, out, *_ = make_wizard(tmp_path, ["", "", "n", GOOD_APP, GOOD_BOT], clock=clock, bridge=bridge)
    wiz.sleep = sleep
    assert wiz.run() == 0
    joined = "\n".join(out)
    assert "bridge is starting for you" in joined and "paired ✅" in joined
    assert joined.index("starting for you") < joined.index("paired ✅")


def test_pairing_wait_reports_failed_activation(tmp_path):
    clock = Clock()
    bridge = FakeBridgeStart(clock, owner=None)

    def sleep(seconds):
        clock.now += seconds
        from herdr_slackbot.setup_cmd import update_env_file

        update_env_file(tmp_path / ".env", {"SLACK_OWNER_USER_ID": "UKIM"})
        pairing_path(tmp_path / "state").write_text(json.dumps({"state": "failed", "at": clock.now}),
                                                    encoding="utf-8")

    wiz, ask, out, *_ = make_wizard(tmp_path, ["", "", "n", GOOD_APP, GOOD_BOT], clock=clock, bridge=bridge)
    wiz.sleep = sleep
    assert wiz.run() == 1
    joined = "\n".join(out)
    assert "could not start for you" in joined and "invoke restart" in joined and "paired ✅" not in joined


# --- readiness (release recheck) ------------------------------------------------------------------------

PAIRED_ENV = (f"SLASH_COMMAND=/herdr-kim\nBOT_DISPLAY_NAME=K\nSLACK_APP_TOKEN={GOOD_APP}\n"
              f"SLACK_BOT_TOKEN={GOOD_BOT}\nSLACK_OWNER_USER_ID=UKIM\n")


def test_already_paired_but_bridge_never_serves_is_not_success(tmp_path):
    """Recheck: an owner key in .env is not proof; a rerun must not say "paired" while the restarted
    bridge fails to reach owner mode."""
    (tmp_path / ".env").write_text(PAIRED_ENV, encoding="utf-8")
    clock = Clock()
    bridge = FakeBridgeStart(clock)
    bridge.serves = False
    wiz, ask, out, *_ = make_wizard(tmp_path, ["n", "n"], clock=clock, bridge=bridge, ready_wait=30)
    assert wiz.run() == 1
    joined = "\n".join(out)
    assert "did not report ready within 30s" in joined and "paired ✅" not in joined
    assert "Not finished" in joined and "not serving yet" in joined and "pair <code>" not in joined


def test_already_paired_and_serving_is_success(tmp_path):
    (tmp_path / ".env").write_text(PAIRED_ENV, encoding="utf-8")
    wiz, ask, out, *_ = make_wizard(tmp_path, ["n", "n"])
    assert wiz.run() == 0
    assert "the bridge is running for Slack user UKIM" in "\n".join(out)


def test_already_paired_ignores_a_marker_from_before_the_restart(tmp_path):
    (tmp_path / ".env").write_text(PAIRED_ENV, encoding="utf-8")
    clock = Clock()
    bridge = FakeBridgeStart(clock, owner=None)  # no later "became ready"
    real_start = bridge.start

    def start(config_dir, out):
        code = real_start(config_dir, out)
        bridge.ready = {"owner": "UKIM", "at": clock.now - 600, "pid": 1}  # stale
        return code

    wiz, ask, out, *_ = make_wizard(tmp_path, ["n", "n"], clock=clock, bridge=bridge, ready_wait=10)
    wiz.start_bridge = start
    assert wiz.run() == 1


def test_ready_marker_for_another_owner_does_not_count(tmp_path):
    (tmp_path / ".env").write_text(PAIRED_ENV, encoding="utf-8")
    clock = Clock()
    bridge = FakeBridgeStart(clock)
    bridge._became_ready = lambda owner: setattr(bridge, "ready", {"owner": "UOLD", "at": clock.now})
    wiz, *_ = make_wizard(tmp_path, ["n", "n"], clock=clock, bridge=bridge, ready_wait=10)
    assert wiz.run() == 1


def test_default_readiness_uses_the_verified_marker(tmp_path, monkeypatch):
    import herdr_slackbot.plugin as P
    from herdr_slackbot.wizard import bridge_readiness

    monkeypatch.setattr(P, "verified_ready", lambda state_dir: {"owner": "U1", "dir": state_dir})
    assert bridge_readiness(tmp_path) == {"owner": "U1", "dir": tmp_path}
