import logging

from herdr_slackbot.__main__ import RedactSecrets
from herdr_slackbot.slack_transport import WebClientTransport


class FakeWebClient:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        def call(**kwargs):
            self.calls.append((name, kwargs))
            return {"channel": {"id": "D1"}, "ts": "9.9", "view": {"id": "V1"}}
        return call


def test_web_client_transport_maps_calls():
    web = FakeWebClient()
    t = WebClientTransport(web)
    assert t.open_dm("U1") == "D1"
    assert t.post_message("D1", "hi", [{"type": "section"}], "1.0") == "9.9"
    t.post_message("D1", "plain")
    t.update_message("D1", "1.0", "x", [])
    t.post_ephemeral("D1", "U1", "eph")
    t.upload_text_file("D1", "1.0", "a.md", "body", "A")
    assert t.open_view("TRIG", {"type": "modal"}) == "V1"
    t.update_view("V1", {"type": "modal"}, "h")
    names = [c[0] for c in web.calls]
    assert names == ["conversations_open", "chat_postMessage", "chat_postMessage", "chat_update",
                     "chat_postEphemeral", "files_upload_v2", "views_open", "views_update"]
    post = web.calls[1][1]
    assert post == {"channel": "D1", "text": "hi", "unfurl_links": False, "unfurl_media": False,
                    "blocks": [{"type": "section"}], "thread_ts": "1.0"}
    assert "thread_ts" not in web.calls[2][1] and "blocks" not in web.calls[2][1]
    assert web.calls[5][1] == {"channel": "D1", "filename": "a.md", "content": "body", "title": "A",
                               "thread_ts": "1.0"}
    assert web.calls[7][1] == {"view_id": "V1", "view": {"type": "modal"}, "hash": "h"}


def test_log_redaction():
    record = logging.LogRecord("x", logging.INFO, __file__, 1, "token %s and %s", ("xoxb-123-abc", "xapp-1-A-9"), None)
    RedactSecrets().filter(record)
    assert record.getMessage() == "token [redacted] and [redacted]"
    clean = logging.LogRecord("x", logging.INFO, __file__, 1, "hello %s", ("world",), None)
    RedactSecrets().filter(clean)
    assert clean.getMessage() == "hello world"


def test_run_refuses_without_slack_settings(tmp_path, monkeypatch, capsys):
    from herdr_slackbot import __main__ as main
    monkeypatch.setenv("HERDR_PLUGIN_CONFIG_DIR", str(tmp_path))
    for key in ("SLACK_BOT_TOKEN", "SLACK_APP_TOKEN", "SLACK_OWNER_USER_ID"):
        monkeypatch.delenv(key, raising=False)
    assert main.main([]) == 2
    assert "SLACK_BOT_TOKEN" in capsys.readouterr().err


# --- review #10: tracebacks are redacted too --------------------------------------------------------

def _render(exc_factory, stack=False):
    import io
    from herdr_slackbot.__main__ import RedactingFormatter

    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(RedactingFormatter("%(levelname)s %(message)s"))
    logger = logging.getLogger(f"redact-test-{id(stream)}")
    logger.propagate = False
    logger.addHandler(handler)
    try:
        exc_factory()
    except Exception:
        logger.exception("background task failed with xapp-1-A-SECRET", stack_info=stack)
    return stream.getvalue()


def test_review10_exception_traceback_is_redacted():
    def boom():
        raise RuntimeError("Slack said no for token xoxb-FAKE-REVIEW-TOKEN")
    out = _render(boom)
    assert "xoxb-FAKE-REVIEW-TOKEN" not in out and "xapp-1-A-SECRET" not in out
    assert "[redacted]" in out and "RuntimeError" in out


def test_review10_chained_exception_and_stack_are_redacted():
    def chained():
        try:
            raise ValueError("inner xoxp-USER-TOKEN-1")
        except ValueError as inner:
            raise RuntimeError("outer xapp-9-B-OTHER") from inner
    out = _render(chained, stack=True)
    for secret in ("xoxp-USER-TOKEN-1", "xapp-9-B-OTHER", "xapp-1-A-SECRET"):
        assert secret not in out
    assert "The above exception was the direct cause" in out


def test_setup_logging_uses_redacting_formatter(tmp_path):
    import dataclasses
    from herdr_slackbot.__main__ import RedactingFormatter, setup_logging
    from herdr_slackbot.config import load_config

    cfg = dataclasses.replace(load_config(env={"USERNAME": "me"}, config_dir=tmp_path), state_dir=tmp_path / "st")
    root = logging.getLogger()
    before = list(root.handlers)
    try:
        setup_logging(cfg)
        added = [h for h in root.handlers if h not in before]
        assert added and all(isinstance(h.formatter, RedactingFormatter) for h in added)
    finally:
        for h in [h for h in root.handlers if h not in before]:
            root.removeHandler(h)
            h.close()


# --- error classification / respond -----------------------------------------------------------------

def _api_error(error, status=200, headers=None):
    from slack_sdk.errors import SlackApiError
    from slack_sdk.web import SlackResponse

    resp = SlackResponse(client=None, http_verb="POST", api_url="x", req_args={}, data={"ok": False, "error": error},
                         headers=headers or {}, status_code=status)
    return SlackApiError("failed", resp)


def test_classify_slack_errors():
    from herdr_slackbot.slack_transport import SlackPermanentError, SlackTransientError, classify_slack_exception

    rl = classify_slack_exception(_api_error("ratelimited", 429, {"Retry-After": "12"}))
    assert isinstance(rl, SlackTransientError) and rl.retry_after == 12
    assert isinstance(classify_slack_exception(_api_error("invalid_blocks")), SlackPermanentError)
    assert isinstance(classify_slack_exception(_api_error("internal_error")), SlackTransientError)
    assert isinstance(classify_slack_exception(TimeoutError()), SlackTransientError)


def test_web_client_transport_raises_typed_errors():
    import pytest
    from herdr_slackbot.slack_transport import SlackPermanentError, SlackTransientError

    class Failing:
        def __init__(self, exc):
            self.exc = exc

        def chat_postMessage(self, **kw):
            raise self.exc

    with pytest.raises(SlackTransientError):
        WebClientTransport(Failing(ConnectionResetError())).post_message("D", "x")
    with pytest.raises(SlackPermanentError):
        WebClientTransport(Failing(_api_error("channel_not_found"))).post_message("D", "x")


def test_respond_via_response_url():
    import pytest
    from herdr_slackbot.slack_transport import SlackPermanentError, SlackTransientError

    sent = []

    class Hook:
        def __init__(self, url, status=200):
            self.url, self.status = url, status

        def send(self, **kw):
            sent.append((self.url, kw))
            return type("R", (), {"status_code": self.status, "body": "x"})()

    WebClientTransport(None, webhook_factory=Hook).respond("https://u", "hi", [{"type": "section"}])
    assert sent == [("https://u", {"text": "hi", "blocks": [{"type": "section"}], "response_type": "ephemeral",
                                   "replace_original": False})]
    with pytest.raises(SlackTransientError):
        WebClientTransport(None, webhook_factory=lambda u: Hook(u, 503)).respond("https://u", "hi")
    with pytest.raises(SlackPermanentError):
        WebClientTransport(None, webhook_factory=lambda u: Hook(u, 404)).respond("https://u", "hi")


# --- M2 recheck R9: unknown post outcomes, op markers, reconciliation -----------------------------

def test_r9_post_outcome_classification():
    import http.client
    import socket
    import urllib.error

    from herdr_slackbot.slack_transport import (
        SlackPermanentError,
        SlackTransientError,
        SlackUncertainError,
        classify_slack_exception as classify,
    )

    for exc in (socket.timeout("read"), ConnectionResetError(), http.client.IncompleteRead(b""),
                urllib.error.URLError(TimeoutError()), _api_error("internal_error", 500),
                _api_error("service_unavailable", 503)):
        assert type(classify(exc, mutating=True)) is SlackUncertainError, exc
        assert type(classify(exc, mutating=False)) in (SlackTransientError, SlackUncertainError)
    for exc in (ConnectionRefusedError(), urllib.error.URLError(socket.gaierror()), _api_error("ratelimited", 429)):
        assert type(classify(exc, mutating=True)) is SlackTransientError, exc
    assert type(classify(_api_error("channel_not_found"), mutating=True)) is SlackPermanentError
    assert type(classify(socket.timeout(), mutating=False)) is SlackTransientError  # reads: plain retry


def test_r9_post_with_op_carries_metadata_and_is_uncertain_on_timeout():
    import socket

    import pytest
    from herdr_slackbot.slack_transport import POST_MARKER_EVENT, SlackUncertainError

    web = FakeWebClient()
    WebClientTransport(web).post_message("D1", "hi", None, "1.0", op="result:t1")
    assert web.calls[0][1]["metadata"] == {"event_type": POST_MARKER_EVENT, "event_payload": {"op": "result:t1"}}

    class Timeout:
        def chat_postMessage(self, **kw):
            raise socket.timeout("read timed out")

    with pytest.raises(SlackUncertainError):
        WebClientTransport(Timeout()).post_message("D", "x", op="o")


def test_r9_find_message_by_marker():
    from herdr_slackbot.slack_transport import POST_MARKER_EVENT

    calls = []

    class Web:
        def _messages(self):
            return {"messages": [
                {"ts": "1.1", "metadata": {"event_type": "other", "event_payload": {"op": "x"}}},
                {"ts": "1.2", "metadata": {"event_type": POST_MARKER_EVENT, "event_payload": {"op": "other"}}},
                {"ts": "1.3", "metadata": {"event_type": POST_MARKER_EVENT, "event_payload": {"op": "x"}}},
            ]}

        def conversations_replies(self, **kw):
            calls.append(("replies", kw))
            return self._messages()

        def conversations_history(self, **kw):
            calls.append(("history", kw))
            return self._messages()

    t = WebClientTransport(Web())
    assert t.find_message("D1", "x", "1.0", oldest=100.5) == "1.3"
    assert calls[-1] == ("replies", {"channel": "D1", "ts": "1.0", "include_all_metadata": True, "limit": 200,
                                     "oldest": "100.500000"})
    assert t.find_message("D1", "missing") is None
    assert calls[-1][0] == "history" and "ts" not in calls[-1][1]


def test_r9_respond_unknown_outcome_is_uncertain():
    import pytest
    from herdr_slackbot.slack_transport import SlackTransientError, SlackUncertainError

    class Hook:
        def __init__(self, url, exc=None, status=200):
            self.exc, self.status = exc, status

        def send(self, **kw):
            if self.exc:
                raise self.exc
            return type("R", (), {"status_code": self.status, "body": ""})()

    with pytest.raises(SlackUncertainError):
        WebClientTransport(None, webhook_factory=lambda u: Hook(u, TimeoutError())).respond("https://u", "x")
    with pytest.raises(SlackUncertainError):
        WebClientTransport(None, webhook_factory=lambda u: Hook(u, status=502)).respond("https://u", "x")
    with pytest.raises(SlackTransientError) as info:
        WebClientTransport(None, webhook_factory=lambda u: Hook(u, ConnectionRefusedError())).respond("https://u", "x")
    assert not isinstance(info.value, SlackUncertainError)
