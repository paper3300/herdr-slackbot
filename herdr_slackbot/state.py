"""Persistent bridge state (STATE_DIR/state.json) and the single-instance lock.

state.json layout (version 1):
{
  "version": 1,
  "counter": 7,                      # last used N for auto names `slack-<N>` (never reused)
  "threads": {
    "<agent_session id>": {
      "channel": "D0123", "thread_ts": "1727.0001",
      "origin": "slack" | "pc",      # who started the conversation with this agent
      "muted": false,
      "agent_name": "slack-7", "pane_id": "w1:p3", "workspace_id": "w1",
      "created_at": 1727650000.0,
      "pending_task": {"started_at": ..., "working_announced": false} | null,
      "dialog": {"token", "fingerprint", "kind", "options", "message_ts", ...} | null,
                                     # the open blocked-dialog message (answered from Slack)
      "deferred_prompt": {"text": ...} | null  # a new agent's first prompt, sent once its
                                     # startup dialog (folder trust) is answered
    }
  }
}
Writes go to a temp file in the same directory and are swapped in with os.replace.
Every mutation is transactional: it is applied to a copy, persisted, and only then
becomes visible in memory. A failed write leaves memory and disk unchanged.

The auto-name counter also has a high-water-mark file (`state.counter.hwm`) that is
written before a number is handed out. A corrupt state.json therefore cannot cause
a number to be reissued. If neither file yields a trustworthy value, allocation is
refused until `recover_counter()` is called with a floor. That condition is persisted
as `state.counter.recovery-required`, so it survives restarts and unrelated writes;
an unreadable high-water file also requires recovery (it may hold burned numbers).
"""

from __future__ import annotations

import copy
import json
import logging
import os
import re
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger(__name__)

STATE_VERSION = 1
ORIGIN_SLACK = "slack"
ORIGIN_PC = "pc"

_COUNTER_SALVAGE_RE = re.compile(r'"counter"\s*:\s*(\d+)')


class StateError(Exception):
    pass


def _empty_state() -> dict:
    return {"version": STATE_VERSION, "counter": 0, "threads": {}}


def atomic_write_text(path: Path, text: str, retries: int = 5, delay: float = 0.05) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        for attempt in range(retries):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                # Windows: target briefly opened by another process (AV scanner, editor).
                if attempt == retries - 1:
                    raise
                time.sleep(delay)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def atomic_write_json(path: Path, data: Any, retries: int = 5, delay: float = 0.05) -> None:
    atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True), retries, delay)


class StateStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.hwm_path = self.path.with_name(self.path.stem + ".counter.hwm")
        self.recovery_path = self.path.with_name(self.path.stem + ".counter.recovery-required")
        self._lock = threading.RLock()
        self._counter_ok = True
        self._data = self._load()

    # --- load / commit ---------------------------------------------------------
    def _read_hwm(self) -> tuple[int | None, bool]:
        """(value, trustworthy). A missing file is trustworthy (nothing issued); a corrupt one is not."""
        try:
            return int(self.hwm_path.read_text(encoding="ascii").strip()), True
        except FileNotFoundError:
            return None, True
        except (OSError, ValueError):
            log.error("counter high-water mark %s is unreadable", self.hwm_path)
            return None, False

    def _require_recovery(self, reason: str) -> None:
        """Disable allocation durably until `recover_counter()`. Raises StateError if the
        requirement cannot be persisted (loading is aborted; no evidence has been moved)."""
        self._counter_ok = False
        log.error("auto-name counter needs recovery (%s); allocation disabled until recover_counter()", reason)
        try:
            atomic_write_text(self.recovery_path, reason)
        except OSError as exc:
            raise StateError(f"cannot persist the counter recovery marker: {exc}") from exc

    def _corrupt_backups_exist(self) -> bool:
        return any(self.path.parent.glob(self.path.name + ".corrupt-*"))

    def _load(self) -> dict:
        hwm, hwm_ok = self._read_hwm()
        if self.recovery_path.exists():
            self._counter_ok = False
        elif not hwm_ok:
            self._require_recovery("high-water mark unreadable")
        state = _empty_state()
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            state["counter"] = hwm or 0
            if hwm is None and self._counter_ok and self._corrupt_backups_exist():
                # A previous run moved a corrupt state file away without leaving a floor.
                self._require_recovery("state file was corrupt earlier and no high-water mark exists")
            return state
        try:
            data = json.loads(raw)
            if not isinstance(data, dict) or not isinstance(data.get("threads", {}), dict):
                raise ValueError("unexpected layout")
            state.update(data)
            state["counter"] = max(int(state.get("counter") or 0), hwm or 0)
            return state
        except (ValueError, TypeError) as exc:
            salvaged = [int(m) for m in _COUNTER_SALVAGE_RE.findall(raw)]
            candidates = ([hwm] if hwm is not None else []) + salvaged
            # Make the recovered floor (or the recovery requirement) durable BEFORE the
            # corrupt file -- the only evidence -- is moved away. Failure aborts the load.
            if hwm_ok and candidates:
                floor = max(candidates)
                try:
                    atomic_write_text(self.hwm_path, str(floor))
                except OSError as write_exc:
                    raise StateError(f"cannot persist the recovered counter floor: {write_exc}") from write_exc
                state["counter"] = floor
            elif self._counter_ok:
                self._require_recovery("state file corrupt and counter not recoverable")
            backup = self.path.with_name(f"{self.path.name}.corrupt-{int(time.time())}")
            log.error("state file %s is unreadable (%s); moved to %s", self.path, exc, backup)
            os.replace(self.path, backup)
            return state

    def _commit(self, mutate: Callable[[dict], Any]) -> Any:
        """Apply `mutate` to a copy, persist it, then publish it. Returns a copy of mutate's result."""
        with self._lock:
            candidate = copy.deepcopy(self._data)
            result = mutate(candidate)
            atomic_write_json(self.path, candidate)
            self._data = candidate
            return copy.deepcopy(result)

    def snapshot(self) -> dict:
        with self._lock:
            return copy.deepcopy(self._data)

    # --- counter -------------------------------------------------------------
    @property
    def counter_ok(self) -> bool:
        return self._counter_ok

    def next_counter(self) -> int:
        """Reserve and persist the next `slack-<N>` number (never reissued)."""
        with self._lock:
            if not self._counter_ok:
                raise StateError("auto-name counter was lost; call recover_counter() with a safe floor")
            hwm, _ = self._read_hwm()
            n = max(self._data["counter"], hwm or 0) + 1
            atomic_write_text(self.hwm_path, str(n))  # burn the number before it is used

            def mutate(data: dict) -> int:
                data["counter"] = n
                return n
            return self._commit(mutate)

    def recover_counter(self, floor: int) -> None:
        """Re-enable allocation after data loss; `floor` = highest number known to be used."""
        with self._lock:
            hwm, _ = self._read_hwm()
            n = max(int(floor), self._data["counter"], hwm or 0)
            atomic_write_text(self.hwm_path, str(n))

            def mutate(data: dict) -> None:
                data["counter"] = n
            self._commit(mutate)
            try:
                self.recovery_path.unlink()
            except FileNotFoundError:
                pass
            self._counter_ok = True

    # --- threads -------------------------------------------------------------
    def get_thread(self, session: str) -> dict | None:
        with self._lock:
            entry = self._data["threads"].get(session)
            return copy.deepcopy(entry) if entry is not None else None

    def all_threads(self) -> dict[str, dict]:
        with self._lock:
            return copy.deepcopy(self._data["threads"])

    def upsert_thread(self, session: str, **fields: Any) -> dict:
        if not session:
            raise ValueError("agent_session id is required")
        fields = copy.deepcopy(fields)  # never retain caller-owned mutable objects

        def mutate(data: dict) -> dict:
            entry = data["threads"].setdefault(session, {
                "origin": ORIGIN_PC, "muted": False, "pending_task": None, "created_at": time.time(),
            })
            entry.update(fields)
            return entry
        return self._commit(mutate)

    def rekey_thread(self, old: str, new: str) -> dict | None:
        """Move a thread entry to a new key (provisional key -> real agent session)."""
        if not new:
            raise ValueError("new key is required")

        def mutate(data: dict) -> dict | None:
            entry = data["threads"].pop(old, None)
            if entry is None:
                return data["threads"].get(new)
            entry.pop("provisional", None)
            existing = data["threads"].get(new) or {}
            merged = {**existing, **entry}
            data["threads"][new] = merged
            return merged
        with self._lock:
            if old not in self._data["threads"]:
                return self.get_thread(new)
            return self._commit(mutate)

    def find_provisional(self, pane_id: str, terminal_id: str | None = None) -> str | None:
        """Key of a provisional thread (agent without a session yet) for `pane_id`, or for
        `terminal_id` (a pane move changes the pane id but not the terminal)."""
        with self._lock:
            for key, entry in self._data["threads"].items():
                if entry.get("provisional") and entry.get("pane_id") == pane_id:
                    return key
            if terminal_id:
                for key, entry in self._data["threads"].items():
                    if entry.get("provisional") and entry.get("terminal_id") == terminal_id:
                        return key
        return None

    def find_provisional_by_terminal(self, terminal_id: str | None) -> str | None:
        """Key of the provisional thread of exactly this terminal (positive identity match only)."""
        if not terminal_id:
            return None
        with self._lock:
            for key, entry in self._data["threads"].items():
                if entry.get("provisional") and entry.get("terminal_id") == terminal_id:
                    return key
        return None

    def find_task(self, task_id: str | None) -> str | None:
        """Key of the thread whose pending task is `task_id` (follows re-keying)."""
        if not task_id:
            return None
        with self._lock:
            for key, entry in self._data["threads"].items():
                if (entry.get("pending_task") or {}).get("task_id") == task_id:
                    return key
        return None

    def find_dialog(self, token: str | None) -> str | None:
        """Key of the thread whose open dialog record has `token` (follows re-keying)."""
        if not token:
            return None
        with self._lock:
            for key, entry in self._data["threads"].items():
                if (entry.get("dialog") or {}).get("token") == token:
                    return key
        return None

    def remove_thread(self, session: str) -> None:
        with self._lock:
            if session in self._data["threads"]:
                self._commit(lambda data: data["threads"].pop(session, None))

    def find_session_by_thread(self, channel: str, thread_ts: str) -> str | None:
        with self._lock:
            for session, entry in self._data["threads"].items():
                if entry.get("channel") == channel and entry.get("thread_ts") == thread_ts:
                    return session
        return None

    def set_muted(self, session: str, muted: bool) -> dict:
        return self.upsert_thread(session, muted=bool(muted))

    def is_muted(self, session: str) -> bool:
        with self._lock:
            return bool(self._data["threads"].get(session, {}).get("muted"))

    def set_pending_task(self, session: str, task: dict | None) -> dict:
        return self.upsert_thread(session, pending_task=task)


class AlreadyRunning(Exception):
    pass


class InstanceLock:
    """Exclusive OS lock on STATE_DIR/bridge.lock; the holder's pid goes to bridge.pid.

    The OS drops the lock when the process dies, so a stale file never blocks startup.
    """

    def __init__(self, state_dir: Path):
        self.state_dir = Path(state_dir)
        self.lock_path = self.state_dir / "bridge.lock"
        self.pid_path = self.state_dir / "bridge.pid"
        self._fh = None

    def acquire(self) -> "InstanceLock":
        self.state_dir.mkdir(parents=True, exist_ok=True)
        fh = open(self.lock_path, "a+b")
        try:
            _lock_file(fh)
        except OSError as exc:
            fh.close()
            raise AlreadyRunning(f"another bridge is running (pid {self.read_pid() or '?'})") from exc
        self._fh = fh
        try:
            self.pid_path.write_text(str(os.getpid()), encoding="ascii")
        except BaseException:
            self.release()  # a failed startup must not keep holding the lock
            raise
        return self

    def is_held(self) -> bool:
        """True when some process (a running bridge) holds the lock. Never writes the pid file."""
        if not self.lock_path.exists():
            return False
        try:
            fh = open(self.lock_path, "a+b")
        except OSError:
            return False
        try:
            _lock_file(fh)
        except OSError:
            return True
        else:
            _unlock_file(fh)
            return False
        finally:
            fh.close()

    def read_pid(self) -> int | None:
        try:
            return int(self.pid_path.read_text(encoding="ascii").strip())
        except (OSError, ValueError):
            return None

    def release(self) -> None:
        if self._fh is None:
            return
        try:
            _unlock_file(self._fh)
        except OSError:
            pass
        self._fh.close()
        self._fh = None
        try:
            if self.read_pid() == os.getpid():
                self.pid_path.unlink()
        except OSError:
            pass

    def __enter__(self) -> "InstanceLock":
        return self.acquire()

    def __exit__(self, *exc) -> None:
        self.release()


if os.name == "nt":
    import msvcrt

    def _lock_file(fh) -> None:
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)

    def _unlock_file(fh) -> None:
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
else:
    import fcntl

    def _lock_file(fh) -> None:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock_file(fh) -> None:
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
