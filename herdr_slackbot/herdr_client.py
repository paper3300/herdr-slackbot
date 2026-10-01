"""Herdr socket API client.

Primary transport is the Herdr named pipe (NDJSON: one JSON request line, one
JSON response line). Every request uses a fresh pipe connection, which keeps
concurrent callers independent. `events.subscribe` keeps its connection open and
streams events (see `EventStream`).

When the pipe cannot be opened, requests fall back to the `herdr` CLI, which
prints the same `{"id", "result"}` envelope on stdout and `{"error"}` on stderr.

Protocol facts verified against Herdr 0.8.2 (protocol 20):
- request: {"id": str, "method": "agent.list", "params": {...}}  (params required, may be {})
- success: {"id": str, "result": {"type": "agent_list", ...}}
- error:   {"id": str, "error": {"code": str, "message": str}}
- read source enum over the socket is `recent_unwrapped` (the CLI spells it `recent-unwrapped`).
"""

from __future__ import annotations

import itertools
import json
import logging
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from typing import Any, Iterator, Sequence

log = logging.getLogger(__name__)

PIPE_PREFIX = "\\\\.\\pipe\\"
READ_CHUNK = 65536

# Windows error codes seen when opening a named pipe.
_ERROR_FILE_NOT_FOUND = 2
_ERROR_PIPE_BUSY = 231
_ERROR_OPERATION_ABORTED = 995


class HerdrError(Exception):
    """An error response from Herdr (`code` is Herdr's error code)."""

    def __init__(self, code: str, message: str = ""):
        super().__init__(f"{code}: {message}" if message else code)
        self.code = code
        self.message = message


class HerdrUnavailable(HerdrError):
    """Herdr could not be reached; the request was NOT submitted (safe to retry elsewhere)."""

    def __init__(self, message: str):
        super().__init__("unavailable", message)


class HerdrOutcomeUnknown(HerdrError):
    """The request was written but no valid reply arrived: it may or may not have run.

    Never blindly resend a mutating request after this; reconcile with live state instead.
    """

    def __init__(self, message: str):
        super().__init__("outcome_unknown", message)


# Requests without side effects: safe to repeat through the CLI after an unknown outcome.
READ_ONLY_METHODS = frozenset({
    "ping", "agent.list", "agent.get", "agent.read", "agent.explain", "workspace.list",
    "workspace.get", "tab.list", "tab.get", "pane.list", "pane.get", "pane.current",
    "pane.read", "pane.layout", "session.snapshot",
})


# Requests whose CLI prints nothing on success (`herdr pane send-text`; verified live on 0.8.2,
# while `agent send-keys` prints the usual {"result": {"type": "ok"}} envelope).
SILENT_CLI_METHODS = frozenset({"pane.send_text"})


def pipe_path(socket_path: str) -> str:
    if socket_path.startswith(PIPE_PREFIX):
        return socket_path
    return PIPE_PREFIX + socket_path


def _unwrap(response: dict, method: str) -> dict:
    if "error" in response:
        err = response.get("error") or {}
        raise HerdrError(str(err.get("code", "unknown")), str(err.get("message", "")))
    if "result" not in response:
        raise HerdrError("bad_response", f"{method}: response has neither result nor error")
    return response["result"]


def _read_line(handle, buf: bytearray) -> bytes | None:
    """Read one NDJSON line from a raw pipe handle, keeping leftovers in `buf`."""
    while True:
        nl = buf.find(b"\n")
        if nl >= 0:
            line = bytes(buf[:nl])
            del buf[: nl + 1]
            return line
        chunk = handle.read(READ_CHUNK)
        if not chunk:
            if buf:
                line = bytes(buf)
                buf.clear()
                return line
            return None
        buf.extend(chunk)


class PipeTransport:
    def __init__(self, socket_path: str, busy_retries: int = 20, busy_delay: float = 0.05):
        if not socket_path:
            raise HerdrUnavailable("HERDR_SOCKET_PATH is not set")
        self.path = pipe_path(socket_path)
        self.busy_retries = busy_retries
        self.busy_delay = busy_delay
        self._ids = itertools.count(1)

    def _open(self):
        last: OSError | None = None
        for _ in range(self.busy_retries + 1):
            try:
                return open(self.path, "r+b", buffering=0)
            except OSError as exc:
                last = exc
                winerror = getattr(exc, "winerror", None)
                # All pipe instances busy: the server spawns a new one shortly.
                if winerror == _ERROR_PIPE_BUSY or exc.errno == 22:
                    time.sleep(self.busy_delay)
                    continue
                break
        raise HerdrUnavailable(f"cannot open {self.path}: {last}")

    def _send(self, handle, method: str, params: dict) -> str:
        req_id = f"hs-{next(self._ids)}"
        payload = json.dumps({"id": req_id, "method": method, "params": params}, ensure_ascii=False)
        handle.write(payload.encode("utf-8") + b"\n")
        return req_id

    def request(self, method: str, params: dict | None = None) -> dict:
        handle = self._open()  # raises HerdrUnavailable: nothing was sent
        try:
            try:
                self._send(handle, method, params or {})
                line = _read_line(handle, bytearray())
            except OSError as exc:
                raise HerdrOutcomeUnknown(f"{method}: pipe failed after sending: {exc}") from exc
        finally:
            try:
                handle.close()
            except OSError:
                pass
        if not line:
            raise HerdrOutcomeUnknown(f"{method}: connection closed without a response")
        try:
            response = json.loads(line.decode("utf-8"))
        except ValueError as exc:
            raise HerdrOutcomeUnknown(f"{method}: undecodable response") from exc
        return _unwrap(response, method)

    def open_stream(self, method: str, params: dict) -> "EventStream":
        handle = self._open()
        try:
            self._send(handle, method, params)
            buf = bytearray()
            line = _read_line(handle, buf)
            if not line:
                raise HerdrUnavailable(f"{method}: connection closed without a response")
            ack = _unwrap(json.loads(line.decode("utf-8")), method)
        except BaseException:
            handle.close()
            raise
        return EventStream(handle, buf, ack)


class EventStream:
    """A long-lived `events.subscribe` connection yielding decoded event envelopes.

    Iterate from one thread; `close()` may be called from any other thread and
    cancels the blocking pipe read.
    """

    def __init__(self, handle, buf: bytearray, ack: dict):
        self._handle = handle
        self._buf = buf
        self.ack = ack
        self._closed = threading.Event()
        self._reader_thread_id: int | None = None
        self._lock = threading.Lock()

    @property
    def closed(self) -> bool:
        return self._closed.is_set()

    def __iter__(self) -> Iterator[dict]:
        self._reader_thread_id = threading.get_native_id()
        try:
            while not self._closed.is_set():
                try:
                    line = _read_line(self._handle, self._buf)
                except (OSError, ValueError):
                    if self._closed.is_set():
                        return
                    raise
                if line is None:
                    return
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line.decode("utf-8"))
                except ValueError:
                    log.warning("dropping undecodable event line: %r", line[:200])
        finally:
            self._reader_thread_id = None
            self.close()

    def close(self) -> None:
        with self._lock:
            if self._closed.is_set():
                return
            self._closed.set()
            tid = self._reader_thread_id
        if tid is not None and tid != threading.get_native_id():
            _cancel_blocking_read(tid)
        try:
            self._handle.close()
        except OSError:
            pass


def _cancel_blocking_read(native_thread_id: int) -> None:
    """Abort a synchronous ReadFile pending on another thread (Windows only)."""
    if sys.platform != "win32":
        return
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenThread.restype = wintypes.HANDLE
    kernel32.OpenThread.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.CancelSynchronousIo.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    THREAD_TERMINATE = 0x0001
    handle = kernel32.OpenThread(THREAD_TERMINATE, False, native_thread_id)
    if not handle:
        return
    try:
        kernel32.CancelSynchronousIo(handle)
    finally:
        kernel32.CloseHandle(handle)


def _cli_source(source: str) -> str:
    return source.replace("_", "-")


def cli_args(method: str, params: dict) -> list[str]:
    """Map a socket request to the equivalent `herdr` CLI arguments."""
    p = params
    if method in ("ping", "workspace.list"):
        return ["workspace", "list"]
    if method == "agent.list":
        return ["agent", "list"]
    if method == "agent.get":
        return ["agent", "get", p["target"]]
    if method == "agent.read":
        args = ["agent", "read", p["target"], "--source", _cli_source(p.get("source", "recent_unwrapped"))]
        if p.get("lines") is not None:
            args += ["--lines", str(p["lines"])]
        return args
    if method == "agent.start":
        args = ["agent", "start", p["name"], "--kind", p["kind"], "--pane", p["pane_id"]]
        if p.get("timeout_ms") is not None:
            args += ["--timeout", str(p["timeout_ms"])]
        if p.get("args"):
            args += ["--", *p["args"]]
        return args
    if method in ("agent.prompt", "agent.wait"):
        args = ["agent", method.split(".")[1], p["target"]]
        if method == "agent.prompt":
            args.append(p["text"])
        wait = p.get("wait") if method == "agent.prompt" else p
        if wait is not None:
            if method == "agent.prompt":
                args.append("--wait")
            for status in wait.get("until") or []:
                args += ["--until", status]
            if wait.get("timeout_ms") is not None:
                args += ["--timeout", str(wait["timeout_ms"])]
        return args
    if method == "agent.send_keys":
        return ["agent", "send-keys", p["target"], *p["keys"]]
    if method == "pane.send_text":
        # Positional on purpose: the CLI types a `--` separator literally, and text starting with
        # `-` is typed correctly without one (both verified live on 0.8.2).
        return ["pane", "send-text", p["pane_id"], p["text"]]
    if method == "tab.list":
        return ["tab", "list"] + (["--workspace", p["workspace_id"]] if p.get("workspace_id") else [])
    if method == "tab.create":
        args = ["tab", "create"]
        if p.get("workspace_id"):
            args += ["--workspace", p["workspace_id"]]
        if p.get("cwd"):
            args += ["--cwd", p["cwd"]]
        if p.get("label"):
            args += ["--label", p["label"]]
        args.append("--focus" if p.get("focus") else "--no-focus")
        return args
    if method == "tab.close":
        return ["tab", "close", p["tab_id"]]
    if method == "pane.list":
        return ["pane", "list"] + (["--workspace", p["workspace_id"]] if p.get("workspace_id") else [])
    if method == "pane.get":
        return ["pane", "get", p["pane_id"]]
    raise HerdrUnavailable(f"no CLI fallback for {method}")


class CliTransport:
    def __init__(self, herdr_bin: str = "herdr", timeout: float | None = None):
        self.herdr_bin = herdr_bin
        self.timeout = timeout

    def request(self, method: str, params: dict | None = None) -> dict:
        params = params or {}
        argv = [self.herdr_bin, *cli_args(method, params)]
        try:
            proc = subprocess.run(argv, capture_output=True, timeout=self.timeout,
                                  creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except OSError as exc:
            raise HerdrUnavailable(f"{argv[0]} failed: {exc}") from exc
        except subprocess.TimeoutExpired as exc:
            raise HerdrOutcomeUnknown(f"{argv[0]} {method} timed out") from exc
        out = proc.stdout.decode("utf-8", "replace").strip()
        err = proc.stderr.decode("utf-8", "replace").strip()
        if proc.returncode != 0:
            try:
                return _unwrap(json.loads(err.splitlines()[-1]), method)
            except (ValueError, IndexError):
                raise HerdrError("cli_error", err or f"exit status {proc.returncode}")
        if method == "agent.read":
            # `herdr agent read` prints the transcript itself, not a JSON envelope.
            text = proc.stdout.decode("utf-8", "replace").replace("\r\n", "\n")
            return {"type": "pane_read", "read": {"text": text, "source": params.get("source")}}
        try:
            result = _unwrap(json.loads(out), method)
        except ValueError as exc:
            if method in SILENT_CLI_METHODS and not out:
                return {"type": "ok", "via": "cli"}
            raise HerdrError("bad_response", f"{method}: CLI printed non-JSON output") from exc
        if method == "ping":
            return {"type": "pong", "via": "cli"}
        return result

    def open_stream(self, method: str, params: dict) -> EventStream:
        raise HerdrUnavailable("event subscriptions need the Herdr pipe")


class AutoTransport:
    """Pipe first; CLI when the pipe is unavailable.

    The CLI is used only when the pipe request was never submitted, or when the
    outcome is unknown for a read-only method. A mutating request with an unknown
    outcome raises `HerdrOutcomeUnknown` so the caller can reconcile.
    """

    def __init__(self, socket_path: str, herdr_bin: str = "herdr"):
        self.pipe = PipeTransport(socket_path) if socket_path else None
        self.cli = CliTransport(herdr_bin)

    def request(self, method: str, params: dict | None = None) -> dict:
        if self.pipe is not None:
            try:
                return self.pipe.request(method, params)
            except HerdrUnavailable as exc:
                log.warning("pipe unavailable (%s); using CLI for %s", exc.message, method)
            except HerdrOutcomeUnknown as exc:
                if method not in READ_ONLY_METHODS:
                    raise
                log.warning("pipe reply lost (%s); retrying read-only %s via CLI", exc.message, method)
        return self.cli.request(method, params)

    def open_stream(self, method: str, params: dict) -> EventStream:
        if self.pipe is None:
            raise HerdrUnavailable("event subscriptions need HERDR_SOCKET_PATH")
        return self.pipe.open_stream(method, params)


@dataclass(frozen=True)
class ReadResult:
    text: str
    lines: int
    attempts: int


class HerdrClient:
    """Typed-ish helpers over the raw request API. Results are Herdr's JSON dicts."""

    def __init__(self, transport):
        self.transport = transport

    @classmethod
    def from_env(cls, socket_path: str | None = None, herdr_bin: str = "herdr") -> "HerdrClient":
        socket_path = socket_path or os.environ.get("HERDR_SOCKET_PATH", "")
        return cls(AutoTransport(socket_path, herdr_bin))

    def request(self, method: str, params: dict | None = None) -> dict:
        return self.transport.request(method, params or {})

    # --- read-only -----------------------------------------------------
    def ping(self) -> dict:
        return self.request("ping")

    def list_agents(self) -> list[dict]:
        return self.request("agent.list")["agents"]

    def get_agent(self, target: str) -> dict:
        return self.request("agent.get", {"target": target})["agent"]

    def find_agent(self, target: str) -> dict | None:
        try:
            return self.get_agent(target)
        except HerdrError as exc:
            if exc.code == "agent_not_found":
                return None
            raise

    def list_workspaces(self) -> list[dict]:
        return self.request("workspace.list")["workspaces"]

    def list_tabs(self, workspace_id: str | None = None) -> list[dict]:
        return self.request("tab.list", {"workspace_id": workspace_id})["tabs"]

    def list_panes(self, workspace_id: str | None = None) -> list[dict]:
        return self.request("pane.list", {"workspace_id": workspace_id})["panes"]

    def get_pane(self, pane_id: str) -> dict:
        return self.request("pane.get", {"pane_id": pane_id})["pane"]

    def read_agent_once(self, target: str, lines: int, source: str = "recent_unwrapped") -> str:
        result = self.request("agent.read", {"target": target, "source": source, "lines": lines})
        return result["read"]["text"]

    def read_agent(self, target: str, lines: int = 200, attempts: int = 4,
                   source: str = "recent_unwrapped", retry_delay: float = 0.3) -> ReadResult:
        """Read an agent transcript, retrying short reads.

        Herdr 0.8.2 captures alternate-screen history by scrolling, and a large
        read sometimes returns only the visible viewport with no indication.
        Retry up to `attempts` times and keep the longest text. Raises
        `agent_not_idle` (from Herdr) while the agent is working/blocked.
        """
        best = ""
        best_lines = -1
        tries = 0
        for tries in range(1, attempts + 1):
            if tries > 1 and retry_delay:
                time.sleep(retry_delay)
            text = self.read_agent_once(target, lines, source)
            n = len(text.splitlines())
            if n > best_lines:
                best, best_lines = text, n
            if n >= lines or _looks_complete(text):
                break
        return ReadResult(best, max(best_lines, 0), tries)

    # --- mutating (only used on panes/tabs the bridge creates or targets) -----
    def create_tab(self, workspace_id: str, label: str | None = None, cwd: str | None = None,
                   focus: bool = False) -> dict:
        return self.request("tab.create", {"workspace_id": workspace_id, "label": label,
                                           "cwd": cwd, "focus": focus})

    def start_agent(self, name: str, kind: str, pane_id: str, args: Sequence[str] = (),
                    timeout_ms: int | None = None) -> dict:
        params: dict[str, Any] = {"name": name, "kind": kind, "pane_id": pane_id, "args": list(args)}
        if timeout_ms is not None:
            params["timeout_ms"] = timeout_ms
        return self.request("agent.start", params)

    def prompt_agent(self, target: str, text: str, wait: bool = False,
                     timeout_ms: int | None = None) -> dict:
        params: dict[str, Any] = {"target": target, "text": text}
        if wait:
            params["wait"] = {"timeout_ms": timeout_ms, "until": []}
        return self.request("agent.prompt", params)["agent"]

    def send_keys(self, target: str, keys: Sequence[str]) -> dict:
        """Press keys in an agent's pane (`1`, `enter`, `esc`, `up`, `down`, `right`, ...)."""
        return self.request("agent.send_keys", {"target": target, "keys": list(keys)})

    def send_text(self, pane_id: str, text: str) -> dict:
        """Type literal text into a pane (no Enter)."""
        return self.request("pane.send_text", {"pane_id": pane_id, "text": text})

    # --- events ------------------------------------------------------------
    def subscribe(self, subscriptions: list[dict]) -> EventStream:
        return self.transport.open_stream("events.subscribe", {"subscriptions": subscriptions})


def _looks_complete(text: str) -> bool:
    """True when the read reaches the session banner, i.e. the whole history was captured.

    Response markers are no proof: a viewport-only read can still contain one.
    """
    return any("Claude Code v" in line or "OpenAI Codex (v" in line for line in text.splitlines())
