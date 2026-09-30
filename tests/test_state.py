import json
import subprocess
import sys
import textwrap
import threading

import pytest

from herdr_slackbot import state as statemod
from herdr_slackbot.state import (
    ORIGIN_PC,
    ORIGIN_SLACK,
    AlreadyRunning,
    InstanceLock,
    StateError,
    StateStore,
    atomic_write_json,
)


def test_counter_is_monotonic_and_persisted(tmp_path):
    path = tmp_path / "state.json"
    store = StateStore(path)
    assert [store.next_counter() for _ in range(3)] == [1, 2, 3]
    assert StateStore(path).next_counter() == 4


def test_counter_thread_safe(tmp_path):
    store = StateStore(tmp_path / "state.json")
    results = []
    threads = [threading.Thread(target=lambda: results.append(store.next_counter())) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results) == list(range(1, 21))


def test_thread_map_roundtrip(tmp_path):
    path = tmp_path / "state.json"
    store = StateStore(path)
    entry = store.upsert_thread("sess-1", channel="D1", thread_ts="100.1", origin=ORIGIN_SLACK,
                                agent_name="slack-1", pane_id="w1:p2")
    assert entry["muted"] is False and entry["pending_task"] is None
    store.set_muted("sess-1", True)
    store.set_pending_task("sess-1", {"started_at": 5.0, "working_announced": False})

    reloaded = StateStore(path)
    got = reloaded.get_thread("sess-1")
    assert got["channel"] == "D1" and got["origin"] == ORIGIN_SLACK
    assert reloaded.is_muted("sess-1")
    assert got["pending_task"]["started_at"] == 5.0
    assert reloaded.find_session_by_thread("D1", "100.1") == "sess-1"
    assert reloaded.find_session_by_thread("D1", "999") is None

    got["channel"] = "mutated"  # returned dicts are copies
    assert reloaded.get_thread("sess-1")["channel"] == "D1"

    reloaded.remove_thread("sess-1")
    assert StateStore(path).get_thread("sess-1") is None


def test_default_origin_is_pc(tmp_path):
    store = StateStore(tmp_path / "s.json")
    assert store.upsert_thread("s")["origin"] == ORIGIN_PC
    assert not store.is_muted("unknown-session")
    with pytest.raises(ValueError):
        store.upsert_thread("")


def test_atomic_write_leaves_no_temp_files(tmp_path):
    path = tmp_path / "sub" / "x.json"
    atomic_write_json(path, {"a": "한글"})
    atomic_write_json(path, {"a": 2})
    assert json.loads(path.read_text(encoding="utf-8")) == {"a": 2}
    assert [p.name for p in path.parent.iterdir()] == ["x.json"]


def test_instance_lock_in_process(tmp_path):
    first = InstanceLock(tmp_path).acquire()
    try:
        assert first.read_pid() is not None
        with pytest.raises(AlreadyRunning):
            InstanceLock(tmp_path).acquire()
    finally:
        first.release()
    with InstanceLock(tmp_path):
        pass


def test_instance_lock_across_processes(tmp_path):
    script = textwrap.dedent(f"""
        import sys
        sys.path.insert(0, {str(__import__('pathlib').Path(__file__).resolve().parent.parent)!r})
        from herdr_slackbot.state import InstanceLock, AlreadyRunning
        try:
            InstanceLock({str(tmp_path)!r}).acquire()
        except AlreadyRunning:
            sys.exit(3)
        sys.exit(0)
    """)
    with InstanceLock(tmp_path):
        assert subprocess.run([sys.executable, "-c", script]).returncode == 3
    assert subprocess.run([sys.executable, "-c", script]).returncode == 0


# --- review #7: failed persistence leaves memory unchanged -----------------------------

def _failing_write(*args, **kwargs):
    raise PermissionError(32, "sharing violation")


def test_review7_failed_save_keeps_memory_and_disk_consistent(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    store = StateStore(path)
    store.upsert_thread("s1", channel="D1", thread_ts="1.0")
    store.set_pending_task("s1", {"started_at": 1.0, "working_announced": False})

    monkeypatch.setattr(statemod, "atomic_write_json", _failing_write)
    with pytest.raises(PermissionError):
        store.set_pending_task("s1", None)  # clearing fails
    with pytest.raises(PermissionError):
        store.upsert_thread("s2", channel="D2")
    with pytest.raises(PermissionError):
        store.remove_thread("s1")
    assert store.get_thread("s1")["pending_task"] == {"started_at": 1.0, "working_announced": False}
    assert store.get_thread("s2") is None

    monkeypatch.undo()
    store.set_muted("s1", True)  # a later successful, unrelated mutation
    disk = StateStore(path)
    assert disk.get_thread("s1")["pending_task"] == {"started_at": 1.0, "working_announced": False}
    assert disk.get_thread("s2") is None and disk.is_muted("s1")


def test_review7_failed_counter_save_does_not_advance_memory(tmp_path, monkeypatch):
    store = StateStore(tmp_path / "state.json")
    assert store.next_counter() == 1
    monkeypatch.setattr(statemod, "atomic_write_json", _failing_write)
    with pytest.raises(PermissionError):
        store.next_counter()
    assert store.snapshot()["counter"] == 1
    monkeypatch.undo()
    # The failed attempt burned number 2 in the high-water mark: it is never handed out.
    assert store.next_counter() == 3


# --- review #8: corruption never reissues numbers ------------------------------------

def test_review8_corrupt_state_recovers_counter_from_high_water_mark(tmp_path):
    path = tmp_path / "state.json"
    store = StateStore(path)
    assert [store.next_counter() for _ in range(3)] == [1, 2, 3]
    path.write_text("{not json", encoding="utf-8")
    recovered = StateStore(path)
    assert recovered.next_counter() == 4
    assert list(tmp_path.glob("state.json.corrupt-*"))


def test_review8_counter_salvaged_from_corrupt_text_when_hwm_missing(tmp_path):
    path = tmp_path / "state.json"
    path.write_text('{"counter": 17, "threads": {"x": ', encoding="utf-8")  # truncated write
    assert StateStore(path).next_counter() == 18


def test_review8_unrecoverable_counter_refuses_allocation(tmp_path):
    path = tmp_path / "state.json"
    store = StateStore(path)
    store.next_counter()
    path.write_text("garbage", encoding="utf-8")
    store.hwm_path.write_text("also garbage", encoding="ascii")
    broken = StateStore(path)
    assert not broken.counter_ok
    with pytest.raises(StateError):
        broken.next_counter()
    broken.recover_counter(5)  # e.g. highest live slack-<N>
    assert broken.next_counter() == 6
    assert StateStore(path).next_counter() == 7


def test_review8_missing_state_with_hwm_keeps_numbering(tmp_path):
    path = tmp_path / "state.json"
    store = StateStore(path)
    store.next_counter()
    store.next_counter()
    path.unlink()
    assert StateStore(path).next_counter() == 3


# --- review #11: caller-owned data is copied ---------------------------------------

def test_review11_nested_input_is_not_retained(tmp_path):
    path = tmp_path / "state.json"
    store = StateStore(path)
    task = {"started_at": 1.0, "working_announced": False}
    store.set_pending_task("s1", task)
    task["working_announced"] = True
    meta = {"tags": ["a"]}
    store.upsert_thread("s1", meta=meta)
    meta["tags"].append("b")
    assert store.get_thread("s1")["pending_task"]["working_announced"] is False
    assert store.get_thread("s1")["meta"] == {"tags": ["a"]}
    assert StateStore(path).get_thread("s1")["pending_task"]["working_announced"] is False


# --- review #12: failed pid write releases the lock -----------------------------------

def test_review12_pid_write_failure_releases_lock(tmp_path, monkeypatch):
    lock = InstanceLock(tmp_path)
    real_write_text = statemod.Path.write_text

    def failing_write_text(self, *a, **k):
        if self.name == "bridge.pid":
            raise PermissionError(5, "access denied")
        return real_write_text(self, *a, **k)

    monkeypatch.setattr(statemod.Path, "write_text", failing_write_text)
    with pytest.raises(PermissionError):
        lock.acquire()
    assert lock._fh is None
    monkeypatch.undo()
    with InstanceLock(tmp_path):  # another instance can start right away
        pass


def test_rekey_and_find_provisional(tmp_path):
    path = tmp_path / "state.json"
    store = StateStore(path)
    store.upsert_thread("pending:t1", channel="D", thread_ts="1.0", pane_id="w1:p1", provisional=True)
    assert store.find_provisional("w1:p1") == "pending:t1"
    assert store.find_provisional("w1:p2") is None
    moved = store.rekey_thread("pending:t1", "S1")
    assert moved["thread_ts"] == "1.0" and "provisional" not in moved
    assert StateStore(path).get_thread("pending:t1") is None
    assert StateStore(path).find_session_by_thread("D", "1.0") == "S1"
    assert store.find_provisional("w1:p1") is None
    assert store.rekey_thread("missing", "S1")["thread_ts"] == "1.0"  # no-op returns the target


# --- recheck R8: the recovery requirement is durable ----------------------------------------------

def test_recheck_r8_disabled_state_survives_second_restart(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("{corrupt legacy state", encoding="utf-8")  # no hwm file, nothing to salvage
    first = StateStore(path)
    assert not first.counter_ok
    second = StateStore(path)  # primary files are gone now, but the evidence is persisted
    assert not second.counter_ok
    with pytest.raises(StateError):
        second.next_counter()


def test_recheck_r8_unrelated_write_then_restart_stays_disabled(tmp_path):
    path = tmp_path / "state.json"
    store = StateStore(path)
    store.next_counter()
    path.write_text("garbage", encoding="utf-8")
    store.hwm_path.write_text("also garbage", encoding="ascii")
    broken = StateStore(path)
    broken.upsert_thread("s1", channel="D")  # persists a valid state.json with counter 0
    reopened = StateStore(path)
    assert not reopened.counter_ok
    with pytest.raises(StateError):
        reopened.next_counter()
    assert reopened.get_thread("s1")["channel"] == "D"


def test_recheck_r8_unreadable_hwm_with_valid_state_requires_recovery(tmp_path):
    path = tmp_path / "state.json"
    store = StateStore(path)
    store.next_counter()
    store.next_counter()
    store.hwm_path.write_text("??", encoding="ascii")
    reopened = StateStore(path)
    assert not reopened.counter_ok
    reopened.recover_counter(10)
    assert reopened.next_counter() == 11
    again = StateStore(path)  # recovery cleared the durable marker
    assert again.counter_ok and again.next_counter() == 12


# --- recheck2 R8 --------------------------------------------------------------------------------

def test_recheck2_r8_salvaged_counter_survives_restart(tmp_path):
    path = tmp_path / "state.json"
    path.write_text('{"counter": 42, "threads": ', encoding="utf-8")  # truncated legacy state, no hwm
    first = StateStore(path)
    assert first.counter_ok
    again = StateStore(path)  # restart before any mutation
    assert again.next_counter() == 43


def test_recheck2_r8_marker_write_failure_aborts_load_and_keeps_evidence(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    path.write_text("{wholly corrupt", encoding="utf-8")
    real = statemod.atomic_write_text

    def fail_marker(p, text, *a, **k):
        if str(p).endswith("recovery-required"):
            raise PermissionError(13, "denied")
        return real(p, text, *a, **k)

    monkeypatch.setattr(statemod, "atomic_write_text", fail_marker)
    with pytest.raises(StateError):
        StateStore(path)
    assert path.read_text(encoding="utf-8") == "{wholly corrupt"  # evidence not moved
    monkeypatch.undo()
    reopened = StateStore(path)
    assert not reopened.counter_ok
    with pytest.raises(StateError):
        reopened.next_counter()
    assert not StateStore(path).counter_ok


def test_recheck2_r8_floor_write_failure_aborts_load(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    path.write_text('{"counter": 7, "threads": ', encoding="utf-8")
    monkeypatch.setattr(statemod, "atomic_write_text",
                        lambda *a, **k: (_ for _ in ()).throw(PermissionError(13, "denied")))
    with pytest.raises(StateError):
        StateStore(path)
    assert path.exists()
    monkeypatch.undo()
    assert StateStore(path).next_counter() == 8


def test_recheck2_r8_leftover_corrupt_backup_counts_as_evidence(tmp_path):
    path = tmp_path / "state.json"
    (tmp_path / "state.json.corrupt-123").write_text("junk", encoding="utf-8")  # older run, no hwm/marker
    store = StateStore(path)
    assert not store.counter_ok
    store.recover_counter(9)
    assert StateStore(path).next_counter() == 10
