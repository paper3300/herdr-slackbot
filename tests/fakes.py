"""Test doubles for the bridge: Herdr client, Slack transport, manager, executor."""

from __future__ import annotations

import itertools
import threading
import time

from herdr_slackbot.events import _Stats
from herdr_slackbot.herdr_client import HerdrError, ReadResult
from herdr_slackbot.slack_transport import SlackUncertainError


class Accepted:
    """`fail_posts` item: Slack accepts the post, then the response is lost."""

    def __init__(self, error="ReadTimeout"):
        self.error = error


class FakeHerdr:
    def __init__(self):
        self._seq = itertools.count(1)
        self._pane_ids = itertools.count(50)
        self._sessions = itertools.count(1)
        self.agents: dict[str, dict] = {}
        self.workspaces = [
            {"workspace_id": "w1", "label": "Main", "focused": True},
            {"workspace_id": "w2", "label": "Other", "focused": False},
            {"workspace_id": "w9", "label": "herdr-slack", "focused": False},
        ]
        self.panes = {"w1": [{"pane_id": "w1:p1", "cwd": "D:\\main\\", "agent": "claude"}],
                      "w2": [{"pane_id": "w2:p1", "cwd": "D:\\other"}]}
        self.calls: list[tuple] = []
        self.prompt_script: list = []  # per prompt_agent call: None (ok) or an exception to raise
        self.after_prompt: str | None = "working"  # status set after a successful prompt
        self.screens: dict[str, str] = {}
        self.visible: dict[str, str] = {}
        self.start_error: Exception | None = None
        self.sessionless_kinds: set[str] = set()  # kinds whose session appears only after a turn
        self.slow: dict[str, float] = {}  # method -> seconds to sleep
        self.log: list | None = None  # shared call log (set by tests)
        self.visible_error: Exception | None = None
        # Called after send_keys / send_text with (pane_id, keys | text): tests script the screen
        # and status changes a key press causes.
        self.on_keys = None
        self.on_text = None
        self.input_error: Exception | None = None

    # --- setup helpers ---
    def add_agent(self, pane_id, status="idle", name=None, session=None, kind="claude", cwd="D:\\main",
                  title="Claude Code"):
        info = {"pane_id": pane_id, "workspace_id": pane_id.split(":")[0], "agent_status": status,
                "name": name, "agent": kind, "cwd": cwd, "terminal_title_stripped": title,
                "agent_session": ({"value": session or f"sess-{next(self._sessions)}"}
                                  if session is not False else None),
                "terminal_id": f"term-{pane_id}", "state_change_seq": next(self._seq)}
        self.agents[pane_id] = info
        return info

    def set_status(self, pane_id, status):
        self.agents[pane_id]["agent_status"] = status
        self.agents[pane_id]["state_change_seq"] = next(self._seq)

    def _resolve(self, target):
        for a in self.agents.values():
            if a.get("name") == target:
                return a
        return self.agents.get(target)

    # --- client API ---
    def ping(self):
        return {"type": "pong", "version": "0.8.2"}

    def list_agents(self):
        if self.slow.get("list_agents"):
            time.sleep(self.slow["list_agents"])
        if self.log is not None:
            self.log.append("list_agents")
        return [dict(a) for a in self.agents.values()]

    def find_agent(self, target):
        a = self._resolve(target)
        return dict(a) if a else None

    def get_agent(self, target):
        a = self.find_agent(target)
        if a is None:
            raise HerdrError("agent_not_found", target)
        return a

    def list_workspaces(self):
        if self.slow.get("list_workspaces"):
            time.sleep(self.slow["list_workspaces"])
        if self.log is not None:
            self.log.append("list_workspaces")
        return [dict(w) for w in self.workspaces]

    def list_panes(self, workspace_id=None):
        return list(self.panes.get(workspace_id, []))

    def create_tab(self, workspace_id, label=None, cwd=None, focus=False):
        self.calls.append(("create_tab", workspace_id, label, cwd))
        pane_id = f"{workspace_id}:p{next(self._pane_ids)}"
        return {"type": "tab_created", "tab": {"tab_id": f"{workspace_id}:t9"}, "root_pane": {"pane_id": pane_id}}

    def start_agent(self, name, kind, pane_id, args=(), timeout_ms=None):
        self.calls.append(("start_agent", name, kind, pane_id, list(args)))
        if self.start_error:
            raise self.start_error
        info = self.add_agent(pane_id, "idle", name=name, kind=kind,
                              session=False if kind in self.sessionless_kinds else None)
        return {"type": "agent_started", "agent": dict(info), "argv": [kind, *args]}

    def prompt_agent(self, target, text, wait=False, timeout_ms=None):
        self.calls.append(("prompt_agent", target, text))
        step = self.prompt_script.pop(0) if self.prompt_script else None
        if callable(step):
            step = step()  # may mutate state, may return an exception to raise
        if isinstance(step, Exception):
            raise step
        a = self._resolve(target)
        if a is None:
            raise HerdrError("agent_not_found", target)
        if self.after_prompt:
            self.set_status(a["pane_id"], self.after_prompt)
        return dict(a)

    def read_agent(self, target, lines=200, attempts=4, source="recent_unwrapped", retry_delay=0.3):
        text = self.screens.get(target, "")
        return ReadResult(text, len(text.splitlines()), 1)

    def read_agent_once(self, target, lines, source="recent_unwrapped"):
        if self.visible_error:
            raise self.visible_error
        return self.visible.get(target, "")

    def send_keys(self, target, keys):
        self.calls.append(("send_keys", target, list(keys)))
        if self.input_error:
            raise self.input_error
        if self.on_keys:
            self.on_keys(target, list(keys))
        return {"type": "ok"}

    def send_text(self, pane_id, text):
        self.calls.append(("send_text", pane_id, text))
        if self.input_error:
            raise self.input_error
        if self.on_text:
            self.on_text(pane_id, text)
        return {"type": "ok"}

    def prompts(self):
        return [c for c in self.calls if c[0] == "prompt_agent"]

    def inputs(self):
        return [c for c in self.calls if c[0] in ("send_keys", "send_text")]


class FakeTransport:
    def __init__(self):
        self.posts: list[dict] = []
        self.updates: list[dict] = []
        self.ephemerals: list[dict] = []
        self.uploads: list[dict] = []
        self.views_opened: list[dict] = []
        self.views_updated: list[dict] = []
        self.responses: list[dict] = []
        self.fail_posts: list = []  # per upcoming post: None = ok, exception = rejected, Accepted() = lost reply
        self.fail_finds: list = []  # exceptions raised by upcoming find_message calls
        self.finds: list[tuple] = []
        self.events: list[str] = []  # ordered log of calls (for ack/trigger ordering tests)
        self.post_delay = 0.0
        self.published: list[tuple] = []  # (user, view) from publish_view
        self.dm_users: list[str] = []  # users open_dm was called for
        self._ts = itertools.count(1)
        self._lock = threading.Lock()

    def open_dm(self, user_id):
        self.dm_users.append(user_id)
        return "D-OWNER"

    def publish_view(self, user_id, view):
        self.published.append((user_id, view))

    def post_message(self, channel, text, blocks=None, thread_ts=None, op=None):
        if self.post_delay:
            time.sleep(self.post_delay)
        with self._lock:
            fault = self.fail_posts.pop(0) if self.fail_posts else None
            if isinstance(fault, Exception):
                raise fault
            ts = f"1000.{next(self._ts):04d}"
            self.posts.append({"channel": channel, "text": text, "blocks": blocks, "thread_ts": thread_ts,
                               "ts": ts, "op": op})
            self.events.append("post")
            if isinstance(fault, Accepted):
                raise SlackUncertainError(fault.error)
        return ts

    def find_message(self, channel, op, thread_ts=None, oldest=None):
        with self._lock:
            self.finds.append((op, thread_ts))
            if self.fail_finds:
                raise self.fail_finds.pop(0)
            for p in self.posts:
                if p["op"] == op and p["channel"] == channel and (
                        p["thread_ts"] == thread_ts if thread_ts else p["thread_ts"] is None):
                    return p["ts"]
        return None

    def respond(self, response_url, text, blocks=None):
        self.responses.append({"url": response_url, "text": text, "blocks": blocks})
        self.events.append("respond")

    def update_message(self, channel, ts, text, blocks=None):
        self.updates.append({"channel": channel, "ts": ts, "text": text, "blocks": blocks})

    def post_ephemeral(self, channel, user, text, blocks=None, thread_ts=None):
        self.ephemerals.append({"channel": channel, "user": user, "text": text, "blocks": blocks})

    def upload_text_file(self, channel, thread_ts, filename, content, title):
        self.uploads.append({"channel": channel, "thread_ts": thread_ts, "filename": filename,
                             "content": content, "title": title})

    def open_view(self, trigger_id, view):
        self.views_opened.append({"trigger_id": trigger_id, "view": view})
        self.events.append("open_view")
        return "V1"

    def update_view(self, view_id, view, view_hash=None):
        self.views_updated.append({"view_id": view_id, "view": view, "hash": view_hash})
        self.events.append("update_view")

    def thread(self, thread_ts):
        return [p for p in self.posts if p["thread_ts"] == thread_ts]

    def texts(self):
        return [p["text"] for p in self.posts]


class FakeManager:
    def __init__(self):
        self.stats = _Stats()
        self.started = self.stopped = False

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True

    def subscribed_panes(self):
        return set()


class SyncExecutor:
    def submit(self, fn, *args):
        fn(*args)

    def shutdown(self, **kwargs):
        pass


class DeferredExecutor:
    """Collects jobs; `run_all()` executes them later (exposes admission races)."""

    def __init__(self):
        self.jobs = []

    def submit(self, fn, *args):
        self.jobs.append((fn, args))

    def run_all(self):
        jobs, self.jobs = self.jobs, []
        for fn, args in jobs:
            fn(*args)

    def shutdown(self, **kwargs):
        pass


class Clock:
    def __init__(self, now=1_790_000_000.0):
        self.now = now

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds
