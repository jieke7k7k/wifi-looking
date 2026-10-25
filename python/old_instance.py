"""Discovery and takeover of leftover old-version instances.

Background (feasibility survey, "old version has no single-instance
protection"): old builds created no lock file, mutex, or local
socket, so the new build's guard cannot see them, and a rename of
the program / install directory makes the guard even blinder. A
leftover old instance keeps running its stale rule set, which is one
mechanism behind "rules are listed but switching does not trigger".

This module therefore enumerates candidate old processes by exe
name / exe path and ends them. Deliberately, it does NOT merely send
a window-close message: for a tray program, closing the window by
default only minimizes to the tray and the process keeps running.
The order is: ``terminate()`` (gentle), wait up to 3 seconds, then
``kill()`` if still alive. ``AccessDenied`` is recorded, never
raised -- e.g. an old instance running elevated while the new one
is not must produce a clear report (and a user prompt upstream),
not a crash and not a silent second instance.
"""

from __future__ import annotations

import os
import time
from typing import Iterable, Optional

DEFAULT_EXE_NAMES = ("WiFi-Looking.exe", "XiaoMaLink.exe")

try:
    import psutil as _psutil

    _AccessDenied = _psutil.AccessDenied
    _NoSuchProcess = _psutil.NoSuchProcess
    _TimeoutExpired = _psutil.TimeoutExpired
except Exception:  # psutil is a declared dependency, but stay importable
    _psutil = None
    _AccessDenied = None
    _NoSuchProcess = None
    _TimeoutExpired = None


def _is_access_denied(exc: BaseException) -> bool:
    if _AccessDenied is not None and isinstance(exc, _AccessDenied):
        return True
    return type(exc).__name__ == "AccessDenied"


def _is_no_such_process(exc: BaseException) -> bool:
    if _NoSuchProcess is not None and isinstance(exc, _NoSuchProcess):
        return True
    return type(exc).__name__ == "NoSuchProcess"


def _is_timeout(exc: BaseException) -> bool:
    if _TimeoutExpired is not None and isinstance(exc, _TimeoutExpired):
        return True
    return isinstance(exc, TimeoutError) or type(exc).__name__ == "TimeoutExpired"


def _basename(path: Optional[str]) -> str:
    if not path:
        return ""
    return path.replace("\\", "/").rsplit("/", 1)[-1]


def _under_dir(exe: Optional[str], install_dirs: Iterable[str]) -> bool:
    if not exe:
        return False
    normalized = exe.replace("\\", "/").lower()
    for directory in install_dirs:
        prefix = directory.replace("\\", "/").rstrip("/").lower() + "/"
        if normalized.startswith(prefix):
            return True
    return False


def find_candidate_processes(
    exe_names: Iterable[str] = DEFAULT_EXE_NAMES,
    install_dirs: Optional[Iterable[str]] = None,
    process_iter=None,
):
    """Return processes that look like an old (or same-name) instance.

    A process matches when its exe basename (or its reported name)
    is in ``exe_names``, or when its exe path lives under one of
    ``install_dirs``. ``process_iter`` is a test seam: when given,
    it is iterated instead of ``psutil.process_iter``. The current
    process is always excluded, so a new XiaoMaLink.exe never
    nominates itself for takeover.
    """
    wanted = {name.lower() for name in exe_names}
    dirs = list(install_dirs) if install_dirs else []
    if process_iter is None:
        if _psutil is None:
            return []
        try:
            process_iter = _psutil.process_iter(["pid", "name", "exe"])
        except Exception:
            return []
    self_pid = os.getpid()
    candidates = []
    for proc in process_iter:
        try:
            pid = proc.pid if isinstance(proc.pid, int) else proc.info.get("pid")
        except Exception:
            pid = None
        if pid is not None and pid == self_pid:
            continue
        try:
            info = getattr(proc, "info", None) or {}
            name = proc.name() if callable(getattr(proc, "name", None)) else info.get("name")
            exe = None
            if callable(getattr(proc, "exe", None)):
                try:
                    exe = proc.exe()
                except Exception:
                    exe = info.get("exe")
            else:
                exe = info.get("exe")
        except Exception:
            continue
        if _basename(exe).lower() in wanted or (name or "").lower() in wanted:
            candidates.append(proc)
        elif dirs and _under_dir(exe, dirs):
            candidates.append(proc)
    return candidates


def _proc_pid(proc) -> Optional[int]:
    try:
        pid = proc.pid
        return pid if isinstance(pid, int) else None
    except Exception:
        return None


def _wait_gone(proc, timeout: float) -> bool:
    """True if ``proc`` is gone within ``timeout`` seconds."""
    wait = getattr(proc, "wait", None)
    if callable(wait):
        try:
            wait(timeout=timeout)
            return True
        except Exception as exc:
            if _is_timeout(exc):
                return False
            if _is_no_such_process(exc):
                return True
            raise
    deadline = time.monotonic() + timeout
    is_running = getattr(proc, "is_running", None)
    while True:
        try:
            alive = is_running() if callable(is_running) else True
        except Exception as exc:
            if _is_no_such_process(exc):
                return True
            raise
        if not alive:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)


def takeover_old_instances(
    dry_run: bool = False,
    candidates=None,
    process_iter=None,
    exe_names: Iterable[str] = DEFAULT_EXE_NAMES,
    install_dirs: Optional[Iterable[str]] = None,
    wait_timeout: float = 3.0,
) -> dict:
    """End candidate old instances; return a structured report.

    Report shape::

        {
            "found": int,              # candidates seen
            "terminated": [pid, ...],  # gone after gentle terminate()
            "killed": [pid, ...],      # needed kill() after the wait
            "access_denied": [pid, ...],  # could not be touched
            "errors": [{"pid": pid, "error": str}, ...],
        }

    ``dry_run=True`` only counts candidates and touches nothing.
    ``candidates`` / ``process_iter`` are test seams. ``wait_timeout``
    defaults to the spec's 3 seconds. ``AccessDenied`` (and any other
    per-process failure) is recorded in the report, never raised.
    """
    if candidates is None:
        candidates = find_candidate_processes(
            exe_names=exe_names, install_dirs=install_dirs, process_iter=process_iter
        )
    report: dict = {
        "found": len(candidates),
        "terminated": [],
        "killed": [],
        "access_denied": [],
        "errors": [],
    }
    if dry_run:
        return report
    for proc in candidates:
        pid = _proc_pid(proc)
        try:
            proc.terminate()
        except Exception as exc:
            if _is_access_denied(exc):
                report["access_denied"].append(pid)
            elif _is_no_such_process(exc):
                report["terminated"].append(pid)
            else:
                report["errors"].append({"pid": pid, "error": str(exc)})
            continue
        try:
            gone = _wait_gone(proc, wait_timeout)
        except Exception as exc:
            if _is_access_denied(exc):
                report["access_denied"].append(pid)
            elif _is_no_such_process(exc):
                report["terminated"].append(pid)
            else:
                report["errors"].append({"pid": pid, "error": str(exc)})
            continue
        if gone:
            report["terminated"].append(pid)
            continue
        try:
            proc.kill()
        except Exception as exc:
            if _is_access_denied(exc):
                report["access_denied"].append(pid)
            elif _is_no_such_process(exc):
                report["terminated"].append(pid)
            else:
                report["errors"].append({"pid": pid, "error": str(exc)})
            continue
        try:
            _wait_gone(proc, 1.0)
        except Exception:
            pass
        report["killed"].append(pid)
    return report
