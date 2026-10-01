"""Bridge orchestration: Herdr events + state + Slack (through `SlackTransport`).

Threading model
- Slack entry points never touch Herdr before Slack is acknowledged: commands are
  acked first and answered through `response_url`; modals open a loading view with
  the trigger id at once and are filled with `views.update`; view submissions run
  local checks plus a live check bounded by `ACK_BUDGET` seconds.
- Sends (slash, modal, thread reply, a new agent's first prompt) all go through
  `_admit()` on a worker thread, under the thread lock: identity re-validation
  (session / terminal), D6 status gate, pending-task reservation and thread-root
  creation happen atomically, so two requests can never both prompt one agent or
  create two roots. The notifier (transitions, restart resume) takes the same lock
  for its whole decision + root creation, so it can't race admission either.
- Every Slack task has a `task_id` and the agent's `state_change_seq` at admission
  (`seq0`). A transition applies to the task only if it is newer than `seq0`, and a
  task is cleared only by its own completion. Completions/blocked alerts are
  de-duplicated per session with `last_result_seq` / `last_blocked_seq`.
- Transitions and restart-resume items go through one ordered notifier thread.
  Slack failures there are retried with backoff (transient) until they succeed.
- Posts that matter (thread roots, results, started/blocked/ended, send echoes) carry a
  stable `op` marker (metadata + block_id). The intent `{since, ts}` is persisted before
  posting. After an ambiguous failure (`SlackUncertainError`) the next attempt looks the
  marker up and reuses the accepted message; a lookup that can't prove absence (or runs
  sooner than MIN_ABSENT_AGE after the attempt) defers instead of re-posting. A successful
  post first records its ts in the intent; the caller's state commit (binding / result /
  task) removes the intent in the same atomic write (`_commit`), so an interruption between
  post and commit never loses track of an accepted message. Other notices: at-most-once.
- A prompt is never re-sent after a stall / unknown outcome; only errors Herdr
  raises before writing input (`agent_not_ready` / `agent_not_found` for a just
  started agent) are retried, each after re-validating the target's identity.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
import queue
import re
import threading
import time
import uuid
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable, Mapping

from . import blocks as B
from .agents import (
    KIND_CODEX,
    KIND_SPECS,
    LaunchError,
    build_agent_args,
    codex_home,
    codex_model_options,
)
from .claude_session import agent_result
from .commands import (
    parse_command,
    parse_new_args,
    parse_send_args,
    resolve_agent,
    resolve_workspace,
    workspace_cwd,
)
from .config import Config
from .dialog import (KIND_PLAN, KIND_TRUST, Dialog, keys_for, parse_dialog, screen_fingerprint, submit_keys,
                     tail_lines, text_keys, typed_text)
from .events import AgentTransition, SubscriptionManager, session_of
from .herdr_client import HerdrError, HerdrOutcomeUnknown
from .naming import (
    agent_display_name,
    auto_agent_name,
    format_duration,
    tab_label,
    truncate,
    validate_agent_name,
)
from .notify import (
    REASON_BLOCKED,
    REASON_BUSY,
    REASON_GONE,
    REASON_SESSION_CHANGED,
    SETTLED,
    Action,
    check_send_allowed,
    decide_notification,
    decide_resume,
)
from .results import ResultStore
from .slack_transport import SlackPermanentError, SlackTransientError, SlackUncertainError
from .state import ORIGIN_PC, ORIGIN_SLACK, StateError, StateStore

log = logging.getLogger(__name__)

PENDING_GRACE_SECONDS = 15.0  # an unreacted reservation blocks other sends this long
# Thread key for an agent whose session id Herdr has not reported yet (Codex reports
# it only once its first turn starts). Re-keyed when it appears.
PROVISIONAL_PREFIX = "pending:"
FRESH_PROMPT_RETRIES = 10  # a just-started agent may not be addressable for a few seconds
PROMPT_EXCERPT = 300
ACK_BUDGET = 1.5  # seconds a view submission may spend on live checks before acking
HOME_MIN_INTERVAL = 5.0  # automatic Home tab refreshes are coalesced to at most one per this many seconds
HOME_SETTLE = 1.0  # a refresh waits at least this long so a burst of transitions is published once
NOTIFY_MAX_ATTEMPTS = 30  # ~25 min with backoff capped at 60s
WORKER_POST_ATTEMPTS = 4
INTENT_LOOKBACK = 120.0  # seconds before an uncertain post's first attempt to search history from
MIN_ABSENT_AGE = 5.0  # a "not found" younger than this may be Slack's indexing lag: defer, don't re-post
INTENT_MAX_AGE = 7 * 86400.0  # leftover intents older than this are pruned on the next commit
DIALOG_POLL_INTERVAL = 0.3  # after answering: how often the screen / status is checked
DIALOG_POLL_TIMEOUT = 5.0  # ... and for how long before "could not confirm"
DIALOG_KEY_GAP = 0.3  # between the digit that opens a text input, the text and Enter
DIALOG_READ_LINES = 80
DIALOG_READ_RETRIES = 2  # extra reads of a screen that did not parse before posting a keypad
IDLE_DIALOG_RECHECK = 15.0  # idle dialogs (Codex trust) are rechecked at most this often
CLOSED_DIALOGS_KEPT = 20  # closed dialog message ts per thread (late clicks leave them alone)
DIALOG_SCREEN_LINES = 40  # [Show screen]
PLAN_FILE_MAX = 200_000
KEYPAD = "keypad"  # pending-dialog kind when the screen could not be parsed
DIALOG_KEYS = frozenset({"1", "2", "3", "4", "up", "down", "enter", "esc", "right", "submit"})
KEY_LABELS = {"up": "↑", "down": "↓", "enter": "Enter", "esc": "Esc", "right": "Next →", "submit": "Next →"}
DIALOG_GONE_TEXT = "This question is no longer open."
DIALOG_BUTTONS_TEXT = "This question needs one of the buttons above."
DIALOG_CHANGED_TEXT = "The question changed on PC; nothing was sent. Answer the updated one above."
DIALOG_STALE_TEXT = "That button was out of date; nothing was sent. Use the updated buttons above."
UNCONFIRMED_TEXT = "⚠️ Could not confirm the answer; check the screen."
ANSWERED_ON_PC = "✅ answered on PC"
ALREADY_ANSWERED = "✅ Already answered or changed on PC"
AGENT_ENDED = "⏹ ended"


@dataclass
class Reply:
    text: str
    blocks: list | None = None


class Delivery(Enum):
    SENT = "sent"
    BLOCKED = "blocked"
    UNCERTAIN = "uncertain"
    FAILED = "failed"
    CHANGED = "changed"


@dataclass
class DeliveryResult:
    outcome: Delivery
    message: str = ""


@dataclass
class Target:
    """Who a send is for. `session` (or, before Herdr reports one, `terminal_id`) is the identity."""
    pane_id: str
    session: str | None = None
    terminal_id: str | None = None
    name: str | None = None
    key: str | None = None  # thread key if known (session or provisional key)


@dataclass
class Admission:
    key: str
    entry: dict
    info: dict
    task: dict


@dataclass
class NewAgentRequest:
    workspace_id: str
    prompt: str
    kind: str = "claude"
    model: str | None = None
    effort: str | None = None
    mode: str | None = None
    cwd: str | None = None
    name: str | None = None  # None -> slack-<N>
    args: list = field(default_factory=list)


@dataclass(frozen=True)
class _Reconcile:
    """Notifier item: reconcile a thread's open dialog / deferred prompt without a transition."""
    key: str


@dataclass(frozen=True)
class _Resume:
    key: str
    task_id: str | None = None  # follows the task if its entry is re-keyed (provisional -> session)


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def prompt_visible(screen: str, prompt: str) -> bool:
    """Whether (the tail of) `prompt` shows up on screen. Only used to word a warning."""
    flat_prompt = _norm(prompt).rstrip("?.!…。 ")
    return bool(flat_prompt) and flat_prompt[-24:] in _norm(screen)


def target_of(info: Mapping) -> Target:
    return Target(info["pane_id"], session_of(info), info.get("terminal_id"), info.get("name"))


def _seq(info: Mapping | None) -> int:
    try:
        return int((info or {}).get("state_change_seq") or 0)
    except (TypeError, ValueError):
        return 0


REASON_TEXT = {
    REASON_BUSY: "⏳ *{name}* is busy (working). Try again when it has finished.",
    REASON_BLOCKED: "⚠️ *{name}* is waiting for an answer: use the buttons in its thread, or answer on PC.",
    "unknown": "❔ *{name}*'s state is unknown, so nothing was sent.",
    REASON_GONE: "🪦 The agent has exited, so nothing was sent.",
    REASON_SESSION_CHANGED: "🔄 The agent is gone; its pane now hosts a different agent. Nothing was sent.",
    "pending": "⏳ *{name}* is still on the previous Slack task; wait for its result.",
}


class HomeRefresher:
    """Home tab refresh scheduler.

    - At most one publish in flight; requests during it collapse into ONE pending refresh.
    - The next automatic publish is scheduled from the actual completion of the previous one:
      at least `interval` after it, at least `settle` after the request, never inside a
      Slack `retry_after` cooldown. At most one timer exists at a time.
    - Manual publishes (`run_now`: home opened / refresh button) publish immediately and
      replace a scheduled automatic refresh (they cover it), so the spacing restarts from them.
    - A transient failure sets a cooldown (`retry_after`, else `failure_backoff`) and retries
      the refresh once after it.
    - `close()` cancels the pending refresh; `publish` itself re-checks shutdown before work.
    """

    def __init__(self, publish: Callable[[], None], interval: float = HOME_MIN_INTERVAL,
                 settle: float = HOME_SETTLE, clock: Callable[[], float] = time.monotonic,
                 timer_factory=threading.Timer, failure_backoff: float = 30.0):
        self.publish = publish
        self.interval = interval
        self.settle = settle
        self.clock = clock
        self.timer_factory = timer_factory
        self.failure_backoff = failure_backoff
        self._lock = threading.Lock()
        self._timer = None
        self._token = None
        self._running = False
        self._pending = False
        self._retry_used = False  # the one retry after a failure has been scheduled
        self._last = float("-inf")  # completion time of the last publish attempt
        self._cooldown_until = float("-inf")
        self._closed = False

    # -- requests
    def request(self) -> None:
        with self._lock:
            if self._closed:
                return
            if self._running:
                self._pending = True  # coalesced; scheduled when the running publish completes
                return
            self._schedule_locked(self.settle)

    def run_now(self) -> None:
        """Publish right away (manual), unless a publish is running or Slack asked us to wait:
        then it becomes the single pending refresh."""
        with self._lock:
            if self._closed:
                return
            if self._running or self.clock() < self._cooldown_until:
                self._pending = True
                if not self._running:
                    self._schedule_locked(0.0)
                return
            self._cancel_timer_locked()  # this publish covers any scheduled automatic one
            self._pending = False
            self._running = True
        self._run()

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._pending = False
            self._cancel_timer_locked()

    # -- internals
    def _schedule_locked(self, min_delay: float) -> None:
        if self._timer is not None or self._closed:
            return  # at most one timer; it will pick up the latest state
        now = self.clock()
        delay = max(min_delay, self._last + self.interval - now, self._cooldown_until - now, 0.0)
        token = self._token = object()  # a cancelled timer that fires anyway must do nothing
        self._timer = self.timer_factory(delay, lambda: self._fire(token))
        if hasattr(self._timer, "daemon"):
            self._timer.daemon = True
        self._timer.start()

    def _cancel_timer_locked(self) -> None:
        self._token = None
        if self._timer is not None:
            cancel = getattr(self._timer, "cancel", None)
            if cancel:
                cancel()
            self._timer = None

    def _fire(self, token=None) -> None:
        with self._lock:
            if token is not self._token:
                return  # stale (cancelled / replaced) timer
            self._timer = None
            self._token = None
            if self._closed or self._running:
                return
            self._pending = False
            self._running = True
        self._run()

    def _run(self) -> None:
        failure: float | None = None
        try:
            self.publish()
        except SlackTransientError as exc:
            failure = exc.retry_after or self.failure_backoff
            log.warning("Home tab refresh failed (%s); next attempt in >= %.0fs", exc.error, failure)
        except Exception:
            log.exception("Home tab refresh failed")
        with self._lock:
            self._running = False
            self._last = self.clock()
            if failure is not None:
                self._cooldown_until = self._last + failure
                if not self._retry_used:
                    self._retry_used = True
                    self._pending = True  # retry the last refresh once after the cooldown
            else:
                self._retry_used = False
            if self._pending and not self._closed:
                self._schedule_locked(0.0)


class Bridge:
    def __init__(self, cfg: Config, client, state: StateStore, transport, results: ResultStore, *,
                 clock: Callable[[], float] = time.time, sleep: Callable[[float], None] = time.sleep,
                 executor=None, manager: SubscriptionManager | None = None,
                 claude_projects: Path | None = None, codex_home_dir: Path | None = None,
                 ack_budget: float = ACK_BUDGET, home_dir: Path | None = None):
        self.cfg = cfg
        self.client = client
        self.state = state
        self.transport = transport
        self.results = results
        self.clock = clock
        self.sleep = sleep
        self.executor = executor or ThreadPoolExecutor(max_workers=4, thread_name_prefix="bridge")
        self._probe_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="bridge-probe")
        self.manager = manager or SubscriptionManager(client, self.enqueue_transition)
        self.claude_projects = claude_projects
        self.codex_home_dir = codex_home_dir or codex_home()
        self.ack_budget = ack_budget
        self.home_dir = home_dir  # `~` of plan file paths shown by plan approval dialogs
        self.dm_channel: str | None = None
        self.started_at = clock()
        self._notify_q: queue.Queue = queue.Queue()
        self._notify_thread: threading.Thread | None = None
        self._stopping = threading.Event()
        # One lock for thread roots, pending tasks and notification decisions (admission,
        # transitions, resume). Order: this lock, then a per-key lock; never the reverse.
        self._admission_lock = threading.RLock()
        self._intents: dict[str, dict] = {}  # op -> {"since", "ts"} (in-process view of post intents)
        self._locks: dict[str, threading.RLock] = defaultdict(threading.RLock)
        self._locks_guard = threading.Lock()
        # Per agent (terminal id): answering a dialog (keys + confirmation polling), its transitions,
        # restart reconciles and startup dialogs are serialized. Order: this lock, then the admission
        # lock, then the per-key lock (see "Blocked dialogs" below).
        self._answer_locks: dict[str, threading.Lock] = defaultdict(threading.Lock)
        self.stats = {"notifications": 0, "prompts": 0, "agents_started": 0, "slack_retries": 0}
        self._home_user: str | None = None  # set once the owner has opened the Home tab
        self._activated = False  # start() / activate_owner() completed
        self.home = HomeRefresher(self.publish_home)
        # Open send modals: view id -> {seq, target, hash, prompt, agents, labels, metadata}
        self._send_views: dict[str, dict] = {}
        self._send_lock = threading.RLock()

    # --- lifecycle --------------------------------------------------------------
    def start(self) -> None:
        self.dm_channel = self.transport.open_dm(self.cfg.slack_owner_user_id)
        self._notify_thread = threading.Thread(target=self._notify_loop, name="bridge-notify", daemon=True)
        self._notify_thread.start()
        self.manager.start()
        self._activated = True
        # Restart recovery runs on the notifier thread, serialized with live transitions.
        self._queue_recovery()

    def _queue_recovery(self) -> None:
        for key, entry in self.state.all_threads().items():
            if entry.get("pending_task"):
                self._notify_q.put(_Resume(key, entry["pending_task"].get("task_id")))
            if entry.get("dialog") or entry.get("deferred_prompt"):
                self._notify_q.put(_Reconcile(key))  # answered / changed while the bridge was down

    def activate_owner(self, user: str) -> None:
        """Pairing succeeded (pairing.py): serve `user` from now on without a restart. Starts
        the notification pipeline (it is not started in pairing mode) and welcomes the owner.
        Raises when a step fails; calling it again resumes at the failed step (Pairing.complete
        retries), and a second call after success does nothing."""
        with self._admission_lock:
            if self._activated or self._stopping.is_set():
                return
            if self.dm_channel is None:
                self.dm_channel = self.transport.open_dm(user)  # may raise: nothing started yet
            self.cfg = dataclasses.replace(self.cfg, slack_owner_user_id=user)
            if self._notify_thread is None:
                self._notify_thread = threading.Thread(target=self._notify_loop, name="bridge-notify", daemon=True)
                self._notify_thread.start()
            try:
                self.manager.start()
            except Exception:
                self.manager.stop()  # leave no half-open subscriptions behind for the retry
                raise
            self._activated = True
            self._queue_recovery()
        cmd = self.cfg.slash_command
        self._post_safe(f"👋 Paired! This Herdr bridge now serves only you.\n"
                        f"Agents' done/blocked notifications arrive here, one thread per agent; reply in a "
                        f"thread to prompt that agent.\n\n{B.usage_text(cmd)}")

    def stop(self) -> None:
        self._stopping.set()
        self.home.close()
        with self._admission_lock:  # an activate_owner() in progress finishes its start() first
            started = self._notify_thread is not None
        if started:  # not started: still in pairing mode
            self.manager.stop()
            self._notify_q.put(None)
            self._notify_thread.join(5)
        for pool in (self.executor, self._probe_pool):
            shutdown = getattr(pool, "shutdown", None)
            if shutdown:
                shutdown(wait=False, cancel_futures=True)

    def wait_idle(self, timeout: float = 5.0) -> bool:
        """Test helper: wait until the notifier queue is drained."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._notify_q.unfinished_tasks == 0:
                return True
            time.sleep(0.01)
        return False

    def _lock(self, key: str) -> threading.RLock:
        with self._locks_guard:
            return self._locks[key]

    def _answer_lock(self, key: str) -> threading.Lock:
        with self._locks_guard:
            return self._answer_locks[key]

    # --- small helpers -------------------------------------------------------------
    def _submit(self, fn, *args) -> None:
        def run():
            try:
                fn(*args)
            except Exception:
                log.exception("background task %s failed", getattr(fn, "__name__", fn))
                self._post_safe("❌ Internal error in the Herdr bridge; see bridge.log.")
        self.executor.submit(run)

    def _post(self, text: str, blocks: list | None = None, thread_ts: str | None = None,
              op: str | None = None, key: str | None = None) -> str:
        """Post to the DM. With `op`, the post is idempotent across attempts (see module doc).
        With `key`, the intent lives in that thread entry (survives restarts) and the caller
        must finish with `_commit(key, op, ...)`; without a key it is in-process only."""
        if op is None:
            return self.transport.post_message(self.dm_channel, text, blocks, thread_ts)
        intent = self._intent(op, key)
        if intent is not None and intent.get("ts"):
            return intent["ts"]  # accepted earlier; only the caller's commit is missing
        if intent is not None:
            # May raise SlackLookupIncomplete / transient errors: the outcome stays unknown.
            found = self.transport.find_message(self.dm_channel, op, thread_ts,
                                                float(intent["since"]) - INTENT_LOOKBACK)
            if found:
                log.info("Slack post %s was accepted before; reusing %s", op, found)
                return self._accepted(op, key, intent, found)
            if self.clock() - float(intent["since"]) < MIN_ABSENT_AGE:
                raise SlackTransientError("lookup_too_early")
        else:
            intent = self._add_intent(op, key)
        try:
            ts = self.transport.post_message(self.dm_channel, text, blocks, thread_ts, op=op)
        except SlackUncertainError:
            raise  # keep the intent: the next attempt reconciles before posting
        except Exception:
            self._drop_intent(op, key)  # Slack answered: definitely not posted
            raise
        return self._accepted(op, key, intent, ts)

    def _intent(self, op: str, key: str | None) -> dict | None:
        if op in self._intents:
            return self._intents[op]
        if key:
            raw = ((self.state.get_thread(key) or {}).get("post_intents") or {}).get(op)
            if raw is not None:
                intent = dict(raw) if isinstance(raw, dict) else {"since": float(raw), "ts": None}
                self._intents[op] = intent
                return intent
        return None

    def _store_intent(self, op: str, key: str | None, intent: dict) -> None:
        self._intents[op] = intent
        if key:
            intents = dict((self.state.get_thread(key) or {}).get("post_intents") or {})
            intents[op] = intent
            self.state.upsert_thread(key, post_intents=intents)

    def _add_intent(self, op: str, key: str | None) -> dict:
        intent = {"since": self.clock(), "ts": None}
        self._store_intent(op, key, intent)
        return intent

    def _accepted(self, op: str, key: str | None, intent: dict, ts: str) -> str:
        if key:
            self._store_intent(op, key, dict(intent, ts=ts))  # durable before any follow-up commit
        else:
            self._intents.pop(op, None)
        return ts

    def _drop_intent(self, op: str, key: str | None) -> None:
        self._intents.pop(op, None)
        if key:
            entry = self.state.get_thread(key)
            if entry is not None and op in (entry.get("post_intents") or {}):
                intents = dict(entry["post_intents"])
                intents.pop(op)
                self.state.upsert_thread(key, post_intents=intents)

    def _commit(self, key: str, op: str | None, **fields) -> dict:
        """Apply the state change that completes `op` and remove its intent in one atomic write."""
        entry = self.state.get_thread(key) or {}
        now = self.clock()
        intents = {}
        for name, raw in (entry.get("post_intents") or {}).items():
            since = raw.get("since") if isinstance(raw, dict) else raw
            if name != op and now - float(since or 0) < INTENT_MAX_AGE:
                intents[name] = raw
        if op:
            self._intents.pop(op, None)
        return self.state.upsert_thread(key, post_intents=intents, **fields)

    def _retrying(self, fn, attempts: int = WORKER_POST_ATTEMPTS, idempotent: bool = True):
        """Call a Slack operation, retrying transient failures a few times (worker threads).
        A non-idempotent call is not retried after an uncertain outcome (at-most-once)."""
        delay = 1.0
        for attempt in range(1, attempts + 1):
            try:
                return fn()
            except SlackTransientError as exc:
                if attempt == attempts or self._stopping.is_set():
                    raise
                if isinstance(exc, SlackUncertainError) and not idempotent:
                    raise
                self.stats["slack_retries"] += 1
                wait = max(exc.retry_after or 0.0, delay)
                log.warning("Slack call failed (%s); retrying in %.0fs", exc.error, wait)
                self.sleep(wait)
                delay = min(delay * 2, 30.0)

    def _post_safe(self, text: str, thread_ts: str | None = None) -> None:
        """Non-critical notice: retried only while it is certainly not posted (at-most-once)."""
        try:
            self._retrying(lambda: self._post(_strip_mrkdwn(text), B.notice_blocks(text), thread_ts),
                           idempotent=False)
        except SlackUncertainError:
            log.warning("a notice may not have reached Slack (outcome unknown); not re-posted")
        except Exception:
            log.exception("posting to Slack failed")

    def _reply_safe(self, reply: Callable[[str], None] | None, text: str, thread_ts: str | None = None) -> None:
        if reply is not None:
            try:
                self._retrying(lambda: reply(text), idempotent=False)
                return
            except SlackUncertainError:
                log.warning("a reply may not have reached Slack (outcome unknown); not repeated in the DM")
                return
            except Exception:
                log.exception("replying to Slack failed")
        self._post_safe(text, thread_ts)

    def _bounded(self, fn, timeout: float):
        """Run `fn` with a deadline. Returns (done, value); exceptions count as not done."""
        future = self._probe_pool.submit(fn)
        try:
            return True, future.result(timeout=timeout)
        except FutureTimeout:
            return False, None
        except Exception as exc:
            log.warning("bounded check failed: %s", exc)
            return False, None

    def _ws_labels(self) -> dict[str, str]:
        try:
            return {w["workspace_id"]: w.get("label") or w["workspace_id"] for w in self.client.list_workspaces()}
        except HerdrError:
            return {}

    def _read_screen(self, target: str) -> str:
        try:
            return self.client.read_agent(target, self.cfg.read_lines).text
        except HerdrError as exc:
            log.warning("agent.read %s failed: %s", target, exc)
            return ""

    def _find_session_agent(self, session: str, pane_hint: str | None) -> dict | None:
        """The live agent running `session` (follows pane moves), or None."""
        if pane_hint:
            info = self.client.find_agent(pane_hint)
            if info is not None and session_of(info) == session:
                return info
        for agent in self.client.list_agents():
            if session_of(agent) == session:
                return agent
        return None

    def _resolve(self, target: Target) -> tuple[dict | None, str | None]:
        """Live agent for `target`, verified by identity -> (info, rejection reason)."""
        if target.session:
            info = self._find_session_agent(target.session, target.pane_id)
            if info is not None:
                return info, None
        else:
            info = self.client.find_agent(target.pane_id)
            if info is not None and (not target.terminal_id or info.get("terminal_id") == target.terminal_id):
                return info, None
        other = self.client.find_agent(target.pane_id)
        return None, (REASON_SESSION_CHANGED if other is not None else REASON_GONE)

    def _identity_ok(self, info: Mapping | None, target: Target) -> bool:
        if info is None:
            return False
        if target.session:
            return session_of(info) == target.session
        return not target.terminal_id or info.get("terminal_id") == target.terminal_id

    # --- threads -----------------------------------------------------------------------
    def _ensure_thread(self, key: str, info: Mapping | None, title: str, lines: list[str],
                       origin: str, provisional: bool = False) -> dict:
        """Reuse or create the thread root. Callers hold the thread lock. The root's op id and
        binding fields are persisted before posting, so an accepted-but-unacknowledged root is
        found again (by its marker) instead of being posted twice."""
        entry = self.state.get_thread(key)
        if entry and entry.get("thread_ts"):
            return entry
        muted = bool(entry and entry.get("muted"))
        info = info or {}
        fields = dict(channel=self.dm_channel, origin=origin,
                      agent_name=info.get("name") or (entry or {}).get("agent_name"),
                      pane_id=info.get("pane_id"), workspace_id=info.get("workspace_id"),
                      kind=info.get("agent"), cwd=info.get("cwd"), terminal_id=info.get("terminal_id"))
        if provisional:
            fields["provisional"] = True
        op = (entry or {}).get("root_op") or f"root:{uuid.uuid4().hex}"
        if (entry or {}).get("root_op") != op:
            self.state.upsert_thread(key, root_op=op, **fields)
        root_blocks = B.thread_root_blocks(title, lines, key, muted)
        ts = self._post(_strip_mrkdwn(title), root_blocks, op=op, key=key)
        return self._commit(key, op, thread_ts=ts, root_op=None, **fields)

    def _key_for(self, info: Mapping) -> tuple[str, bool]:
        """Thread key for a live agent: its session, else a provisional key (adopting an existing one)."""
        session = session_of(info)
        # Only a provisional entry of the very same terminal is this agent's (never a stale
        # entry that merely shares the pane id).
        existing = self.state.find_provisional_by_terminal(info.get("terminal_id"))
        if session:
            if existing:
                self.state.rekey_thread(existing, session)
            return session, False
        if existing:
            return existing, True
        return f"{PROVISIONAL_PREFIX}{info.get('terminal_id') or info['pane_id']}", True

    def _transition_key(self, t: AgentTransition) -> str | None:
        """Thread key for a transition. A provisional thread is adopted only on positive
        evidence: the transition's live info carries the same terminal id."""
        if t.ended:
            return t.session  # no live info: never migrate anything on an ended event
        prov = self.state.find_provisional_by_terminal((t.info or {}).get("terminal_id"))
        if t.session:
            if prov:
                self.state.rekey_thread(prov, t.session)
            return t.session
        return prov

    def _agent_title(self, info: Mapping, ws_label: str, icon: str = "🤖") -> str:
        return f"{icon} *{B.escape(agent_display_name(info))}* · {B.escape(ws_label)}"

    def _agent_context(self, info: Mapping) -> list[str]:
        parts = [f"`{info.get('agent') or '?'}`"]
        if info.get("cwd"):
            parts.append(f"📁 `{B.escape(info['cwd'])}`")
        return [" · ".join(parts)]

    # =====================================================================================
    # Slash commands (called after the command was acknowledged)
    # =====================================================================================
    def run_command(self, user: str, channel: str | None, text: str, trigger_id: str | None,
                    response_url: str | None) -> None:
        sub, rest = parse_command(text)
        if sub == "new" and not rest.strip():
            self.open_new_modal(trigger_id, channel)
            return
        if sub == "send" and not rest.strip():
            self.open_send_modal(trigger_id, channel)
            return
        responder = (lambda t, b=None: self.transport.respond(response_url, t, b)) if response_url else None
        reply = self.command_reply(sub, rest, responder)
        if reply is None:
            return
        if responder is not None:
            try:
                self._retrying(lambda: responder(reply.text, reply.blocks), idempotent=False)
                return
            except SlackUncertainError:
                log.warning("the slash command reply may not have arrived (outcome unknown); not repeated")
                return
            except Exception:
                log.exception("responding to the slash command failed")
        self._post_safe(reply.text)

    def command_reply(self, sub: str, rest: str, responder=None) -> Reply | None:
        cmd = self.cfg.slash_command
        reply_fn = (lambda t: responder(t)) if responder else None
        try:
            if sub in ("", "help", "usage"):
                return Reply(B.usage_text(cmd), B.usage_blocks(cmd))
            if sub == "list":
                agents = self.client.list_agents()
                return Reply(f"{len(agents)} agents", B.agent_list_blocks(agents, self._ws_labels()))
            if sub == "status":
                return Reply("Herdr bridge status", B.status_blocks(self.status_lines()))
            if sub == "pair":
                text = ("✅ Already paired with you. To pair another Slack account, clear "
                        "SLACK_OWNER_USER_ID in the plugin's .env and restart the bridge.")
                return Reply(text, B.notice_blocks(text))
            if sub == "new":
                return self.command_new(rest, reply_fn)
            if sub == "send":
                return self.command_send(rest, reply_fn)
        except HerdrError as exc:
            log.warning("command %s failed: %s", sub, exc)
            return Reply(f"❌ Herdr error: {exc}")
        return Reply(f"Unknown command `{B.escape(sub)}`.\n" + B.usage_text(cmd),
                     B.notice_blocks(f"Unknown command `{B.escape(sub)}`.") + B.usage_blocks(cmd))

    def status_lines(self) -> list[str]:
        uptime = format_duration(self.clock() - self.started_at)
        try:
            pong = self.client.ping()
            herdr = f"Herdr {pong.get('version', '?')} ✅"
            agents = len(self.client.list_agents())
        except HerdrError as exc:
            herdr, agents = f"Herdr ❌ {exc}", "?"
        threads = self.state.all_threads()
        pending = sum(1 for e in threads.values() if e.get("pending_task"))
        muted = sum(1 for e in threads.values() if e.get("muted"))
        stats = self.manager.stats
        return [
            f"• {herdr} · {agents} agents · {len(self.manager.subscribed_panes())} subscribed panes",
            f"• uptime {uptime} · threads {len(threads)} (pending {pending}, muted {muted})",
            f"• events {stats.events} · transitions {stats.transitions} · resyncs {stats.resyncs}"
            f" · stream errors {stats.stream_errors}",
            f"• notifications {self.stats['notifications']} · prompts {self.stats['prompts']}"
            f" · agents started {self.stats['agents_started']} · Slack retries {self.stats['slack_retries']}"
            f" · queued {self._notify_q.qsize()}",
        ]

    # --- new agent ---------------------------------------------------------------------
    def _selectable_workspaces(self) -> list[dict]:
        return [w for w in self.client.list_workspaces() if w.get("label") != self.cfg.bridge_workspace]

    def _codex_models(self):
        return codex_model_options(self.codex_home_dir)

    def build_new_view(self, st: B.NewModalState) -> dict:
        models, default = self._codex_models()
        return B.new_agent_view(st, self._selectable_workspaces(), models, default)

    def open_new_modal(self, trigger_id: str, channel: str | None) -> None:
        # Use the short-lived trigger before any Herdr I/O, then fill the modal in.
        view_id = self.transport.open_view(trigger_id, B.loading_view("New Herdr agent", B.NEW_CALLBACK))
        try:
            workspaces = self._selectable_workspaces()
            if not workspaces:
                self.transport.update_view(view_id, B.loading_view(
                    "New Herdr agent", B.NEW_CALLBACK, "No Herdr workspace is available for a new agent."))
                return
            ws = next((w for w in workspaces if w.get("focused")), workspaces[0])
            st = B.NewModalState(workspace_id=ws["workspace_id"], metadata={"channel": channel})
            st.cwd = workspace_cwd(self.client.list_panes(ws["workspace_id"]))
            models, default = self._codex_models()
            self.transport.update_view(view_id, B.new_agent_view(st, workspaces, models, default))
        except HerdrError as exc:
            self.transport.update_view(view_id, B.loading_view("New Herdr agent", B.NEW_CALLBACK,
                                                               f"❌ Herdr error: {B.escape(str(exc))}"))

    def update_new_modal(self, action_id: str, view: Mapping) -> None:
        """`dispatch_action` on workspace/kind: prefill cwd or swap kind-specific fields."""
        st = B.parse_new_view_state((view.get("state") or {}).get("values") or {})
        try:
            st.metadata = json.loads(view.get("private_metadata") or "{}")
        except ValueError:
            st.metadata = {}
        if action_id == B.ACTION_NEW_WS and st.workspace_id:
            st.cwd = workspace_cwd(self.client.list_panes(st.workspace_id))
        if action_id == B.ACTION_NEW_KIND:
            st.model = st.effort = None  # use the new kind's defaults
        self.transport.update_view(view["id"], self.build_new_view(st), view.get("hash"))

    @staticmethod
    def _local_new_errors(ws_id, name, kind, model, effort, mode, prompt) -> tuple[dict, list]:
        """Checks that need no Herdr call. Returns ({block_id: error}, launch args)."""
        errors: dict[str, str] = {}
        if not ws_id:
            errors[B.BLOCK_WS] = "Choose a workspace."
        if not prompt.strip():
            errors[B.BLOCK_PROMPT] = "Enter a prompt."
        if name:
            err = validate_agent_name(name, ())
            if err:
                errors[B.BLOCK_NAME] = err
        args: list = []
        try:
            args = build_agent_args(kind, model, effort, mode)
        except LaunchError as exc:
            errors[f"{B.BLOCK_EFFORT_PREFIX}{kind}"] = str(exc)
        return errors, args

    def submit_new_view(self, view: Mapping) -> dict | None:
        """Runs before the submission is acked: local checks + a bounded live name check."""
        st = B.parse_new_view_state((view.get("state") or {}).get("values") or {})
        if st.kind not in KIND_SPECS:
            return {B.BLOCK_KIND: "Choose claude or codex."}
        mode = st.permission_mode if KIND_SPECS[st.kind].supports_permission_mode else None
        errors, args = self._local_new_errors(st.workspace_id, st.name or None, st.kind, st.model, st.effort,
                                              mode, st.prompt)
        if errors:
            return errors
        if st.name:
            done, names = self._bounded(lambda: [a.get("name") for a in self.client.list_agents()],
                                        self.ack_budget)
            if done and st.name in names:
                return {B.BLOCK_NAME: f"An agent named {st.name!r} is already running."}
            # Not done in time: the worker re-validates and reports in the DM.
        req = NewAgentRequest(st.workspace_id, st.prompt, st.kind, st.model, st.effort, mode,
                              st.cwd or None, st.name or None, args)
        self._submit(self.run_new_agent, req, None)
        return None

    def command_new(self, rest: str, reply: Callable[[str], None] | None = None) -> Reply:
        parsed = parse_new_args(rest)
        if isinstance(parsed, str):
            return Reply(f"❌ {parsed}")
        ws = resolve_workspace(self.client.list_workspaces(), parsed.workspace)
        if ws is None:
            return Reply(f"❌ Unknown workspace `{B.escape(parsed.workspace)}`.")
        errors, args = self._local_new_errors(ws["workspace_id"], parsed.name, parsed.kind, parsed.model,
                                              parsed.effort, parsed.mode, parsed.prompt)
        if errors:
            return Reply("❌ " + " ".join(errors.values()))
        cwd = parsed.cwd or workspace_cwd(self.client.list_panes(ws["workspace_id"])) or None
        req = NewAgentRequest(ws["workspace_id"], parsed.prompt, parsed.kind, parsed.model, parsed.effort,
                              parsed.mode, cwd, parsed.name, args)
        self._submit(self.run_new_agent, req, reply)
        return Reply(f"🚀 Starting a {parsed.kind} agent in *{B.escape(ws.get('label') or ws['workspace_id'])}*…")

    def run_new_agent(self, req: NewAgentRequest, reply: Callable[[str], None] | None = None) -> None:
        """Worker: tab create -> agent start -> thread root -> prompt."""
        name = req.name or "agent"
        pane_id = None
        try:
            live = {a.get("name") for a in self.client.list_agents()}
            if req.name:
                auto = False
                err = validate_agent_name(name, live)
                if err:
                    self._reply_safe(reply, f"❌ {err}")
                    return
            else:
                auto = True
                while True:
                    name = auto_agent_name(self.state.next_counter())
                    if name not in live:
                        break
            label = tab_label(name, req.prompt, auto)
            created = self.client.create_tab(req.workspace_id, label, req.cwd)
            pane_id = created["root_pane"]["pane_id"]
            started = self.client.start_agent(name, req.kind, pane_id, req.args,
                                              timeout_ms=self.cfg.start_timeout_ms)
        except StateError as exc:
            self._reply_safe(reply, f"❌ Cannot pick an automatic name: {exc}. Give the agent a name.")
            return
        except HerdrOutcomeUnknown as exc:
            self._reply_safe(reply, f"⚠️ Herdr did not answer while starting the agent ({exc.message}). "
                                    "Check Herdr on the PC; nothing was retried.")
            return
        except HerdrError as exc:
            if exc.code == "agent_not_ready":
                self._startup_dialog(req, name, pane_id, reply)
            else:
                self._reply_safe(reply, f"❌ Could not start the agent: {B.escape(str(exc))}")
            return
        info = dict(started.get("agent") or {})
        info.setdefault("pane_id", pane_id)
        # Pin the started agent's identity: the waits below must never adopt another agent
        # that took over the pane in the meantime.
        expected = {"session": session_of(info), "terminal_id": info.get("terminal_id")}
        if not session_of(info):
            info = self._wait_for_session(pane_id, expected, info)
        # Herdr may report a just-started agent as `unknown` for a moment: wait until it has
        # settled so the D6 gate in _admit() sees its real state.
        if info is not None:
            info = self._wait_settled(pane_id, info, expected)
        if info is None:
            self._reply_safe(reply, f"🔄 *{B.escape(name)}* was replaced by a different agent while starting; "
                                    "the prompt was not sent.")
            return
        info.setdefault("workspace_id", req.workspace_id)
        info.setdefault("agent", req.kind)
        info.setdefault("name", name)
        if req.kind == KIND_CODEX:
            if self.cfg.codex_prompt_delay > 0:
                self.sleep(self.cfg.codex_prompt_delay)  # Codex drops input pasted while it initializes
            # Codex reports `idle` while its folder-trust screen is up: treat that as blocked. Checked
            # after the delay (the screen may be drawn during it), on a few reads.
            if self._codex_trust_shown(info["pane_id"]):
                self._startup_dialog(req, name, info["pane_id"], reply, info=info, idle=True)
                return
        self.stats["agents_started"] += 1
        ws_label = self._ws_labels().get(req.workspace_id, req.workspace_id)
        # Admit by the identity pinned at start: never by whatever the pane showed last.
        target = Target(info["pane_id"], expected.get("session"), expected.get("terminal_id"), info.get("name"))
        self.send(target, req.prompt, reply, fresh=True, root=self._new_root(req, info, ws_label))

    def _codex_trust_shown(self, pane_id: str) -> bool:
        for attempt in range(DIALOG_READ_RETRIES + 1):
            if attempt:
                self.sleep(DIALOG_POLL_INTERVAL)
            dialog = parse_dialog(self._visible(pane_id) or "", KIND_CODEX)
            if dialog is not None and dialog.kind == KIND_TRUST:
                return True
        return False

    def _new_root(self, req: NewAgentRequest, info: Mapping, ws_label: str) -> tuple[str, list[str]]:
        """Thread root (title, lines) of an agent started from Slack."""
        opts = " · ".join(a for a in (req.model, req.effort, req.mode if req.kind != KIND_CODEX else None) if a)
        lines = [f"`{req.kind}`" + (f" · {B.escape(opts)}" if opts else "")]
        if info.get("cwd") or req.cwd:
            lines.append(f"📁 `{B.escape(info.get('cwd') or req.cwd)}`")
        lines.append("📨 " + B.escape(truncate(_norm(req.prompt), PROMPT_EXCERPT)))
        return self._agent_title(info, ws_label, "🚀"), lines

    def _startup_dialog(self, req: NewAgentRequest, name: str, pane_id: str | None, reply,
                        info: dict | None = None, idle: bool = False) -> None:
        """A new agent waits for an answer while starting (folder trust): open its thread with the
        dialog and keep the prompt; it is sent once the dialog is answered and the agent is idle.
        `idle`: the agent reports idle although the dialog is up (Codex)."""
        if info is None and pane_id:
            try:
                info = self.client.find_agent(pane_id)
            except HerdrError as exc:
                log.warning("finding the starting agent in %s failed: %s", pane_id, exc)
        if info is not None and info.get("agent_status") != "blocked" and not idle:
            # Not reported blocked (yet): only a folder-trust screen counts as a startup dialog.
            dialog = parse_dialog(self._visible(info.get("pane_id") or pane_id) or "", req.kind)
            idle = dialog is not None and dialog.kind == KIND_TRUST
            if not idle:
                info = None
        if info is None:
            self._reply_safe(reply, f"⚠️ *{B.escape(name)}* is waiting for confirmation on PC during "
                                    "startup. Confirm it there, then use send.")
            return
        info = dict(info)
        info.setdefault("pane_id", pane_id)
        info.setdefault("workspace_id", req.workspace_id)
        info.setdefault("agent", req.kind)
        info.setdefault("name", name)
        ws_label = self._ws_labels().get(req.workspace_id, req.workspace_id)
        title, lines = self._new_root(req, info, ws_label)
        seq = _seq(info)
        answer_id = self._answer_id(info.get("terminal_id"), info.get("pane_id"))
        try:
            with self._answer_lock(answer_id), self._admission_lock:
                key, provisional = self._key_for(info)
                with self._lock(key):
                    entry = self._retrying(lambda: self._ensure_thread(key, info, title, lines, ORIGIN_SLACK,
                                                                       provisional=provisional))
                    fields = {"deferred_prompt": {"text": req.prompt, "at": self.clock()}}
                    if seq:
                        fields["last_blocked_seq"] = seq
                    if entry.get("dialog"):
                        # The notifier posted this agent's dialog already: keep it, add the prompt.
                        self._commit(key, None, **fields)
                    else:
                        op = f"blocked:{info.get('terminal_id') or key}:startup"
                        self._retrying(lambda: self._open_dialog(key, entry, info, ws_label, op, idle=idle,
                                                                 **fields))
        except Exception:
            log.exception("posting the startup dialog failed")
            self._reply_safe(reply, f"❌ *{B.escape(name)}* is waiting for an answer during startup, but posting "
                                    "it to Slack failed. Answer it on PC, then use send.")
            return
        self.stats["agents_started"] += 1
        self._reply_safe(reply, f"⚠️ *{B.escape(name)}* is waiting for an answer during startup: answer it in "
                                "its thread. The prompt is sent once it is answered.")

    @staticmethod
    def _same_agent(live: Mapping, expected: dict) -> bool:
        """Is `live` the agent we started? Pins identity parts as they become known."""
        tid, session = live.get("terminal_id"), session_of(live)
        if expected.get("terminal_id") and tid and tid != expected["terminal_id"]:
            return False
        if expected.get("session") and session and session != expected["session"]:
            return False
        if tid and not expected.get("terminal_id"):
            expected["terminal_id"] = tid
        if session and not expected.get("session"):
            expected["session"] = session
        return True

    def _observe(self, pane_id: str, info: dict, expected: dict) -> tuple[dict | None, bool]:
        """(merged info or None if replaced, whether a live observation was made)."""
        try:
            live = self.client.find_agent(pane_id)
        except HerdrError:
            live = None
        if live is None:
            return info, False
        if not self._same_agent(live, expected):
            log.warning("pane %s now hosts a different agent than the one just started", pane_id)
            return None, True
        merged = {**info, **live}
        # A snapshot without a session / terminal must not erase what is already pinned.
        if expected.get("session") and not session_of(live):
            merged["agent_session"] = {"value": expected["session"]}
        if expected.get("terminal_id") and not live.get("terminal_id"):
            merged["terminal_id"] = expected["terminal_id"]
        return merged, True

    def _wait_settled(self, pane_id: str, info: dict, expected: dict | None = None,
                      timeout: float = 20.0) -> dict | None:
        expected = expected if expected is not None else {"session": session_of(info),
                                                          "terminal_id": info.get("terminal_id")}
        deadline = self.clock() + timeout
        while info.get("agent_status") not in SETTLED and self.clock() < deadline:
            self.sleep(0.5)
            info, _ = self._observe(pane_id, info, expected)
            if info is None:
                return None
        return info

    def _wait_for_session(self, pane_id: str, expected: dict, info: dict, attempts: int = 4) -> dict | None:
        for _ in range(attempts):
            merged, _ = self._observe(pane_id, info, expected)
            if merged is None:
                return None
            info = merged
            if session_of(info):
                return info
            self.sleep(0.5)
        return info

    # --- send --------------------------------------------------------------------------
    def _precheck(self, agent: Mapping) -> str | None:
        """Early, advisory rejection (the worker re-checks everything under the lock)."""
        check = check_send_allowed(agent.get("agent_status"))
        if not check.ok:
            return REASON_TEXT.get(check.reason, REASON_TEXT["unknown"]).format(
                name=B.escape(agent_display_name(agent)))
        return None

    def command_send(self, rest: str, reply: Callable[[str], None] | None = None) -> Reply:
        parsed = parse_send_args(rest)
        if isinstance(parsed, str):
            return Reply(f"❌ {parsed}")
        token, text = parsed
        agent = resolve_agent(self.client.list_agents(), token)
        if agent is None:
            return Reply(f"❌ No agent named or at `{B.escape(token)}`.")
        error = self._precheck(agent)
        if error:
            return Reply(error)
        self._submit(self.send, target_of(agent), text, reply)
        return Reply(f"📨 Sending to *{B.escape(agent_display_name(agent))}*…")

    def open_send_modal(self, trigger_id: str, channel: str | None, initial_target: str | None = None) -> None:
        # Ack-first: the trigger is used before any Herdr I/O; the preview is fetched afterwards.
        view_id = self.transport.open_view(trigger_id, B.loading_view("Send to agent", B.SEND_CALLBACK))
        try:
            agents = self.client.list_agents()
            if not agents:
                self.transport.update_view(view_id, B.loading_view("Send to agent", B.SEND_CALLBACK,
                                                                   "No agents are running in Herdr."))
                return
            values = {a.get("name") or a["pane_id"] for a in agents}
            target = initial_target if initial_target in values else None
            labels = self._ws_labels()
        except HerdrError as exc:
            self.transport.update_view(view_id, B.loading_view("Send to agent", B.SEND_CALLBACK,
                                                               f"❌ Herdr error: {B.escape(str(exc))}"))
            return
        with self._send_lock:  # bookkeeping only: never any Slack/Herdr I/O under this lock
            rec = {"gen": 1, "sel_ts": 0.0, "target": target, "hash": None, "prompt": "", "agents": agents,
                   "labels": labels, "metadata": {"channel": channel}}
            self._send_views[view_id] = rec
            while len(self._send_views) > 20:  # forget the oldest modals
                self._send_views.pop(next(iter(self._send_views)))
            view = self._send_view_for(rec, self._preview_placeholder(target))
        self._apply_send_update(view_id, 1, view, None, retry=False)
        if target:
            self._submit(self._load_preview, view_id, 1, target)

    # --- send modal: last-response preview -------------------------------------------------
    #
    # Concurrency: `_send_lock` only guards the per-view records (short, no I/O), so a view
    # submission never waits for preview work. Each record has a generation `gen`, bumped by
    # every accepted selection; selections carry Slack's `action_ts` and an older/duplicate one
    # is dropped before it touches the record. An update is applied only for the current
    # generation, and a hash_conflict is retried (with the newest hash we know) only while that
    # generation is still the latest selection.

    @staticmethod
    def _preview_placeholder(target: str | None) -> list | None:
        if not target:
            return None  # "pick an agent" hint
        return [B.context(f"_{B.PREVIEW_LOADING}_") | {"block_id": B.BLOCK_PREVIEW_HEAD}]

    @staticmethod
    def _send_view_for(rec: Mapping, preview: list | None) -> dict:
        return B.send_view(rec["agents"], rec["labels"], rec["target"], rec["metadata"], preview=preview,
                           prompt=rec.get("prompt") or "")

    def _is_current(self, view_id: str, gen: int) -> dict | None:
        with self._send_lock:
            rec = self._send_views.get(view_id)
            return rec if rec is not None and rec["gen"] == gen else None

    def _apply_send_update(self, view_id: str, gen: int, view: dict, view_hash: str | None,
                           retry: bool = True) -> bool:
        """views.update outside the lock. On hash_conflict, retry once with our newest hash, but
        only if `gen` is still the latest selection. Returns True if applied."""
        for attempt in range(2):
            try:
                new_hash = self.transport.update_view(view_id, view, view_hash)
            except SlackPermanentError as exc:
                if exc.error != "hash_conflict":
                    log.warning("updating the send modal failed: %s", exc.error)
                    return False
                with self._send_lock:
                    rec = self._send_views.get(view_id)
                    latest = rec is not None and rec["gen"] == gen
                    newer_hash = rec.get("hash") if rec else None
                if not (retry and attempt == 0 and latest and newer_hash and newer_hash != view_hash):
                    log.info("send modal changed meanwhile; update for generation %d dropped", gen)
                    return False
                view_hash = newer_hash
                continue
            except Exception:
                log.exception("updating the send modal failed")
                return False
            with self._send_lock:
                rec = self._send_views.get(view_id)
                if rec is not None and new_hash:
                    rec["hash"] = new_hash  # the view's current hash, whatever generation set it
            return True
        return False

    def update_send_modal(self, view: Mapping, action_ts: str | float | None = None) -> None:
        """`dispatch_action` on the agent select (acked by the caller): show "loading", then fetch
        that agent's preview. Selections older than (or equal to) the last accepted one are ignored."""
        target, prompt = B.parse_send_view_state((view.get("state") or {}).get("values") or {})
        view_id = view.get("id")
        try:
            sel_ts = float(action_ts or 0)
        except (TypeError, ValueError):
            sel_ts = 0.0
        with self._send_lock:
            known = view_id in self._send_views
        agents = labels = None
        if not known:  # e.g. opened before a bridge restart: rebuild (I/O outside the lock)
            try:
                agents, labels = self.client.list_agents(), self._ws_labels()
            except HerdrError:
                return
        with self._send_lock:
            rec = self._send_views.get(view_id)
            if rec is None:
                try:
                    metadata = json.loads(view.get("private_metadata") or "{}")
                except ValueError:
                    metadata = {}
                rec = {"gen": 0, "sel_ts": 0.0, "hash": None, "agents": agents or [], "labels": labels or {},
                       "metadata": metadata}
                self._send_views[view_id] = rec
            if sel_ts and sel_ts <= rec.get("sel_ts", 0.0):
                log.info("ignoring an older/duplicate agent selection (%s <= %s)", sel_ts, rec.get("sel_ts"))
                return
            rec["sel_ts"] = max(sel_ts, rec.get("sel_ts", 0.0))
            rec["gen"] += 1
            rec["target"], rec["prompt"] = target, prompt
            gen = rec["gen"]
            placeholder = self._send_view_for(rec, self._preview_placeholder(target))
        self._apply_send_update(view_id, gen, placeholder, view.get("hash"))
        if target:
            self._submit(self._load_preview, view_id, gen, target)

    def preview_blocks(self, agent: Mapping | None) -> list:
        """The agent's last response (same source as completion results), never raising."""
        if agent is None:
            return B.last_response_blocks(None, note=B.PREVIEW_FAILED)
        if (agent.get("agent_status") or "") in ("working", "blocked"):
            return B.last_response_blocks(None, note=B.PREVIEW_BUSY)
        try:
            pane = agent.get("pane_id")
            res = agent_result(agent.get("agent"), session_of(agent), agent.get("cwd"),
                               lambda: self._read_screen(pane), fallback_lines=self.cfg.fallback_lines,
                               base=self.claude_projects)
        except Exception:
            log.exception("loading the last response for the send modal failed")
            return B.last_response_blocks(None, note=B.PREVIEW_FAILED)
        when = ""
        if res.at:
            ago = max(0.0, time.time() - res.at)
            when = "just now" if ago < 60 else f"{int(ago // 60)} min ago" if ago < 3600 else \
                time.strftime("%m-%d %H:%M", time.localtime(res.at))
        return B.last_response_blocks(res.text, when, res.duration or "", markdown=res.source != "tail")

    def _load_preview(self, view_id: str, gen: int, target: str) -> None:
        """Worker: fetch the preview (no lock held) and apply it only if `gen` is still current."""
        if self._is_current(view_id, gen) is None:
            return  # already stale: skip the Herdr work
        try:
            agent = resolve_agent(self.client.list_agents(), target)
        except HerdrError:
            agent = None
        preview = self.preview_blocks(agent)
        with self._send_lock:
            rec = self._send_views.get(view_id)
            if rec is None or rec["gen"] != gen or rec["target"] != target:
                return  # stale: a newer selection owns the modal
            view, view_hash = self._send_view_for(rec, preview), rec.get("hash")
        self._apply_send_update(view_id, gen, view, view_hash)

    def submit_send_view(self, view: Mapping) -> dict | None:
        """Runs before the submission is acked: local checks + a bounded live check."""
        token, prompt = B.parse_send_view_state((view.get("state") or {}).get("values") or {})
        with self._send_lock:
            self._send_views.pop(view.get("id"), None)
        errors = {}
        if not prompt.strip():
            errors[B.BLOCK_PROMPT] = "Enter a prompt."
        if not token:
            errors[B.BLOCK_TARGET] = "Choose an agent."
        if errors:
            return errors
        done, agent = self._bounded(lambda: resolve_agent(self.client.list_agents(), token), self.ack_budget)
        if done:
            if agent is None:
                return {B.BLOCK_TARGET: f"No agent named or at {token}."}
            error = self._precheck(agent)
            if error:
                return {B.BLOCK_TARGET: _strip_mrkdwn(error)}
            self._submit(self.send, target_of(agent), prompt, None)
        else:
            self._submit(self._send_by_token, token, prompt)
        return None

    def _send_by_token(self, token: str, text: str) -> None:
        agent = resolve_agent(self.client.list_agents(), token)
        if agent is None:
            self._post_safe(f"❌ No agent named or at `{B.escape(token)}`; nothing was sent.")
            return
        self.send(target_of(agent), text, None)

    # --- DM messages (thread replies) -------------------------------------------------------
    def handle_dm_message(self, channel: str, user: str, text: str, ts: str, thread_ts: str | None) -> None:
        if not thread_ts or thread_ts == ts:
            self.transport.post_ephemeral(channel, user, "Use the slash command, or reply in an agent's thread.",
                                          B.usage_blocks(self.cfg.slash_command))
            return
        key = self.state.find_session_by_thread(channel, thread_ts)
        if not key:
            self.transport.post_message(channel, "This thread is not bound to an agent.", None, thread_ts)
            return
        entry = self.state.get_thread(key) or {}
        provisional = bool(entry.get("provisional"))
        target = Target(entry.get("pane_id") or "", None if provisional else key, entry.get("terminal_id"),
                        entry.get("agent_name"), key)
        if entry.get("dialog"):
            # D6 revised: while the agent's dialog is open, a reply is its free-text answer.
            self._submit(self.answer_thread_reply, key, target, text, thread_ts)
            return
        self._submit(self.send, target, text, None, False, None, thread_ts)

    # --- admission + delivery -----------------------------------------------------------
    def _reject(self, reason: str, name: str, reply, thread_ts: str | None) -> None:
        text = REASON_TEXT.get(reason, reason).format(name=B.escape(name or "agent"))
        self._reply_safe(reply, text, thread_ts)

    def _admit(self, target: Target, text: str, reply, root: tuple | None,
               thread_hint: str | None) -> Admission | None:
        """Atomically: verify identity + status, settle/refuse older pending work, reserve a task,
        and create or reuse the thread root. Returns None after posting a rejection."""
        with self._admission_lock:
            info, reason = self._resolve(target)
            if info is None:
                self._reject(reason, target.name or target.pane_id, reply, thread_hint)
                return None
            name = info.get("name") or target.name or info["pane_id"]
            key, provisional = self._key_for(info)
            if target.key and target.key != key and not session_of(info):
                key = target.key  # thread-bound provisional key
            entry = self.state.get_thread(key) or {}
            thread_ts = entry.get("thread_ts") or thread_hint
            check = check_send_allowed(info.get("agent_status"))
            if not check.ok:
                self._reject(check.reason, name, reply, thread_ts)
                return None
            pending = entry.get("pending_task")
            if pending:
                if info.get("agent_status") in SETTLED and _seq(info) > int(pending.get("seq0") or 0):
                    # The previous task finished but its completion is not processed yet: settle it.
                    if not self._complete_pending_now(key, info, pending):
                        self._reject("pending", name, reply, thread_ts)
                        return None
                elif self.clock() - float(pending.get("started_at") or 0) < PENDING_GRACE_SECONDS:
                    self._reject("pending", name, reply, thread_ts)
                    return None
                # else: an old reservation the agent never reacted to; replace it.
            task_id = uuid.uuid4().hex
            echo_op = None
            try:
                if entry.get("thread_ts"):
                    if not thread_hint:  # a thread reply is already visible in the thread: no echo
                        echo_op = f"sent:{task_id}"
                        self._retrying(lambda: self._post("📨 " + _norm(text)[:200], B.sent_blocks(text),
                                                          entry["thread_ts"], op=echo_op, key=key))
                else:
                    ws_label = self._ws_labels().get(info.get("workspace_id"), info.get("workspace_id") or "")
                    title, lines = root or (self._agent_title(info, ws_label),
                                            self._agent_context(info) +
                                            ["📨 " + B.escape(truncate(_norm(text), PROMPT_EXCERPT))])
                    entry = self._retrying(lambda: self._ensure_thread(key, info, title, lines, ORIGIN_SLACK,
                                                                       provisional=provisional))
            except Exception:
                log.exception("creating the Slack thread failed")
                self._reply_safe(reply, f"❌ Could not post to Slack; nothing was sent to *{B.escape(name)}*.")
                return None
            task = {"task_id": task_id, "started_at": self.clock(), "seq0": _seq(info),
                    "working_announced": False, "prompt": truncate(_norm(text), PROMPT_EXCERPT)}
            with self._lock(key):
                entry = self._commit(key, echo_op, pending_task=task)
            return Admission(key, entry, info, task)

    def send(self, target: Target, text: str, reply=None, fresh: bool = False, root: tuple | None = None,
             thread_hint: str | None = None) -> DeliveryResult | None:
        admission = self._admit(target, text, reply, root, thread_hint)
        if admission is None:
            return None
        result = self._deliver(admission, text, fresh)
        self._after_delivery(admission, result)
        return result

    def _deliver(self, adm: Admission, text: str, fresh: bool) -> DeliveryResult:
        target = target_of(adm.info)
        attempts = FRESH_PROMPT_RETRIES if fresh else 1
        for attempt in range(1, attempts + 1):
            # Re-validate identity right before writing input (the name/pane may have been reused).
            try:
                live = self.client.find_agent(target.pane_id)
            except HerdrError as exc:
                return DeliveryResult(Delivery.FAILED, str(exc))
            if live is not None and not self._identity_ok(live, target):
                return DeliveryResult(Delivery.CHANGED)
            if live is None and not fresh:
                return DeliveryResult(Delivery.CHANGED)
            try:
                self.client.prompt_agent(target.pane_id, text)
                return DeliveryResult(Delivery.SENT)
            except HerdrError as exc:
                if exc.code in ("agent_not_ready", "agent_not_found") and attempt < attempts:
                    # Raised while resolving the target, before input is written: safe to retry.
                    log.info("prompt to fresh agent %s: %s; retrying", target.pane_id, exc.code)
                    self.sleep(1.0)
                    continue
                if exc.code == "agent_blocked":
                    return DeliveryResult(Delivery.BLOCKED)
                if exc.code == "agent_prompt_stalled" or isinstance(exc, HerdrOutcomeUnknown):
                    return self._verify_unknown(target, text, _seq(adm.info), exc)
                return DeliveryResult(Delivery.FAILED, str(exc))
        return DeliveryResult(Delivery.FAILED, "the agent never became addressable")

    def _verify_unknown(self, target: Target, text: str, seq0: int, exc: HerdrError) -> DeliveryResult:
        """A stalled / unknown prompt is never re-sent: only watch for activity and report."""
        log.warning("prompt to %s: %s; watching for activity (no resend)", target.pane_id, exc)
        if self._wait_activity(target, seq0):
            return DeliveryResult(Delivery.SENT)
        try:
            screen = self.client.read_agent_once(target.pane_id, 60, source="visible")
        except HerdrError:
            screen = None
        if screen is not None and prompt_visible(screen, text):
            return DeliveryResult(Delivery.UNCERTAIN,
                                  "The prompt reached the agent but it has not started. Check it on the PC")
        return DeliveryResult(Delivery.UNCERTAIN,
                              "Delivery is uncertain and was not retried. Check the agent on the PC")

    def _wait_activity(self, target: Target, seq0: int) -> bool:
        deadline = self.clock() + self.cfg.stall_wait
        while True:
            try:
                info = self.client.find_agent(target.pane_id)
            except HerdrError:
                info = None
            if self._identity_ok(info, target) and (
                    _seq(info) > seq0 or info.get("agent_status") in ("working", "blocked")):
                return True
            if self.clock() >= deadline:
                return False
            self.sleep(0.5)

    def _after_delivery(self, adm: Admission, result: DeliveryResult) -> None:
        name = B.escape(adm.info.get("name") or adm.entry.get("agent_name") or adm.info["pane_id"])
        key = adm.key if self.state.get_thread(adm.key) is not None else (
            self.state.find_session_by_thread(self.dm_channel, adm.entry.get("thread_ts")) or adm.key)
        thread_ts = adm.entry.get("thread_ts")
        if result.outcome is Delivery.SENT:
            self.stats["prompts"] += 1
            return
        if result.outcome is not Delivery.UNCERTAIN:
            self._clear_task(key, adm.task["task_id"])
        if result.outcome is Delivery.BLOCKED:
            self._post_safe(REASON_TEXT[REASON_BLOCKED].format(name=name), thread_ts)
        elif result.outcome is Delivery.CHANGED:
            self._post_safe(REASON_TEXT[REASON_SESSION_CHANGED], thread_ts)
        elif result.outcome is Delivery.UNCERTAIN:
            self._post_safe(f"⚠️ {result.message} (*{name}*)", thread_ts)
        else:
            self._post_safe(f"❌ Could not send to *{name}*: {B.escape(result.message)}", thread_ts)

    def _clear_task(self, key: str, task_id: str) -> None:
        """Clear the pending task only if it is still the given task."""
        with self._lock(key):
            entry = self.state.get_thread(key) or {}
            if (entry.get("pending_task") or {}).get("task_id") == task_id:
                self.state.set_pending_task(key, None)

    def _complete_pending_now(self, key: str, info: Mapping, pending: Mapping) -> bool:
        """Settle a finished-but-unprocessed task during admission. False if Slack is unreachable."""
        try:
            with self._lock(key):
                entry = self.state.get_thread(key) or {}
                current = entry.get("pending_task") or {}
                if current.get("task_id") != pending.get("task_id"):
                    return True  # settled meanwhile
                if _seq(info) > int(entry.get("last_result_seq") or 0):
                    ws_label = self._ws_labels().get(info.get("workspace_id"), info.get("workspace_id") or "")
                    op = _result_op(key, current, info)
                    self._retrying(lambda: self.post_result(key, entry, info, ws_label, current, op=op))
                    self._commit(key, op, last_result_seq=_seq(info), pending_task=None)
                else:
                    self.state.set_pending_task(key, None)
            return True
        except Exception:
            log.exception("settling the previous task failed")
            return False

    # =====================================================================================
    # Actions
    # =====================================================================================
    def toggle_mute(self, channel: str, message: Mapping, value: str) -> None:
        try:
            data = json.loads(value)
            button_key, muted = data["s"], bool(data["m"])
        except (ValueError, KeyError, TypeError):
            return
        # Resolve through the thread binding: the button may carry a pre-migration key.
        key = self.state.find_session_by_thread(channel, message.get("thread_ts") or message.get("ts"))
        if key is None and self.state.get_thread(button_key) is not None:
            key = button_key
        if key is None:
            log.warning("mute toggle for an unknown thread %s", message.get("ts"))
            return
        self.state.set_muted(key, muted)
        blocks = B.with_mute_button(message.get("blocks") or [], key, muted)
        self.transport.update_message(channel, message["ts"], message.get("text") or "Herdr agent", blocks)

    def show_full(self, channel: str, message: Mapping, result_id: str) -> None:
        loaded = self.results.load(result_id)
        thread_ts = message.get("thread_ts") or message.get("ts")
        if loaded is None:
            self.transport.post_message(channel, "The full text is no longer available.", None, thread_ts)
            return
        text, meta = loaded
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(meta.get("at") or self.clock()))
        name = re.sub(r"[^A-Za-z0-9_-]+", "-", meta.get("name") or "result")
        self.transport.upload_text_file(channel, thread_ts, f"{name}-{stamp}.md", text,
                                        f"{meta.get('name') or 'Result'} (full)")

    # =====================================================================================
    # Blocked dialogs, answered from Slack
    #
    # A blocked agent's dialog is posted with one button per option. Its pending record lives
    # in the thread entry (`dialog`): {token, fingerprint, kind, options, message_ts, ...}; the
    # buttons carry only the token. The message and its record are built once per post op and
    # persisted (`dialog_pending`) before the first attempt, so a retried post can never pair
    # the buttons with another screen's record. Before any key is sent the live agent and screen
    # must still match the record (same session, still blocked, same fingerprint).
    #
    # Locks: answering (keys + confirmation polling), transition handling and startup dialogs of
    # one agent are serialized by its answer lock, keyed by the terminal id (stable across the
    # provisional -> session re-key). Order: answer lock, then the admission lock, then the
    # per-key lock. The answer worker never takes the admission lock while it holds its lock.
    # =====================================================================================
    @staticmethod
    def _answer_id(terminal_id: str | None, fallback: str | None) -> str:
        return terminal_id or fallback or ""

    def _transition_answer_id(self, t: AgentTransition) -> str:
        terminal = (t.info or {}).get("terminal_id")
        if not terminal and t.session:
            terminal = (self.state.get_thread(t.session) or {}).get("terminal_id")
        if not terminal and t.pane_id:  # e.g. an ended transition of a still provisional thread
            prov = self.state.find_provisional(t.pane_id)
            terminal = (self.state.get_thread(prov) or {}).get("terminal_id") if prov else None
        return self._answer_id(terminal, t.session or t.pane_id)

    def _key_now(self, key: str, terminal_id: str | None) -> str:
        """`key`, or the key its entry moved to (provisional -> session) if it was re-keyed."""
        if self.state.get_thread(key) is not None or not terminal_id:
            return key
        for other, entry in self.state.all_threads().items():
            if entry.get("terminal_id") == terminal_id:
                return other
        return key

    def _visible(self, pane_id: str | None) -> str | None:
        """The pane's visible screen, or None when it could not be read."""
        if not pane_id:
            return None
        try:
            return self.client.read_agent_once(pane_id, DIALOG_READ_LINES, source="visible")
        except HerdrError as exc:
            log.warning("reading the screen of %s failed: %s", pane_id, exc)
            return None

    def _read_dialog(self, pane_id: str | None, agent_kind: str | None) -> tuple[str | None, Dialog | None]:
        """Screen + parsed dialog. An unparsed screen is read again (it may not be drawn yet)."""
        screen = self._visible(pane_id)
        dialog = parse_dialog(screen or "", agent_kind)
        for _ in range(DIALOG_READ_RETRIES):
            if dialog is not None or screen is None:
                break
            self.sleep(DIALOG_POLL_INTERVAL)
            screen = self._visible(pane_id)
            dialog = parse_dialog(screen or "", agent_kind)
        return screen, dialog

    def _plan_text(self, dialog: Dialog | None) -> str | None:
        """The full plan from the plan file named in a plan approval dialog, if readable."""
        if dialog is None or not dialog.plan_file:
            return None
        raw = dialog.plan_file
        if raw.startswith("~"):
            path = (self.home_dir or Path.home()) / raw[1:].replace("\\", "/").lstrip("/")
        else:
            path = Path(raw)
        if path.suffix.lower() != ".md" or path.parent.name != "plans" or path.parent.parent.name != ".claude":
            return None
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                return f.read(PLAN_FILE_MAX)
        except OSError as exc:
            log.info("plan file %s is not readable: %s", path, exc)
            return None

    @staticmethod
    def _dialog_fields(dialog: Dialog | None, screen: str | None) -> dict:
        """The record fields that describe what the message shows."""
        if dialog is None:
            return {"fingerprint": screen_fingerprint(screen or ""), "kind": KEYPAD, "options": [],
                    "question": "", "summary": B.dialog_summary(None), "plan_hash": None}
        return {"fingerprint": dialog.fingerprint, "kind": dialog.kind,
                "options": [{"n": o.number, "label": o.label, "free_text": o.free_text, "chat": o.chat,
                             "checked": o.checked, "typed": typed_text(o)} for o in dialog.options],
                "question": truncate(dialog.question, 500), "summary": B.dialog_summary(dialog), "plan_hash": None}

    def _render(self, name: str, ws_label: str, dialog: Dialog | None, screen: str | None, token: str,
                note: str | None = None, session: str | None = None) -> tuple[str, list, dict]:
        """(fallback text, blocks, record fields) of a dialog message."""
        fields = self._dialog_fields(dialog, screen)
        plan_text = full_id = None
        if dialog is not None and dialog.kind == KIND_PLAN:
            plan_text = self._plan_text(dialog)
            if plan_text is not None:
                fields["plan_hash"] = _text_hash(plan_text)  # a re-plan in the same file is a change
            full = plan_text if plan_text is not None else "\n".join(dialog.body)
            if len(B.to_mrkdwn(full)) > B.PLAN_MAX_CHARS:
                full_id = self.results.save(full, {"name": name, "session": session, "at": self.clock()})
        blocks = B.dialog_blocks(name, ws_label, dialog, token, plan_text=plan_text, full_id=full_id,
                                 screen_tail=tail_lines(screen or "", B.KEYPAD_TAIL_LINES), note=note)
        return _strip_mrkdwn(B.dialog_header(name, ws_label)), blocks, fields

    def _open_dialog(self, key: str, entry: Mapping, info: Mapping, ws_label: str, op: str,
                     idle: bool = False, **fields) -> None:
        """Post the dialog of a blocked agent in its thread and store the pending record (the caller
        holds the thread lock). `fields` are committed together with the record."""
        pending = (self.state.get_thread(key) or {}).get("dialog_pending")
        if not pending or pending.get("op") != op:
            pane = info.get("pane_id") or entry.get("pane_id")
            agent_kind = info.get("agent") or entry.get("kind")
            screen, dialog = self._read_dialog(pane, agent_kind)
            token = uuid.uuid4().hex[:16]
            name = _display(info, entry)
            text, blocks, shown = self._render(name, ws_label, dialog, screen, token, session=session_of(info) or key)
            record = {"token": token, **shown, "thread_ts": entry.get("thread_ts"), "channel": self.dm_channel,
                      "agent_session": session_of(info), "terminal_id": info.get("terminal_id"), "pane_id": pane,
                      "agent_kind": agent_kind, "name": name, "ws": ws_label, "idle": idle}
            # Durable before the first attempt: every retry posts (or reuses) exactly this message
            # and commits exactly this record.
            pending = {"op": op, "text": text, "blocks": blocks, "record": record}
            self.state.upsert_thread(key, dialog_pending=pending)
        ts = self._post(pending["text"], pending["blocks"], entry.get("thread_ts"), op=op, key=key)
        record = dict(pending["record"], message_ts=ts, created_at=self.clock())
        self._commit(key, op, dialog=record, dialog_pending=None, **fields)

    def _dialog_agent(self, rec: Mapping) -> dict | None:
        """The live agent a dialog record belongs to (identity-checked). Raises HerdrError."""
        if rec.get("agent_session"):
            return self._find_session_agent(rec["agent_session"], rec.get("pane_id"))
        info = self.client.find_agent(rec["pane_id"]) if rec.get("pane_id") else None
        if info is not None and rec.get("terminal_id") and info.get("terminal_id") != rec["terminal_id"]:
            return None
        return info

    def _dialog_state(self, rec: Mapping) -> tuple[str, dict | None, Dialog | None, str | None]:
        """(state, live info, parsed dialog, screen). state: `open` (the message still shows the live
        dialog), `changed` (a different dialog / screen), `answered` (no longer waiting), `gone`, or
        `unreadable`. Raises HerdrError when Herdr could not be asked."""
        info = self._dialog_agent(rec)
        if info is None:
            return "gone", None, None, None
        if info.get("agent_status") != "blocked" and not rec.get("idle"):
            return "answered", info, None, None
        screen = self._visible(info.get("pane_id"))
        if screen is None:
            return "unreadable", info, None, None
        dialog = parse_dialog(screen, rec.get("agent_kind") or info.get("agent"), _typed_row_hint(rec))
        if rec.get("idle") and (dialog is None or dialog.kind != KIND_TRUST):
            return "answered", info, dialog, screen  # the startup screen reported as idle is gone
        if rec.get("kind") == KEYPAD:
            same = screen_fingerprint(screen) == rec.get("fingerprint")
        else:
            same = dialog is not None and dialog.fingerprint == rec.get("fingerprint")
            if same and rec.get("plan_hash"):
                plan = self._plan_text(dialog)
                same = plan is not None and _text_hash(plan) == rec["plan_hash"]
        return ("open" if same else "changed"), info, dialog, screen

    def _close_dialog(self, key: str, rec: Mapping, outcome: str) -> None:
        """Replace the dialog message's buttons by `outcome` and drop the record (best effort). The
        message is remembered as closed, so a late click on it leaves the outcome alone."""
        key = self.state.find_dialog(rec.get("token")) or key
        blocks = B.dialog_closed_blocks(rec.get("name") or "agent", rec.get("ws") or "", rec.get("summary") or "",
                                        outcome)
        try:
            self._retrying(lambda: self.transport.update_message(rec.get("channel") or self.dm_channel,
                                                                 rec["message_ts"], _strip_mrkdwn(outcome), blocks))
        except Exception:
            log.warning("removing the buttons of a dialog message failed", exc_info=True)
        with self._lock(key):
            entry = self.state.get_thread(key) or {}
            if (entry.get("dialog") or {}).get("token") == rec.get("token"):
                closed = [ts for ts in entry.get("closed_dialogs") or [] if ts != rec.get("message_ts")]
                closed = (closed + [rec.get("message_ts")])[-CLOSED_DIALOGS_KEPT:]
                self.state.upsert_thread(key, dialog=None, closed_dialogs=closed)

    def _rerender_dialog(self, key: str, rec: Mapping, dialog: Dialog | None, screen: str | None,
                         note: str | None = None) -> dict:
        """Edit the dialog message to show the current dialog, under a new token."""
        key = self.state.find_dialog(rec.get("token")) or key
        token = uuid.uuid4().hex[:16]
        text, blocks, shown = self._render(rec.get("name") or "agent", rec.get("ws") or "", dialog, screen, token,
                                           note, rec.get("agent_session") or key)
        try:
            self._retrying(lambda: self.transport.update_message(rec.get("channel") or self.dm_channel,
                                                                 rec["message_ts"], text, blocks))
        except Exception:
            log.warning("updating a dialog message failed; its old buttons now report it out of date", exc_info=True)
        new = dict(rec, token=token, **shown)
        with self._lock(key):
            current = (self.state.get_thread(key) or {}).get("dialog") or {}
            if current.get("token") == rec.get("token"):
                self.state.upsert_thread(key, dialog=new)
        return new

    def _reconcile_dialog(self, key: str, rec: Mapping, transition: bool = True) -> None:
        """Bring the dialog message in line with the live agent. No longer waiting -> closed. A different
        dialog: after a transition the message is closed (the `blocked` transition that follows posts
        the new one); without a transition (restart, idle recheck) nothing else would post it, so the
        message is re-rendered with the new dialog under a new token instead."""
        try:
            state, _, dialog, screen = self._dialog_state(rec)
        except HerdrError as exc:
            log.warning("checking the open dialog of %s failed: %s", key, exc)
            return
        if state == "gone":
            self._close_dialog(key, rec, AGENT_ENDED)
        elif state == "answered":
            self._close_dialog(key, rec, ANSWERED_ON_PC)
        elif state == "changed" and not (rec.get("kind") == KEYPAD and dialog is None):
            if transition:
                self._close_dialog(key, rec, ANSWERED_ON_PC)
            else:
                self._rerender_dialog(key, rec, dialog, screen, note=DIALOG_CHANGED_TEXT)

    def handle_reconcile(self, key: str) -> None:
        """Restart / idle recheck (notifier thread): reconcile an open dialog without a transition,
        then send a deferred first prompt whose dialog is gone."""
        entry = self.state.get_thread(key)
        if not entry:
            return
        with self._answer_lock(self._answer_id(entry.get("terminal_id"), key)):
            rec = (self.state.get_thread(key) or {}).get("dialog")
            if rec:
                self._reconcile_dialog(key, rec, transition=False)
        entry = self.state.get_thread(key) or {}
        if entry.get("deferred_prompt") and not entry.get("dialog"):
            self._submit(self._send_deferred, key, entry.get("terminal_id"))

    def _recheck_idle_dialogs(self) -> None:
        """Dialogs of agents reported idle (Codex folder trust) produce no transition when they are
        answered on PC: recheck them now and then."""
        for key, entry in self.state.all_threads().items():
            if (entry.get("dialog") or {}).get("idle"):
                self._notify_q.put(_Reconcile(key))

    def _dialog_message_state(self, channel: str | None, ts: str) -> str | None:
        """`open` if `ts` is the message of a current dialog record, `closed` if it was one, else None."""
        for entry in self.state.all_threads().values():
            rec = entry.get("dialog") or {}
            if rec.get("message_ts") == ts and (rec.get("channel") or self.dm_channel) == (channel or self.dm_channel):
                return "open"
            if ts in (entry.get("closed_dialogs") or []):
                return "closed"
        return None

    # --- Slack entry points ---------------------------------------------------------------
    def dialog_action(self, action_id: str, value: str, trigger_id: str | None, channel: str | None,
                      message: Mapping | None) -> None:
        """A dialog button (acked by the caller). The free-text modal opens with the trigger id
        before any Herdr I/O; everything else runs on a worker."""
        token, choice = B.parse_dialog_value(value)
        if action_id == B.ACTION_DIALOG_SCREEN:
            self._submit(self.show_dialog_screen, channel, message or {})
        elif action_id.startswith(B.ACTION_DIALOG_TEXT):
            self.open_dialog_text_modal(trigger_id, token, choice)
        elif action_id.startswith(B.ACTION_DIALOG_KEY):
            self._submit(self.answer_dialog, token, ("key", str(choice)), channel, message)
        elif action_id.startswith(B.ACTION_DIALOG_OPTION):
            self._submit(self.answer_dialog, token, ("opt", choice), channel, message)

    def open_dialog_text_modal(self, trigger_id: str | None, token: str | None, index) -> None:
        key = self.state.find_dialog(token)
        rec = ((self.state.get_thread(key) or {}).get("dialog") or {}) if key else {}
        options = rec.get("options") or []
        if not isinstance(index, int) or not 0 <= index < len(options) or not options[index].get("free_text"):
            self.transport.open_view(trigger_id, B.notice_view("Herdr", DIALOG_GONE_TEXT))
            return
        where = {"c": rec.get("channel"), "m": rec.get("message_ts"), "th": rec.get("thread_ts")}
        self.transport.open_view(trigger_id, B.dialog_text_view(token, index, rec.get("name") or "agent",
                                                                options[index].get("label") or "",
                                                                rec.get("question") or "", where,
                                                                options[index].get("typed")))

    def submit_dialog_text(self, view: Mapping) -> dict | None:
        """Free-text modal submission (runs before the ack: local checks only)."""
        token, index, text, where = B.parse_dialog_text_view(view)
        if not text.strip():
            return {B.BLOCK_DIALOG_TEXT: "Enter an answer."}
        message = {"ts": where.get("m"), "thread_ts": where.get("th")} if where.get("m") else None
        self._submit(self.answer_dialog, token, ("text", index, text), where.get("c"), message)
        return None

    def show_dialog_screen(self, channel: str | None, message: Mapping) -> None:
        """[Show screen]: post the last lines of the agent's screen in its thread."""
        thread_ts = message.get("thread_ts") or message.get("ts")
        key = self.state.find_session_by_thread(channel, thread_ts) if channel and thread_ts else None
        entry = self.state.get_thread(key) if key else None
        if not entry:
            self._post_safe("This thread is not bound to an agent.", thread_ts)
            return
        pane = entry.get("pane_id")
        if not entry.get("provisional"):
            try:
                info = self._find_session_agent(key, pane)
                pane = info["pane_id"] if info else pane
            except HerdrError:
                pass
        screen = self._visible(pane)
        if screen is None:
            self._post_safe("❌ Could not read the agent's screen.", thread_ts)
            return
        lines = tail_lines(screen, DIALOG_SCREEN_LINES)
        try:
            self._retrying(lambda: self._post("Screen", B.screen_blocks(lines), thread_ts), idempotent=False)
        except Exception:
            log.exception("posting the screen failed")

    def answer_dialog(self, token: str | None, choice: tuple, channel: str | None = None,
                      message: Mapping | None = None) -> None:
        """Worker: answer the dialog `token` names with an option / key / free text."""
        key = self.state.find_dialog(token)
        rec = ((self.state.get_thread(key) or {}).get("dialog") or {}) if key else {}
        terminal = rec.get("terminal_id")
        closed = False
        if rec:
            with self._answer_lock(self._answer_id(terminal, key)):
                key = self.state.find_dialog(token)  # re-resolved: the entry may have been re-keyed
                rec = ((self.state.get_thread(key) or {}).get("dialog") or {}) if key else {}
                if rec:
                    closed = self._answer_locked(key, rec, choice)
        if not rec:
            self._dialog_gone(channel, message)
            return
        if closed:
            self._send_deferred(key, terminal)

    def answer_thread_reply(self, key: str, target: Target, text: str, thread_ts: str | None) -> None:
        """Worker: a thread reply while the thread's dialog is open is its free-text answer. If the
        agent is no longer waiting, the reply is sent as a prompt (normal D6 rules)."""
        terminal = (self.state.get_thread(key) or {}).get("terminal_id") or target.terminal_id
        handled = closed = False
        with self._answer_lock(self._answer_id(terminal, key)):
            if thread_ts:  # re-resolved: the entry may have been re-keyed meanwhile
                key = self.state.find_session_by_thread(self.dm_channel, thread_ts) or key
            rec = (self.state.get_thread(key) or {}).get("dialog")
            if rec:
                handled, closed = self._thread_answer_locked(key, rec, text)
        if closed:
            self._send_deferred(key, terminal)
        if not handled:
            self.send(target, text, None, False, None, thread_ts)

    def _dialog_gone(self, channel: str | None, message: Mapping | None) -> None:
        """A click whose token is not open. The message of a live dialog (re-rendered under a new
        token) or of a closed one (showing its outcome) is left as it is."""
        thread_ts = None
        if message and message.get("ts"):
            thread_ts = message.get("thread_ts") or message["ts"]
            state = self._dialog_message_state(channel, message["ts"])
            if state == "open":
                self._post_safe(DIALOG_STALE_TEXT, thread_ts)
                return
            if state is None and channel and message.get("blocks") is not None:
                try:
                    self.transport.update_message(channel, message["ts"], message.get("text") or DIALOG_GONE_TEXT,
                                                  B.without_actions(message.get("blocks") or [], DIALOG_GONE_TEXT))
                except Exception:
                    log.warning("removing the buttons of a closed dialog failed", exc_info=True)
        self._post_safe(DIALOG_GONE_TEXT, thread_ts)

    # --- answering (callers hold the agent's answer lock) ----------------------------------
    def _answer_locked(self, key: str, rec: dict, choice: tuple) -> bool:
        """Verify, send, confirm. Returns True when the dialog is closed (agent no longer waiting)."""
        thread_ts = rec.get("thread_ts")
        try:
            state, info, dialog, screen = self._dialog_state(rec)
        except HerdrError as exc:
            self._post_safe(f"❌ Herdr error: {B.escape(str(exc))}. Nothing was sent.", thread_ts)
            return False
        if state == "gone":
            self._close_dialog(key, rec, f"{AGENT_ENDED}; nothing was sent")
            return False
        if state == "answered":
            self._close_dialog(key, rec, ALREADY_ANSWERED)
            return True
        if state == "unreadable":
            self._post_safe("⚠️ Could not read the agent's screen; nothing was sent.", thread_ts)
            return False
        # A keypad's unparsed screen changes by itself (spinners); once it parses as a real dialog
        # the user has not seen that dialog, so nothing is sent then either.
        if state == "changed" and not (rec.get("kind") == KEYPAD and dialog is None):
            if dialog is not None or info.get("agent_status") == "blocked":
                self._rerender_dialog(key, rec, dialog, screen, note=DIALOG_CHANGED_TEXT)
                return False
            self._close_dialog(key, rec, ALREADY_ANSWERED)
            return True
        return self._send_answer(key, rec, info, dialog, screen, choice)

    def _thread_answer_locked(self, key: str, rec: dict, text: str) -> tuple[bool, bool]:
        """(handled, closed) for a thread reply while `rec` is open."""
        thread_ts = rec.get("thread_ts")
        try:
            state, info, dialog, screen = self._dialog_state(rec)
        except HerdrError as exc:
            self._post_safe(f"❌ Herdr error: {B.escape(str(exc))}. Nothing was sent.", thread_ts)
            return True, False
        if state == "gone":
            self._close_dialog(key, rec, AGENT_ENDED)
            return False, False
        if state == "answered":
            self._close_dialog(key, rec, ALREADY_ANSWERED)
            return False, True
        if state == "unreadable":
            self._post_safe("⚠️ Could not read the agent's screen; nothing was sent.", thread_ts)
            return True, False
        if state == "changed" and not (rec.get("kind") == KEYPAD and dialog is None):
            self._rerender_dialog(key, rec, dialog, screen, note=DIALOG_CHANGED_TEXT)
            self._post_safe(DIALOG_CHANGED_TEXT, thread_ts)
            return True, False
        option = dialog.free_text_option() if dialog is not None and rec.get("kind") != KEYPAD else None
        if option is None:
            self._post_safe(DIALOG_BUTTONS_TEXT, thread_ts)
            return True, False
        index = next(i for i, o in enumerate(dialog.options) if o is option)
        already = typed_text(option)
        closed = self._send_answer(key, rec, info, dialog, screen, ("text", index, text))
        if already is not None:
            # The reply went after the text the row already held: say so, with the text it holds now.
            now = self._typed_now(key, index) or f"{already}\n{text}"
            self._post_safe(B.typed_notice(now, sent=True), thread_ts)
        return True, closed

    def _typed_now(self, key: str, index: int) -> str | None:
        """The text the open record says row `index` holds now (after a re-render)."""
        rec = (self.state.get_thread(self._key_now(key, None)) or {}).get("dialog") or {}
        options = rec.get("options") or []
        return options[index].get("typed") if 0 <= index < len(options) else None

    @staticmethod
    def _answer_keys(dialog: Dialog | None, choice: tuple) -> tuple[list[str], str | None, bool, str] | None:
        """(keys, free text or None, Enter after the text, label for the answered message), or None
        if the choice does not apply to the live dialog."""
        if choice[0] == "key":
            key = choice[1]
            if key == "submit":
                keys = submit_keys(dialog) if dialog is not None else None
                return (keys, None, False, KEY_LABELS[key]) if keys else None
            return ([key], None, False, KEY_LABELS.get(key, key)) if key in DIALOG_KEYS else None
        index = choice[1]
        if dialog is None or not isinstance(index, int) or not 0 <= index < len(dialog.options):
            return None
        option = dialog.options[index]
        label = truncate(f"{option.number}. {option.label}" if option.number is not None else option.label, 200)
        if choice[0] == "text":
            # Newlines go as they are: they break the line in the agent's input, they do not submit.
            text = str(choice[2]).replace("\r\n", "\n").replace("\r", "\n").strip("\n")
            if not option.free_text or not text.strip():
                return None
            keys, enter = text_keys(dialog, index)
            return keys, text, enter, f"{label}: {truncate(' '.join(text.split()), 200)}"
        return keys_for(dialog, index), None, False, label

    def _send_answer(self, key: str, rec: dict, info: Mapping, dialog: Dialog | None, screen: str | None,
                     choice: tuple) -> bool:
        thread_ts = rec.get("thread_ts")
        planned = self._answer_keys(dialog, choice)
        if planned is None:
            self._post_safe("⚠️ That option is not available any more; nothing was sent.", thread_ts)
            return False
        keys, text, enter, label = planned
        pane = info["pane_id"]
        try:
            if keys:
                self.client.send_keys(pane, keys)
            if text is not None:
                if keys:
                    self.sleep(DIALOG_KEY_GAP)  # the digit / cursor move turns the option into an input
                self.client.send_text(pane, text)
                if enter:
                    self.sleep(DIALOG_KEY_GAP)
                    self.client.send_keys(pane, ["enter"])
        except HerdrError as exc:
            self._post_safe(f"❌ Could not send the answer to *{B.escape(rec.get('name') or 'agent')}*: "
                            f"{B.escape(str(exc))}. Check the screen.", thread_ts)
            return False
        return self._confirm_answer(key, rec, label, dialog, screen)

    def _confirm_answer(self, key: str, rec: dict, label: str, dialog: Dialog | None, screen: str | None) -> bool:
        """Poll until the dialog changed (edit the message: next question, toggled box, ...) or the
        agent stopped waiting (answered). The screen lags the keys, so a changed dialog must read
        the same twice before it is shown. Returns True when the dialog is closed."""
        deadline = self.clock() + DIALOG_POLL_TIMEOUT
        candidate = None
        while True:
            self.sleep(DIALOG_POLL_INTERVAL)
            try:
                state, _, live, live_screen = self._dialog_state(rec)
            except HerdrError as exc:
                log.warning("confirming a dialog answer: %s", exc)
                state = "unreadable"
            if state == "gone":
                self._close_dialog(key, rec, f"⏹ *{B.escape(label)}* — sent, then the agent ended")
                return False
            if state == "answered":
                self._close_dialog(key, rec, f"✅ *{B.escape(label)}* — answered from Slack")
                return True
            if state == "changed":
                fp = live.fingerprint if live is not None else screen_fingerprint(live_screen or "")
                if fp == candidate:
                    self._rerender_dialog(key, rec, live, live_screen)
                    return False
                candidate = fp
            else:
                candidate = None
                if state == "open":
                    dialog, screen = live, live_screen
            if self.clock() >= deadline:
                # New token, same buttons: a click queued behind this answer is out of date now and
                # cannot press the key a second time.
                new = self._rerender_dialog(key, rec, dialog, screen, note=UNCONFIRMED_TEXT)
                try:
                    self._retrying(lambda: self._post(UNCONFIRMED_TEXT, B.dialog_unconfirmed_blocks(new["token"]),
                                                      rec.get("thread_ts")), idempotent=False)
                except Exception:
                    log.exception("posting the unconfirmed-answer notice failed")
                return False

    # --- a new agent's first prompt, held while its startup dialog was open -------------------
    def _send_deferred(self, key: str, terminal_id: str | None = None) -> None:
        key = self._key_now(key, terminal_id)
        with self._lock(key):
            entry = self.state.get_thread(key) or {}
            deferred = entry.get("deferred_prompt")
            if not deferred or entry.get("dialog"):
                return
            self.state.upsert_thread(key, deferred_prompt=None)  # whoever takes it sends it (once)
        log.info("sending the deferred first prompt of %s (dropped if the bridge stops now)", key)
        name = entry.get("agent_name") or "agent"
        thread_ts = entry.get("thread_ts")
        target = Target(entry.get("pane_id") or "", None if entry.get("provisional") else key,
                        entry.get("terminal_id"), name, key)
        try:
            info, _ = self._resolve(target)
        except HerdrError as exc:
            info = None
            log.warning("sending the deferred prompt of %s: %s", key, exc)
        if info is not None:
            expected = {"session": session_of(info), "terminal_id": info.get("terminal_id")}
            info = self._wait_settled(info["pane_id"], info, expected)
        status = (info or {}).get("agent_status")
        if status == "blocked":
            # Another dialog came up: keep the prompt until that one is answered too.
            with self._lock(key):
                self.state.upsert_thread(key, deferred_prompt=deferred)
            return
        if info is None or status not in SETTLED:
            self._post_safe(f"⚠️ *{B.escape(name)}* is not ready ({status or 'gone'}); its first prompt was not "
                            "sent. Use send when it is idle.", thread_ts)
            return
        if (info.get("agent") or entry.get("kind")) == KIND_CODEX and self.cfg.codex_prompt_delay > 0:
            self.sleep(self.cfg.codex_prompt_delay)
        self.send(Target(info["pane_id"], expected.get("session"), expected.get("terminal_id"),
                         info.get("name") or name, key), deferred.get("text") or "", None, fresh=True)

    # =====================================================================================
    # Notifications (single notifier thread)
    # =====================================================================================
    def enqueue_transition(self, transition: AgentTransition) -> None:
        self._notify_q.put(transition)
        if self._home_user:  # only once the owner has opened the Home tab
            self.home.request()

    # =====================================================================================
    # App Home tab
    # =====================================================================================
    def handle_home_opened(self, user: str) -> None:
        """app_home_opened (the owner guard already ran): remember the viewer and publish."""
        self._home_user = user
        self.home.run_now()

    def home_action(self, action_id: str, trigger_id: str | None, value: str | None = None) -> None:
        """Home tab buttons (acked by the caller). Modals open with the trigger id first."""
        if action_id == B.ACTION_HOME_NEW:
            self.open_new_modal(trigger_id, None)
        elif action_id == B.ACTION_HOME_SEND:
            self.open_send_modal(trigger_id, None)
        elif action_id == B.ACTION_HOME_SEND_AGENT:
            self.open_send_modal(trigger_id, None, initial_target=value or None)
        elif action_id == B.ACTION_HOME_REFRESH:
            self.home.run_now()

    def build_home_view(self) -> dict:
        uptime = format_duration(self.clock() - self.started_at)
        parts = [f"⏱ uptime {uptime}", f"`{self.cfg.slash_command}`", time.strftime("updated %H:%M:%S")]
        try:
            agents = self.client.list_agents()
            labels = self._ws_labels()
            parts.insert(1, f"{len(agents)} agents")
            return B.home_view(self.cfg.bot_display_name, parts, agents, labels)
        except HerdrError as exc:
            return B.home_view(self.cfg.bot_display_name, parts, None, {}, error=str(exc))

    def publish_home(self) -> None:
        """One Home tab publish (called only by `self.home`, which serializes, logs and backs off;
        Slack errors propagate to it). Does nothing once the bridge is stopping."""
        user = self._home_user
        if not user or self._stopping.is_set():
            return
        view = self.build_home_view()  # Herdr snapshot
        if self._stopping.is_set():
            return
        self.transport.publish_view(user, view)

    def _notify_loop(self) -> None:
        last_recheck = time.monotonic()
        while True:
            if time.monotonic() - last_recheck >= IDLE_DIALOG_RECHECK and not self._stopping.is_set():
                last_recheck = time.monotonic()  # also under steady traffic from other agents
                self._recheck_idle_dialogs()
            try:
                item = self._notify_q.get(timeout=IDLE_DIALOG_RECHECK)
            except queue.Empty:
                continue
            try:
                if item is None:
                    return
                self.process_with_retry(item)
            finally:
                self._notify_q.task_done()

    def process_with_retry(self, item) -> bool:
        """Process a transition / resume item; retry transient Slack failures with backoff."""
        delay = 1.0
        for attempt in range(1, NOTIFY_MAX_ATTEMPTS + 1):
            if self._stopping.is_set():
                return False
            try:
                if isinstance(item, _Resume):
                    self.handle_resume(item.key, item.task_id)
                elif isinstance(item, _Reconcile):
                    self.handle_reconcile(item.key)
                else:
                    self.handle_transition(item)
                return True
            except SlackTransientError as exc:
                self.stats["slack_retries"] += 1
                wait = max(exc.retry_after or 0.0, delay)
                log.warning("notification failed (%s, attempt %d); retrying in %.0fs", exc.error, attempt, wait)
                self.sleep(wait)
                delay = min(delay * 2, 60.0)
            except SlackPermanentError as exc:
                log.error("notification dropped: Slack rejected it (%s)", exc.error)
                return False
            except Exception:
                log.exception("notification for %r failed", item)
                return False
        log.error("notification dropped after %d attempts", NOTIFY_MAX_ATTEMPTS)
        return False

    def handle_transition(self, t: AgentTransition) -> None:
        """Idempotent: Slack failures propagate (for retry); state changes only after a post succeeds.
        Runs under the thread lock, so admission can't create a second root meanwhile (R2)."""
        # The agent's answer lock first (a click in flight finishes before its transition is
        # handled), then the admission lock: waiting for a click never blocks admission.
        with self._answer_lock(self._transition_answer_id(t)):
            with self._admission_lock:
                deferred = self._handle_transition(t)
        if deferred:
            self._submit(self._send_deferred, deferred, (t.info or {}).get("terminal_id"))

    def _handle_transition(self, t: AgentTransition) -> str | None:
        """Returns the thread key whose deferred first prompt can be sent now, if any."""
        key = self._transition_key(t)
        if not key:
            return None
        rec = (self.state.get_thread(key) or {}).get("dialog")
        if rec:
            if t.ended:
                self._close_dialog(key, rec, AGENT_ENDED)
            else:
                self._reconcile_dialog(key, rec)
        self._notify_transition(key, t)
        entry = self.state.get_thread(key) or {}
        if not t.ended and t.status in SETTLED and entry.get("deferred_prompt") and not entry.get("dialog"):
            return key
        return None

    def _notify_transition(self, key: str, t: AgentTransition) -> None:
        with self._lock(key):
            entry = self.state.get_thread(key)
            if t.ended:
                if entry and entry.get("pending_task"):
                    name = B.escape(entry.get("agent_name") or t.name or t.pane_id)
                    text = f"⚠️ *{name}* ended before completing the Slack task."
                    task_id = entry["pending_task"].get("task_id")
                    op = f"ended:{task_id}"
                    self._post(_strip_mrkdwn(text), B.notice_blocks(text), entry.get("thread_ts"), op=op, key=key)
                    self._commit(key, op, **self._task_cleared(key, task_id))
                return
            if entry and t.pane_id and entry.get("pane_id") != t.pane_id:
                # The agent moved (the session is kept across moves): follow it.
                entry = self.state.upsert_thread(key, pane_id=t.pane_id, workspace_id=t.workspace_id)
            seq = _seq(t.info)
            pending = (entry or {}).get("pending_task")
            if pending and seq and seq <= int(pending.get("seq0") or 0):
                pending = None  # this transition predates the task: it is not the task's outcome
            muted = bool((entry or {}).get("muted"))
            decision = decide_notification(t.prev_status, t.status, pending_task=pending, muted=muted)
            if decision.action is Action.NONE:
                if decision.clear_pending and pending:
                    self._clear_task(key, pending.get("task_id"))
                return
            if decision.action is Action.COMPLETED and seq and seq <= int((entry or {}).get("last_result_seq") or 0):
                return  # already posted (e.g. settled during admission or by resume)
            if decision.action is Action.BLOCKED and seq and seq <= int((entry or {}).get("last_blocked_seq") or 0):
                return
            info = dict(t.info or {})
            info.setdefault("pane_id", t.pane_id)
            info.setdefault("workspace_id", t.workspace_id)
            ws_label = self._ws_labels().get(info.get("workspace_id"), info.get("workspace_id") or "")
            entry = self._ensure_thread(key, info, self._agent_title(info, ws_label),
                                        self._agent_context(info), ORIGIN_PC)
            thread_ts = entry.get("thread_ts")
            if decision.action is Action.STARTED:
                op = f"started:{pending.get('task_id')}"
                self._post("⏳ started (working)", B.started_blocks(), thread_ts, op=op, key=key)
                current = (self.state.get_thread(key) or {}).get("pending_task") or {}
                fields = {}
                if current.get("task_id") == pending.get("task_id"):
                    fields["pending_task"] = dict(current, working_announced=True)
                self._commit(key, op, **fields)
            elif decision.action is Action.BLOCKED:
                fields = {"last_blocked_seq": seq} if seq else {}
                if entry.get("dialog"):  # the open dialog message shows it already (reconciled above)
                    if fields:
                        self._commit(key, None, **fields)
                else:
                    op = f"blocked:{info.get('terminal_id') or key}:{seq or t.at}"
                    self._open_dialog(key, entry, info, ws_label, op, **fields)
            elif decision.action is Action.COMPLETED:
                op = _result_op(key, pending, info, t.at)
                self.post_result(key, entry, info, ws_label, pending, op=op)
                fields = {"last_result_seq": seq} if seq else {}
                if decision.clear_pending and pending:
                    fields.update(self._task_cleared(key, pending.get("task_id")))
                self._commit(key, op, **fields)
            self.stats["notifications"] += 1

    def _task_cleared(self, key: str, task_id: str | None) -> dict:
        """Fields that clear the pending task, only if it is still `task_id` (for `_commit`)."""
        current = ((self.state.get_thread(key) or {}).get("pending_task") or {}).get("task_id")
        return {"pending_task": None} if task_id and current == task_id else {}

    def post_result(self, key: str, entry: Mapping, info: Mapping, ws_label: str,
                    pending: Mapping | None, op: str | None = None) -> None:
        kind = info.get("agent") or entry.get("kind")
        cwd = info.get("cwd") or entry.get("cwd")
        pane = info.get("pane_id") or entry.get("pane_id")
        session = session_of(info) or key
        since = float(pending["started_at"]) if pending and pending.get("started_at") else None
        res = agent_result(kind, session, cwd, lambda: self._read_screen(pane), since=since,
                           fallback_lines=self.cfg.fallback_lines, base=self.claude_projects)
        duration = format_duration(self.clock() - since) if since else (res.duration or "")
        name = _display(info, entry)
        header = f"✅ *{B.escape(name)}* · {B.escape(ws_label)}" + (f" · {duration}" if duration else "")
        title = (info.get("terminal_title_stripped") or info.get("terminal_title") or "").strip()
        ctx = [f"📁 `{B.escape(cwd)}`" if cwd else "", B.escape(truncate(title, 80)) if title else "",
               "_raw output_" if not res.parsed else ""]
        markdown = res.source != "tail"
        blocks, truncated = B.result_blocks(header, ctx, res.text, None, res.recap,
                                            self.cfg.result_max_chars, markdown)
        if truncated:
            rid = self.results.save(res.text, {"name": name, "session": session, "at": self.clock()})
            blocks, _ = B.result_blocks(header, ctx, res.text, rid, res.recap, self.cfg.result_max_chars, markdown)
        fallback = f"✅ {name} finished" + (f" · {duration}" if duration else "")
        self._post(fallback, blocks, entry.get("thread_ts"), op=op, key=key if op else None)

    # --- restart ------------------------------------------------------------------------
    def handle_resume(self, key: str, task_id: str | None = None) -> None:
        """Settle a Slack task left pending across a restart (runs on the notifier thread).

        The whole lookup + decision runs under the thread lock (no admission in between, R5)
        and only for the original task: if the entry was re-keyed meanwhile (provisional ->
        session), the task is found by its id (N3); a different task is left alone."""
        with self._admission_lock:
            if task_id:
                current = ((self.state.get_thread(key) or {}).get("pending_task") or {}).get("task_id")
                if current != task_id:
                    moved = self.state.find_task(task_id)
                    if moved is None:
                        return  # settled meanwhile
                    key = moved
            with self._lock(key):
                entry = self.state.get_thread(key)
                pending = (entry or {}).get("pending_task")
                if not pending:
                    return  # already settled by a live transition
                task_id = pending.get("task_id")
                try:
                    if entry.get("provisional"):
                        info = self.client.find_agent(entry["pane_id"]) if entry.get("pane_id") else None
                        if info is not None and entry.get("terminal_id") and \
                                info.get("terminal_id") != entry["terminal_id"]:
                            info = None
                        if info is not None and session_of(info):
                            self.state.rekey_thread(key, session_of(info))
                            key = session_of(info)
                    else:
                        info = self._find_session_agent(key, entry.get("pane_id"))
                except HerdrError as exc:
                    log.warning("resume %s: %s", key, exc)
                    return
            with self._lock(key):
                entry = self.state.get_thread(key) or {}
                pending = entry.get("pending_task")
                if not pending or pending.get("task_id") != task_id:
                    return
                self._settle_resumed(key, entry, pending, info)

    def _settle_resumed(self, key: str, entry: dict, pending: dict, info: dict | None) -> None:
        status = info.get("agent_status") if info else None
        seq = _seq(info)
        task_id = pending.get("task_id")
        if info is not None and status in SETTLED and seq and seq <= int(pending.get("seq0") or 0):
            # Settled, but not after the task's admission: the prompt never took effect.
            name = B.escape(entry.get("agent_name") or key[:8])
            text = f"⚠️ *{name}* never started the Slack task (the bridge restarted); send it again if needed."
            op = f"unstarted:{task_id}"
            self._post(_strip_mrkdwn(text), B.notice_blocks(text), entry.get("thread_ts"), op=op, key=key)
            self._commit(key, op, **self._task_cleared(key, task_id))
            return
        decision = decide_resume(status, pending)
        cleared = self._task_cleared(key, task_id) if decision.clear_pending else {}
        if decision.action is Action.COMPLETED:
            if not seq or seq > int(entry.get("last_result_seq") or 0):
                ws_label = self._ws_labels().get(info.get("workspace_id"), info.get("workspace_id") or "")
                op = _result_op(key, pending, info)
                self.post_result(key, entry, info, ws_label, pending, op=op)
                self._commit(key, op, **({"last_result_seq": seq} if seq else {}), **cleared)
                return
        elif decision.action is Action.BLOCKED:
            if not seq or seq > int(entry.get("last_blocked_seq") or 0):
                fields = dict({"last_blocked_seq": seq} if seq else {}, **cleared)
                if entry.get("dialog"):
                    self._commit(key, None, **fields)
                    return
                ws_label = self._ws_labels().get(info.get("workspace_id"), info.get("workspace_id") or "")
                info = dict(info)
                info.setdefault("pane_id", entry.get("pane_id"))
                op = f"blocked:{info.get('terminal_id') or key}:{seq or task_id}"
                self._open_dialog(key, entry, info, ws_label, op, **fields)
                return
        elif decision.clear_pending:
            name = B.escape(entry.get("agent_name") or key[:8])
            text = f"⚠️ *{name}* ended before completing the Slack task."
            op = f"ended:{task_id}"
            self._post(_strip_mrkdwn(text), B.notice_blocks(text), entry.get("thread_ts"), op=op, key=key)
            self._commit(key, op, **cleared)
            return
        if cleared:
            self._commit(key, None, **cleared)


def _result_op(key: str, pending: Mapping | None, info: Mapping | None, at: float | None = None) -> str:
    """Stable id of one result post: per Slack task, else per agent state (seq)."""
    if pending and pending.get("task_id"):
        return f"result:{pending['task_id']}"
    seq = _seq(info)
    return f"result:{(info or {}).get('terminal_id') or key}:{seq or at}"


def _display(info: Mapping | None, entry: Mapping | None) -> str:
    info, entry = info or {}, entry or {}
    return info.get("name") or entry.get("agent_name") or info.get("pane_id") or entry.get("pane_id") or "agent"


def _typed_row_hint(rec: Mapping) -> int | None:
    """Index of the multi-select free-text row the record showed (evidence for the parser)."""
    return next((i for i, o in enumerate(rec.get("options") or [])
                 if o.get("free_text") and o.get("checked") is not None), None)


def _text_hash(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def _strip_mrkdwn(text: str) -> str:
    return re.sub(r"[*_`]", "", text).replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")
