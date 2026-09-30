"""Herdr event subscriptions: per-pane status streams + a lifecycle stream.

Herdr 0.8.2 facts this module is built around:
- `pane.agent_status_changed` subscriptions require a `pane_id`, so every agent
  pane gets its own `events.subscribe` connection (a pipe handle cannot write a
  new request while a read is pending, so connections are never shared).
- Status streams do not replay history. The lifecycle stream (`pane.created`,
  `pane.agent_detected`, `pane.closed`, ...) does: on subscribe the server
  re-sends retained history (paced ~0.1s apart).
- Event names are inconsistent: `pane.agent_status_changed` (dotted) vs
  `pane_agent_detected` / `pane_created` (underscored). `normalize_event_name`
  maps everything to the dotted form.
- Status events carry neither the agent session nor a sequence number, while
  `agent.get` / `agent.list` return `agent_session` and a monotonic
  `state_change_seq`.

Design: every event is only a *hint*. The dispatcher thread owns all state and,
for each hint, asks Herdr for the pane's live agent and applies it through
`_reconcile`, which moves a pane forward only when `state_change_seq` grew and
keeps per-session identity. Consequences:
- buffered or replayed events can never move a pane backward or duplicate a
  transition that a newer snapshot already covered;
- a queued event for session A is never attributed to a session B that replaced
  it in the same pane (A gets an `ended` transition instead);
- destructive lifecycle hints (closed/exited/moved) are verified before anything
  is torn down;
- a failed lookup leaves the pane untouched and marked dirty; a retry timer and
  the periodic resync (snapshot `state_change_seq`) recover the transition.
Fast sequences can be collapsed (e.g. working->blocked->done seen as
working->done when processed late); that is preferred over stale alerts. The
status carried by a queued event is kept as evidence of activity: when it was
active but the pane has already settled, the transition is reported as
prev -> working -> settled so a complete fast task is not lost.

A session that shows up in a new pane (move) adopts its tracked state from the
old pane instead of being ended there and seeded as new. Before a tracked
session is ever reported as ended (pane empty / reused), the live `agent.list`
is searched for it (by session id, or terminal id before Herdr reports a
session); if it lives elsewhere it is relocated, and if the list call fails the
decision is parked until the next resync. The periodic resync is the safety net
for anything event ordering misses; it skips panes whose status events are still
queued, so a snapshot never overtakes the evidence those events carry. Activity
seen in a status event is kept on the pane (`activity`) until a snapshot accounts
for it, so a failed lookup cannot erase a fast task.

Every state change and callback checks the run generation of the thread doing
it, so a dispatcher that outlived `stop()` cannot touch a restarted manager.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Iterable

from .herdr_client import HerdrError

log = logging.getLogger(__name__)

_EVENT_PREFIXES = ("workspace", "worktree", "tab", "pane", "layout")

LIFECYCLE_SUBSCRIPTIONS = [
    {"type": "pane.agent_detected"},
    {"type": "pane.closed"},
    {"type": "pane.exited"},
    {"type": "pane.moved"},
]

STATUS_CHANGED = "pane.agent_status_changed"
AGENT_DETECTED = "pane.agent_detected"
PANE_CLOSED = "pane.closed"
PANE_EXITED = "pane.exited"
PANE_MOVED = "pane.moved"

STATUS_ENDED = "unknown"  # status reported on a transition whose session left the pane
ACTIVE_STATUSES = ("working", "blocked")
SETTLED_STATUSES = ("idle", "done")


def normalize_event_name(name: str) -> str:
    """`pane_agent_detected` -> `pane.agent_detected`; dotted names pass through."""
    if not name or "." in name:
        return name
    for prefix in _EVENT_PREFIXES:
        if name.startswith(prefix + "_"):
            return f"{prefix}.{name[len(prefix) + 1:]}"
    return name


@dataclass(frozen=True)
class HerdrEvent:
    kind: str
    data: dict
    received_at: float = 0.0

    @property
    def pane_id(self) -> str | None:
        pane_id = self.data.get("pane_id")
        if pane_id:
            return pane_id
        pane = self.data.get("pane")
        return pane.get("pane_id") if isinstance(pane, dict) else None


def parse_event(envelope: dict, received_at: float | None = None) -> HerdrEvent | None:
    """Decode an event envelope `{"event": ..., "data": {...}}` (None for non-events)."""
    if not isinstance(envelope, dict) or "event" not in envelope:
        return None
    data = envelope.get("data") or {}
    kind = normalize_event_name(str(envelope.get("event") or data.get("type") or ""))
    return HerdrEvent(kind, data, time.time() if received_at is None else received_at)


def session_of(info: dict | None) -> str | None:
    return ((info or {}).get("agent_session") or {}).get("value")


@dataclass(frozen=True)
class AgentTransition:
    """A status change of one agent session in one pane."""

    pane_id: str
    workspace_id: str | None
    prev_status: str | None
    status: str
    at: float
    session: str | None = None  # the agent session this transition belongs to
    name: str | None = None
    info: dict | None = None  # live `agent.get`/`agent.list` data (None when the session ended)
    synthetic: bool = False  # inferred from a snapshot instead of a live event
    ended: bool = False  # the session left the pane (exit, release, replacement)

    @property
    def agent_session(self) -> str | None:
        return self.session


def plan_reconcile(subscribed: Iterable[str], live_agent_panes: Iterable[str]) -> tuple[set[str], set[str]]:
    """Return (panes to subscribe, panes to drop) to match the live agent snapshot."""
    subscribed = set(subscribed)
    live = set(live_agent_panes)
    return live - subscribed, subscribed - live


@dataclass
class _PaneState:
    status: str
    seq: int
    session: str | None
    name: str | None
    workspace_id: str | None
    terminal_id: str | None = None
    activity: bool = False  # an active status was seen in an event and not yet accounted for


def _new_state(info: dict) -> _PaneState:
    return _PaneState(info.get("agent_status") or "unknown", int(info.get("state_change_seq") or 0),
                      session_of(info), info.get("name"), info.get("workspace_id"), info.get("terminal_id"))


def _same_agent(st: _PaneState, info: dict) -> bool:
    """Is live `info` the agent tracked by `st`? Session id, else terminal id (pre-session)."""
    session = session_of(info)
    if st.session:
        return session == st.session
    return bool(st.terminal_id) and info.get("terminal_id") == st.terminal_id


@dataclass
class _Sub:
    key: str
    stream: object
    thread: threading.Thread | None = None


@dataclass(frozen=True)
class _Recheck:
    pane_id: str


_RESYNC = object()


@dataclass
class _Stats:
    events: int = 0
    transitions: int = 0
    resyncs: int = 0
    stream_errors: int = 0
    lookup_errors: int = 0
    started_at: float = field(default_factory=time.time)


class SubscriptionManager:
    """Tracks agent status per pane and reports transitions to `on_transition`.

    All state changes and callbacks happen on one dispatcher thread (or on the
    caller's thread for `resync()`/`drain()` before `start()`, as tests do).
    """

    def __init__(self, client, on_transition: Callable[[AgentTransition], None],
                 resync_interval: float = 60.0, retry_delay: float | None = 2.0,
                 clock: Callable[[], float] = time.time, join_timeout: float = 3.0):
        self.client = client
        self.on_transition = on_transition
        self.resync_interval = resync_interval
        self.retry_delay = retry_delay
        self.clock = clock
        self.join_timeout = join_timeout
        self.stats = _Stats()
        self._panes: dict[str, _PaneState] = {}
        self._subs: dict[str, _Sub] = {}
        self._lifecycle: _Sub | None = None
        self._dirty: set[str] = set()
        self._lock = threading.RLock()
        self._gen = 0
        self._active = True  # stream installation allowed (False after stop until next start)
        self._queue: queue.Queue = queue.Queue()
        self._stop = threading.Event()
        self._workers: list[threading.Thread] = []
        self._timers: list[threading.Timer] = []
        self._tls = threading.local()  # .gen: run generation of a worker thread
        self._inflight: dict[str, int] = {}  # pane -> status events queued but not yet dispatched
        self._orphans: list[tuple[str, _PaneState]] = []  # displaced states awaiting a snapshot

    # --- lifecycle -----------------------------------------------------------
    def start(self) -> None:
        with self._lock:
            self._gen += 1
            self._active = True
            self._stop = threading.Event()
            self._queue = queue.Queue()
            self._inflight = {}
            stop, q, gen = self._stop, self._queue, self._gen
        self._open_lifecycle()
        self.resync()
        workers = [
            threading.Thread(target=self._dispatch_loop, args=(q, gen), name="herdr-events-dispatch", daemon=True),
            threading.Thread(target=self._resync_loop, args=(stop, q), name="herdr-events-resync", daemon=True),
        ]
        with self._lock:
            self._workers = workers
        for t in workers:
            t.start()

    def stop(self) -> None:
        with self._lock:
            self._active = False
            self._gen += 1
            self._stop.set()
            self._queue.put(None)
            subs = list(self._subs.values())
            self._subs.clear()
            if self._lifecycle is not None:
                subs.append(self._lifecycle)
                self._lifecycle = None
            workers, self._workers = self._workers, []
            timers, self._timers = self._timers, []
        for timer in timers:
            timer.cancel()
        for sub in subs:
            _close_quietly(sub.stream)
        me = threading.current_thread()
        for t in workers + [s.thread for s in subs if s.thread is not None]:
            if t is not me and t.is_alive():
                t.join(self.join_timeout)

    # --- queries ---------------------------------------------------------------
    def subscribed_panes(self) -> set[str]:
        with self._lock:
            return set(self._subs)

    def known_status(self, pane_id: str) -> str | None:
        with self._lock:
            st = self._panes.get(pane_id)
            return st.status if st else None

    def known_session(self, pane_id: str) -> str | None:
        with self._lock:
            st = self._panes.get(pane_id)
            return st.session if st else None

    def _stale(self) -> bool:
        """True on a worker thread whose run generation has been stopped/replaced."""
        gen = getattr(self._tls, "gen", None)
        return gen is not None and (gen != self._gen or not self._active)

    def _tracked(self, pane_id: str) -> bool:
        with self._lock:
            return pane_id in self._subs or pane_id in self._panes

    def dirty_panes(self) -> set[str]:
        with self._lock:
            return set(self._dirty)

    # --- dispatcher-side operations -------------------------------------------
    def resync(self) -> None:
        """Reconcile everything with an `agent.list` snapshot (also reopens dead streams)."""
        self.stats.resyncs += 1
        if self._lifecycle is None or getattr(self._lifecycle.stream, "closed", False):
            self._open_lifecycle()
        try:
            agents = self.client.list_agents()
        except HerdrError as exc:
            self.stats.lookup_errors += 1
            log.warning("resync: agent.list failed: %s", exc)
            return
        live = {a["pane_id"]: a for a in agents}
        with self._lock:
            if self._stale():
                return
            orphans, self._orphans = self._orphans, []
            tracked = set(self._panes) | set(self._subs)
        for old_pane, st in orphans:
            self._settle_displaced(old_pane, st, True, agents)
        for pane_id, info in live.items():
            with self._lock:
                if self._stale():
                    return
                if self._inflight.get(pane_id):
                    continue  # its queued status events will reconcile it (and carry activity)
                self._dirty.discard(pane_id)
                sub = self._subs.get(pane_id)
                healthy = sub is not None and not getattr(sub.stream, "closed", False)
            if healthy:
                self._reconcile(pane_id, info, synthetic=True, agents=agents)
            else:
                self._subscribe(pane_id, info)
        for pane_id in tracked - set(live):
            self._apply_absent(pane_id, synthetic=True, agents=agents)

    def recheck(self, pane_id: str, synthetic: bool = True, hint: str | None = None) -> None:
        found, info = self._lookup(pane_id)
        if found is None:
            return  # stays dirty; retried by timer / resync
        with self._lock:
            if self._stale():
                return
            self._dirty.discard(pane_id)
        if not found:
            self._apply_absent(pane_id, synthetic=synthetic)
        elif pane_id in self.subscribed_panes():
            self._reconcile(pane_id, info, synthetic=synthetic, hint=hint)
        else:
            self._subscribe(pane_id, info)

    def handle_event(self, event: HerdrEvent) -> None:
        self.stats.events += 1
        pane_id = event.pane_id
        if not pane_id:
            return
        if event.kind == STATUS_CHANGED:
            hint = event.data.get("agent_status")
            with self._lock:
                if self._stale():
                    return
                subscribed = pane_id in self._subs
                st = self._panes.get(pane_id)
                if st is not None and hint in ACTIVE_STATUSES:
                    st.activity = True  # survives a failed lookup (retried via the dirty set)
            if subscribed:
                self.recheck(pane_id, synthetic=False, hint=hint)
        elif event.kind == AGENT_DETECTED:
            # New agent, replacement, or release: verify against live state (may be replay).
            self.recheck(pane_id, synthetic=False)
        elif event.kind in (PANE_CLOSED, PANE_EXITED):
            # Destructive hints may be replayed history: `recheck` verifies before tearing down.
            if self._tracked(pane_id):
                self.recheck(pane_id, synthetic=False)
        elif event.kind == PANE_MOVED:
            # Destination first: it adopts the session's state from the old pane, so the
            # old pane's recheck finds nothing to end.
            prev = event.data.get("previous_pane_id")
            self.recheck(pane_id, synthetic=False)
            if prev and self._tracked(prev):
                self.recheck(prev, synthetic=False)

    def drain(self) -> None:
        """Process everything queued so far on the calling thread (tests)."""
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                return
            if item is not None:
                self._dispatch(item)

    # --- core state machine -------------------------------------------------------
    def _reconcile(self, pane_id: str, info: dict, synthetic: bool, at: float | None = None,
                   hint: str | None = None, agents: list | None = None) -> None:
        """Apply live `info` to the pane. `hint` = status carried by the event being handled."""
        status = info.get("agent_status") or "unknown"
        seq = int(info.get("state_change_seq") or 0)
        session = session_of(info)
        emit: list[AgentTransition] = []
        relocated_from = None
        displaced = None
        with self._lock:
            if self._stale():
                return
            st = self._panes.get(pane_id)
            if st is None:
                other = next((p for p, o in self._panes.items() if p != pane_id and _same_agent(o, info)), None)
                if other is None:
                    # First sight: seed without a transition.
                    self._panes[pane_id] = _new_state(info)
                    return
                # The agent moved here from `other`: carry its state over.
                st = self._panes.pop(other)
                self._panes[pane_id] = st
                self._dirty.discard(other)
                relocated_from = other
            if session and st.session and session != st.session:
                # A different session occupies the pane now. The old one may have moved away:
                # `_settle_displaced` decides between relocation and `ended`.
                displaced = st
                self._panes[pane_id] = _new_state(info)
            elif seq > st.seq or (seq == 0 and st.seq == 0 and status != st.status):
                prev = st.status
                st.status, st.seq = status, seq
                st.session = st.session or session
                st.terminal_id = st.terminal_id or info.get("terminal_id")
                st.name = info.get("name") or st.name
                st.workspace_id = info.get("workspace_id") or st.workspace_id
                when = self.clock() if at is None else at
                active_seen = hint in ACTIVE_STATUSES or st.activity
                st.activity = False  # this snapshot accounts for everything seen so far
                if active_seen and status in SETTLED_STATUSES and prev not in ACTIVE_STATUSES:
                    # The event saw activity the snapshot no longer shows: a whole task ran
                    # before dispatch. Report it instead of collapsing it to idle/done.
                    emit.append(AgentTransition(pane_id, st.workspace_id, prev, "working", when, st.session,
                                                st.name, info, synthetic))
                    prev = "working"
                if prev != status:
                    emit.append(AgentTransition(pane_id, st.workspace_id, prev, status, when, st.session,
                                                st.name, info, synthetic))
            # else: stale or duplicate information -> ignore
        if relocated_from is not None:
            self._unsubscribe(relocated_from)
        for t in emit:
            self._emit(t)
        if displaced is not None:
            self._settle_displaced(pane_id, displaced, synthetic, agents)

    def _apply_absent(self, pane_id: str, synthetic: bool, agents: list | None = None) -> None:
        """The pane hosts no agent (or is gone): drop it; its agent is relocated or ended."""
        with self._lock:
            if self._stale():
                return
            st = self._panes.pop(pane_id, None)
            self._dirty.discard(pane_id)
        self._unsubscribe(pane_id)
        if st is not None:
            self._settle_displaced(pane_id, st, synthetic, agents)

    def _settle_displaced(self, old_pane: str, st: _PaneState, synthetic: bool,
                          agents: list | None = None) -> None:
        """An agent left `old_pane`: relocate it if it lives elsewhere, else report it ended.

        Uses the live agent list as the authority. If the list cannot be read, the state is
        parked and settled by the next resync (never ended on a guess).
        """
        if not st.session and not st.terminal_id:
            return
        if agents is None:
            try:
                agents = self.client.list_agents()
            except HerdrError as exc:
                self.stats.lookup_errors += 1
                log.warning("agent.list failed while settling %s: %s", old_pane, exc)
                with self._lock:
                    if not self._stale():
                        self._orphans.append((old_pane, st))
                return
        live = next((a for a in agents if a.get("pane_id") != old_pane and _same_agent(st, a)), None)
        if live is not None:
            self._adopt(live, st)
        elif st.session:
            self._emit(self._ended(old_pane, st, synthetic))

    def _adopt(self, live: dict, st: _PaneState) -> None:
        """Track `st` (an agent that moved) at its live pane, then reconcile it there."""
        pane_id = live["pane_id"]
        emit = None
        with self._lock:
            if self._stale():
                return
            cur = self._panes.get(pane_id)
            if cur is None:
                self._panes[pane_id] = st
            elif _same_agent(cur, live) and st.status in ACTIVE_STATUSES and cur.status in SETTLED_STATUSES:
                # The destination was seeded after the task finished: report the completion we know of.
                emit = AgentTransition(pane_id, cur.workspace_id, st.status, cur.status, self.clock(),
                                       cur.session or st.session, cur.name or st.name, live, True)
            elif _same_agent(cur, live):
                cur.activity = cur.activity or st.activity or st.status in ACTIVE_STATUSES
            subscribed = pane_id in self._subs
        if emit is not None:
            self._emit(emit)
        if subscribed:
            self._reconcile(pane_id, live, synthetic=True)
        else:
            self._subscribe(pane_id, live)

    def _ended(self, pane_id: str, st: _PaneState, synthetic: bool) -> AgentTransition:
        return AgentTransition(pane_id, st.workspace_id, st.status, STATUS_ENDED, self.clock(),
                               st.session, st.name, None, synthetic, ended=True)

    def _lookup(self, pane_id: str) -> tuple[bool | None, dict | None]:
        """(True, info) live agent; (False, None) definitely no agent; (None, None) lookup failed."""
        try:
            info = self.client.find_agent(pane_id)
        except HerdrError as exc:
            self.stats.lookup_errors += 1
            log.warning("agent.get %s failed: %s", pane_id, exc)
            self._mark_dirty(pane_id)
            return None, None
        return (info is not None), info

    def _mark_dirty(self, pane_id: str) -> None:
        with self._lock:
            if self._stale():
                return
            self._dirty.add(pane_id)
            if self.retry_delay is None or not self._active:
                return
            q = self._queue
            timer = threading.Timer(self.retry_delay, q.put, args=(_Recheck(pane_id),))
            timer.daemon = True
            self._timers = [t for t in self._timers if t.is_alive()] + [timer]
        timer.start()

    # --- streams -----------------------------------------------------------------
    def _subscribe(self, pane_id: str, info: dict) -> None:
        with self._lock:
            gen, q = getattr(self._tls, "gen", None) or self._gen, self._queue
            if not self._active or gen != self._gen:
                return
        try:
            stream = self.client.subscribe([{"type": STATUS_CHANGED, "pane_id": pane_id}])
        except HerdrError as exc:
            self.stats.stream_errors += 1
            log.warning("subscribe %s failed: %s", pane_id, exc)
            self._mark_dirty(pane_id)
            return
        sub = _Sub(pane_id, stream)
        with self._lock:
            if not self._active or gen != self._gen:
                installed = False  # stopped while the pipe was opening
            else:
                installed = True
                old = self._subs.pop(pane_id, None)
                self._subs[pane_id] = sub
        if not installed:
            _close_quietly(stream)
            return
        if old is not None:
            _close_quietly(old.stream)
        sub.thread = self._spawn_reader(sub, q)
        # Apply live state after the stream is open: anything buffered on the stream
        # is older than or equal to this lookup and will be ignored by `_reconcile`.
        found, fresh = self._lookup(pane_id)
        if found is None:
            return
        if not found:
            self._apply_absent(pane_id, synthetic=True)
        else:
            self._reconcile(pane_id, fresh or info, synthetic=True)

    def _unsubscribe(self, pane_id: str) -> None:
        with self._lock:
            if self._stale():
                return
            sub = self._subs.pop(pane_id, None)
        if sub is not None:
            _close_quietly(sub.stream)

    def _open_lifecycle(self) -> None:
        with self._lock:
            gen, q = self._gen, self._queue
            if not self._active:
                return
        try:
            stream = self.client.subscribe(LIFECYCLE_SUBSCRIPTIONS)
        except HerdrError as exc:
            self.stats.stream_errors += 1
            log.warning("lifecycle subscribe failed: %s", exc)
            return
        sub = _Sub("*lifecycle*", stream)
        with self._lock:
            installed = self._active and gen == self._gen
            if installed:
                old, self._lifecycle = self._lifecycle, sub
        if not installed:
            _close_quietly(stream)
            return
        if old is not None:
            _close_quietly(old.stream)
        sub.thread = self._spawn_reader(sub, q)

    def _spawn_reader(self, sub: _Sub, q: queue.Queue) -> threading.Thread:
        t = threading.Thread(target=self._read_loop, args=(sub, q), name=f"herdr-sub-{sub.key}", daemon=True)
        t.start()
        return t

    def _read_loop(self, sub: _Sub, q: queue.Queue) -> None:
        try:
            for envelope in sub.stream:
                event = parse_event(envelope, self.clock())
                if event is not None:
                    if event.kind == STATUS_CHANGED and event.pane_id:
                        with self._lock:
                            self._inflight[event.pane_id] = self._inflight.get(event.pane_id, 0) + 1
                    q.put(event)
        except Exception as exc:  # stream died (server restart, cancelled read, ...)
            if not getattr(sub.stream, "closed", False):
                self.stats.stream_errors += 1
                log.warning("event stream %s ended: %s", sub.key, exc)
        finally:
            _close_quietly(sub.stream)

    # --- worker loops --------------------------------------------------------------
    def _dispatch_loop(self, q: queue.Queue, gen: int) -> None:
        self._tls.gen = gen
        while True:
            item = q.get()
            if item is None:
                return
            self._dispatch(item)

    def _dispatch(self, item) -> None:
        try:
            if item is _RESYNC:
                self.resync()
            elif isinstance(item, _Recheck):
                if item.pane_id in self.dirty_panes():
                    self.recheck(item.pane_id)
            else:
                self.handle_event(item)
        except Exception:
            log.exception("error dispatching %r", item)
        finally:
            if isinstance(item, HerdrEvent) and item.kind == STATUS_CHANGED and item.pane_id:
                with self._lock:
                    left = self._inflight.get(item.pane_id, 0) - 1
                    if left > 0:
                        self._inflight[item.pane_id] = left
                    else:
                        self._inflight.pop(item.pane_id, None)

    def _resync_loop(self, stop: threading.Event, q: queue.Queue) -> None:
        while not stop.wait(self.resync_interval):
            q.put(_RESYNC)

    def _emit(self, transition: AgentTransition) -> None:
        if self._stale():
            return
        self.stats.transitions += 1
        try:
            self.on_transition(transition)
        except Exception:
            log.exception("on_transition failed for %s", transition.pane_id)


def _close_quietly(stream) -> None:
    try:
        stream.close()
    except Exception:
        pass
