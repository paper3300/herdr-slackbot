"""Process identity helpers (Windows via ctypes; best effort elsewhere).

Used by the plugin launcher: a pane is only typed into when its shell has no child
processes (Herdr on Windows reports the shell as the pane's foreground process even
while a program runs in it), and a PID is only terminated when its creation time still
matches the running bridge's record (PIDs are reused).
"""

from __future__ import annotations

import os
import subprocess
import sys

# Helper processes a Windows console shell may own that don't mean "busy".
IGNORED_CHILDREN = {"conhost.exe", "openconsole.exe"}


def child_processes(pid: int) -> list[tuple[int, str]] | None:
    """Direct children of `pid` as (pid, exe name); None when this can't be determined."""
    if sys.platform == "win32":
        return _win_children(pid)
    try:
        out = subprocess.run(["ps", "-o", "pid=,comm=", "--ppid", str(pid)], capture_output=True, text=True,
                             timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode not in (0, 1):
        return None
    children = []
    for line in out.stdout.splitlines():
        parts = line.split(None, 1)
        if len(parts) == 2 and parts[0].isdigit():
            children.append((int(parts[0]), parts[1].strip()))
    return children


def busy_children(pid: int) -> list[tuple[int, str]] | None:
    children = child_processes(pid)
    if children is None:
        return None
    return [(p, n) for p, n in children if n.lower() not in IGNORED_CHILDREN]


def process_start_time(pid: int) -> int | None:
    """Opaque creation-time token for `pid` (stable for the process's lifetime), or None."""
    if not pid or pid <= 0:
        return None
    if sys.platform == "win32":
        return _win_start_time(pid)
    try:
        with open(f"/proc/{pid}/stat", "rb") as fh:
            fields = fh.read().rsplit(b")", 1)[1].split()
        return int(fields[19])  # starttime, in clock ticks since boot
    except (OSError, IndexError, ValueError):
        return None


def current_start_time() -> int | None:
    return process_start_time(os.getpid())


if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    _TH32CS_SNAPPROCESS = 0x00000002
    _PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    _INVALID_HANDLE = ctypes.c_void_p(-1).value

    class _PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_void_p), ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD), ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD), ("szExeFile", ctypes.c_wchar * 260),
        ]

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    _k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    _k32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PROCESSENTRY32W)]
    _k32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PROCESSENTRY32W)]
    _k32.OpenProcess.restype = wintypes.HANDLE
    _k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _k32.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    _k32.CloseHandle.argtypes = [wintypes.HANDLE]

    def _win_children(pid: int) -> list[tuple[int, str]] | None:
        snap = _k32.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
        if not snap or snap == _INVALID_HANDLE:
            return None
        try:
            entry = _PROCESSENTRY32W()
            entry.dwSize = ctypes.sizeof(entry)
            children = []
            ok = _k32.Process32FirstW(snap, ctypes.byref(entry))
            while ok:
                if entry.th32ParentProcessID == pid and entry.th32ProcessID != pid:
                    children.append((int(entry.th32ProcessID), entry.szExeFile))
                ok = _k32.Process32NextW(snap, ctypes.byref(entry))
            # Parent ids can outlive their parent and be reused: keep only children created
            # after the parent (a stale orphan's parent id pointing at a reused pid is not a child).
            parent_start = _win_start_time(pid)
            if parent_start is not None:
                children = [(p, n) for p, n in children
                            if (_win_start_time(p) or parent_start) >= parent_start]
            return children
        finally:
            _k32.CloseHandle(snap)

    def _win_start_time(pid: int) -> int | None:
        handle = _k32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return None
        try:
            created, exited, kernel, user = (wintypes.FILETIME() for _ in range(4))
            if not _k32.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited),
                                        ctypes.byref(kernel), ctypes.byref(user)):
                return None
            return (created.dwHighDateTime << 32) | created.dwLowDateTime
        finally:
            _k32.CloseHandle(handle)
