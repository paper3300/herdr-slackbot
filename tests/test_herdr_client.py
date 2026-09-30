import io
import json

import pytest

from herdr_slackbot import herdr_client as hc
from herdr_slackbot.herdr_client import (
    AutoTransport,
    CliTransport,
    HerdrClient,
    HerdrError,
    HerdrOutcomeUnknown,
    HerdrUnavailable,
    PipeTransport,
    cli_args,
    pipe_path,
)


def test_pipe_path():
    assert pipe_path(r"C:\Users\u\AppData\Roaming\herdr\herdr.sock") == \
        "\\\\.\\pipe\\C:\\Users\\u\\AppData\\Roaming\\herdr\\herdr.sock"
    assert pipe_path("\\\\.\\pipe\\x") == "\\\\.\\pipe\\x"


def test_unwrap():
    assert hc._unwrap({"id": "1", "result": {"type": "pong"}}, "ping") == {"type": "pong"}
    with pytest.raises(HerdrError) as exc:
        hc._unwrap({"id": "1", "error": {"code": "agent_not_found", "message": "agent target x not found"}}, "agent.get")
    assert exc.value.code == "agent_not_found"
    with pytest.raises(HerdrError):
        hc._unwrap({"id": "1"}, "ping")


def test_read_line_handles_chunks_and_leftovers():
    handle = io.BytesIO(b'{"a":1}\n{"b":')
    handle2 = io.BytesIO(b'2}\n')
    buf = bytearray()
    assert hc._read_line(handle, buf) == b'{"a":1}'
    assert hc._read_line(handle, buf) == b'{"b":'  # EOF flushes remainder
    assert hc._read_line(handle, buf) is None
    buf = bytearray(b'{"x"')
    assert hc._read_line(io.BytesIO(b':1}\nrest'), buf) == b'{"x":1}'
    assert bytes(buf) == b"rest"
    assert hc._read_line(handle2, bytearray()) == b"2}"


@pytest.mark.parametrize("method,params,expected", [
    ("agent.list", {}, ["agent", "list"]),
    ("agent.get", {"target": "coder"}, ["agent", "get", "coder"]),
    ("agent.read", {"target": "w1:p1", "source": "recent_unwrapped", "lines": 200},
     ["agent", "read", "w1:p1", "--source", "recent-unwrapped", "--lines", "200"]),
    ("agent.start", {"name": "slack-1", "kind": "claude", "pane_id": "w1:p2",
                     "args": ["--model", "opus"], "timeout_ms": 60000},
     ["agent", "start", "slack-1", "--kind", "claude", "--pane", "w1:p2", "--timeout", "60000", "--", "--model", "opus"]),
    ("agent.prompt", {"target": "slack-1", "text": "hi\nthere"}, ["agent", "prompt", "slack-1", "hi\nthere"]),
    ("agent.prompt", {"target": "a", "text": "t", "wait": {"timeout_ms": 5, "until": ["done"]}},
     ["agent", "prompt", "a", "t", "--wait", "--until", "done", "--timeout", "5"]),
    ("agent.wait", {"target": "a", "until": ["idle", "done"]}, ["agent", "wait", "a", "--until", "idle", "--until", "done"]),
    ("agent.send_keys", {"target": "a", "keys": ["esc"]}, ["agent", "send-keys", "a", "esc"]),
    ("tab.create", {"workspace_id": "w1", "label": "slack-1 hi", "cwd": "D:\\x", "focus": False},
     ["tab", "create", "--workspace", "w1", "--cwd", "D:\\x", "--label", "slack-1 hi", "--no-focus"]),
    ("tab.list", {"workspace_id": "w1"}, ["tab", "list", "--workspace", "w1"]),
    ("pane.list", {}, ["pane", "list"]),
    ("pane.get", {"pane_id": "w1:p1"}, ["pane", "get", "w1:p1"]),
    ("workspace.list", {}, ["workspace", "list"]),
])
def test_cli_args(method, params, expected):
    assert cli_args(method, params) == expected


def test_cli_args_unsupported():
    with pytest.raises(HerdrUnavailable):
        cli_args("events.subscribe", {})


class FakeProc:
    def __init__(self, returncode, stdout=b"", stderr=b""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


def test_cli_transport_success_error_and_plain_read(monkeypatch):
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        if argv[1:3] == ["agent", "get"]:
            return FakeProc(1, stderr=json.dumps({"error": {"code": "agent_not_found", "message": "nope"},
                                                  "id": "cli:agent:get"}).encode())
        if argv[1:3] == ["agent", "read"]:
            return FakeProc(0, stdout="● 한글\r\n✻ Worked for 1s\r\n".encode("utf-8"))
        return FakeProc(0, stdout=json.dumps({"id": "cli", "result": {"type": "agent_list", "agents": []}}).encode())

    monkeypatch.setattr(hc.subprocess, "run", fake_run)
    client = HerdrClient(CliTransport("herdr.exe"))
    assert client.list_agents() == []
    assert client.find_agent("x") is None
    assert client.read_agent_once("a", 10) == "● 한글\n✻ Worked for 1s\n"
    assert calls[0][0] == "herdr.exe"


def test_cli_transport_missing_binary(monkeypatch):
    def boom(*a, **k):
        raise FileNotFoundError("herdr")
    monkeypatch.setattr(hc.subprocess, "run", boom)
    with pytest.raises(HerdrUnavailable):
        CliTransport().request("agent.list")


class ScriptedTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def request(self, method, params=None):
        self.requests.append((method, params))
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def _read(text):
    return {"type": "pane_read", "read": {"text": text}}


def test_read_agent_retries_viewport_only_reads():
    viewport = "\n".join(["x"] * 50)
    full = "\n".join(["y"] * 150)
    t = ScriptedTransport([_read(viewport), _read(full), _read(viewport)])
    r = HerdrClient(t).read_agent("a", lines=200, attempts=3, retry_delay=0)
    assert r.text == full and r.attempts == 3
    assert t.requests[0] == ("agent.read", {"target": "a", "source": "recent_unwrapped", "lines": 200})


def test_read_agent_stops_when_banner_or_enough_lines():
    banner = " ▐▛███▜▌   Claude Code v2.1.285\n● hi"
    t = ScriptedTransport([_read(banner)])
    assert HerdrClient(t).read_agent("a", lines=200, retry_delay=0).attempts == 1
    t = ScriptedTransport([_read("\n".join(["z"] * 20))])
    assert HerdrClient(t).read_agent("a", lines=20, retry_delay=0).attempts == 1


def test_read_agent_propagates_not_idle():
    t = ScriptedTransport([HerdrError("agent_not_idle", "working")])
    with pytest.raises(HerdrError) as exc:
        HerdrClient(t).read_agent("a", retry_delay=0)
    assert exc.value.code == "agent_not_idle"


def test_client_request_shapes():
    t = ScriptedTransport([
        {"type": "agent_started", "agent": {"name": "slack-1"}, "argv": ["claude"]},
        {"type": "agent_prompted", "agent": {"agent_status": "working"}},
        {"type": "tab_created", "tab": {"tab_id": "w1:t2"}, "root_pane": {"pane_id": "w1:p9"}},
    ])
    c = HerdrClient(t)
    c.start_agent("slack-1", "claude", "w1:p9", ["--model", "opus"])
    c.prompt_agent("slack-1", "hello")
    c.create_tab("w1", "slack-1 hello", None)
    assert t.requests == [
        ("agent.start", {"name": "slack-1", "kind": "claude", "pane_id": "w1:p9", "args": ["--model", "opus"]}),
        ("agent.prompt", {"target": "slack-1", "text": "hello"}),
        ("tab.create", {"workspace_id": "w1", "label": "slack-1 hello", "cwd": None, "focus": False}),
    ]


def test_auto_transport_falls_back_to_cli(monkeypatch):
    auto = AutoTransport("definitely-missing-herdr-pipe-for-tests", "herdr")
    auto.pipe.busy_retries = 0
    monkeypatch.setattr(auto.cli, "request", lambda m, p=None: {"type": "via-cli", "method": m})
    assert auto.request("agent.list") == {"type": "via-cli", "method": "agent.list"}
    with pytest.raises(HerdrUnavailable):
        auto.open_stream("events.subscribe", {"subscriptions": []})


def test_pipe_transport_requires_socket():
    with pytest.raises(HerdrUnavailable):
        PipeTransport("")


# --- review #1: no blind resend after an unknown outcome -----------------------------

class EofAfterWriteHandle:
    """Pipe handle that accepts the request and then closes without replying."""

    def __init__(self, fail_write=False):
        self.written = b""
        self.fail_write = fail_write

    def write(self, data):
        if self.fail_write:
            raise OSError(232, "The pipe is being closed")
        self.written += data
        return len(data)

    def read(self, n):
        return b""

    def close(self):
        pass


def _auto_with_fake_pipe(monkeypatch, handle):
    auto = AutoTransport("fake-pipe", "herdr")
    monkeypatch.setattr(auto.pipe, "_open", lambda: handle)
    cli_calls = []
    monkeypatch.setattr(auto.cli, "request", lambda m, p=None: cli_calls.append(m) or {"type": "cli"})
    return auto, cli_calls


@pytest.mark.parametrize("method,params", [
    ("agent.prompt", {"target": "a", "text": "hi"}),
    ("tab.create", {"workspace_id": "w1"}),
    ("agent.start", {"name": "n", "kind": "claude", "pane_id": "w1:p1"}),
])
def test_review1_eof_after_mutation_write_does_not_invoke_cli(monkeypatch, method, params):
    handle = EofAfterWriteHandle()
    auto, cli_calls = _auto_with_fake_pipe(monkeypatch, handle)
    with pytest.raises(HerdrOutcomeUnknown):
        auto.request(method, params)
    assert handle.written  # the request did reach the pipe
    assert cli_calls == []


def test_review1_write_error_on_mutation_is_unknown_not_resent(monkeypatch):
    auto, cli_calls = _auto_with_fake_pipe(monkeypatch, EofAfterWriteHandle(fail_write=True))
    with pytest.raises(HerdrOutcomeUnknown):
        auto.request("agent.prompt", {"target": "a", "text": "hi"})
    assert cli_calls == []


def test_review1_read_only_request_may_retry_via_cli(monkeypatch):
    auto, cli_calls = _auto_with_fake_pipe(monkeypatch, EofAfterWriteHandle())
    assert auto.request("agent.list") == {"type": "cli"}
    assert cli_calls == ["agent.list"]


def test_review1_unsent_request_falls_back_for_mutations(monkeypatch):
    auto = AutoTransport("fake-pipe", "herdr")

    def cannot_open():
        raise HerdrUnavailable("no pipe")
    monkeypatch.setattr(auto.pipe, "_open", cannot_open)
    monkeypatch.setattr(auto.cli, "request", lambda m, p=None: {"type": "cli", "m": m})
    assert auto.request("agent.prompt", {"target": "a", "text": "x"}) == {"type": "cli", "m": "agent.prompt"}


def test_review1_cli_timeout_is_unknown_outcome(monkeypatch):
    def slow(*a, **k):
        raise hc.subprocess.TimeoutExpired("herdr", 1)
    monkeypatch.setattr(hc.subprocess, "run", slow)
    with pytest.raises(HerdrOutcomeUnknown):
        CliTransport(timeout=1).request("agent.prompt", {"target": "a", "text": "x"})
