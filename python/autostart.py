"""开机自启 via Windows Task Scheduler (demand doc section 5.3).

The spec requires autostart through the Task Scheduler with
"Run with highest privileges" so the app can operate Bluetooth /
network adapters and force-close elevated ClashMi processes without
a UAC prompt on every logon, and so it starts silently into the
tray (``--minimized``, matching the existing installer autostart
shortcut behaviour) instead of popping the main window.

Only Windows can do this. On any other platform every entry point
reports ``unsupported`` and never claims success: ``register`` /
``unregister`` return a dict with ``ok=False`` and
``status="unsupported"``, and ``is_registered`` returns ``None``
(unknown -- not ``False``, which would falsely claim "not
registered"). All schtasks interaction goes through ``subprocess``;
``runner`` parameters are test seams that stand in for
``subprocess.run`` (and bypass the platform gate so the Windows
command shapes can be tested on Linux).
"""

from __future__ import annotations

import subprocess
import sys
from typing import Optional

DEFAULT_TASK_NAME = "XiaoMaLink"


def _quote_command(exe_path: str) -> str:
    """Quote an exe path for schtasks /TR, preserving existing quotes."""
    path = str(exe_path).strip()
    if path.startswith('"'):
        return path
    return f'"{path}"'


def build_schtasks_args(exe_path: str, task_name: str = DEFAULT_TASK_NAME) -> list:
    """Pure function: schtasks arguments registering autostart.

    ``/TR`` carries ``"<exe>" --minimized`` so a logon start goes
    straight to the tray (demand doc 5.3: silent tray entry, no main
    window). ``/SC ONLOGON`` + ``/RL HIGHEST`` + ``/F`` are exactly
    as specified; no other flags are added here.
    """
    target = f"{_quote_command(exe_path)} --minimized"
    return [
        "schtasks",
        "/CREATE",
        "/TN",
        task_name,
        "/TR",
        target,
        "/SC",
        "ONLOGON",
        "/RL",
        "HIGHEST",
        "/F",
    ]


def _run(args: list, runner=None):
    if runner is not None:
        return runner(args)
    return subprocess.run(args, capture_output=True, text=True, check=False)


def _result_text(result, attr: str) -> str:
    value = getattr(result, attr, "")
    return value if isinstance(value, str) else ""


def register(exe_path: str, task_name: str = DEFAULT_TASK_NAME, runner=None) -> dict:
    """Register the logon task. Non-Windows -> unsupported, never ok."""
    if runner is None and sys.platform != "win32":
        return {
            "ok": False,
            "status": "unsupported",
            "task_name": task_name,
            "error": "Task Scheduler autostart is Windows-only",
        }
    try:
        result = _run(build_schtasks_args(exe_path, task_name), runner=runner)
    except Exception as exc:  # e.g. schtasks missing
        return {"ok": False, "status": "failed", "task_name": task_name, "error": str(exc)}
    code = getattr(result, "returncode", 1)
    if code == 0:
        return {"ok": True, "status": "registered", "task_name": task_name}
    return {
        "ok": False,
        "status": "failed",
        "task_name": task_name,
        "returncode": code,
        "stdout": _result_text(result, "stdout"),
        "stderr": _result_text(result, "stderr"),
    }


def unregister(task_name: str = DEFAULT_TASK_NAME, runner=None) -> dict:
    """Delete the logon task. Non-Windows -> unsupported, never ok."""
    if runner is None and sys.platform != "win32":
        return {
            "ok": False,
            "status": "unsupported",
            "task_name": task_name,
            "error": "Task Scheduler autostart is Windows-only",
        }
    try:
        result = _run(["schtasks", "/DELETE", "/TN", task_name, "/F"], runner=runner)
    except Exception as exc:
        return {"ok": False, "status": "failed", "task_name": task_name, "error": str(exc)}
    code = getattr(result, "returncode", 1)
    if code == 0:
        return {"ok": True, "status": "unregistered", "task_name": task_name}
    return {
        "ok": False,
        "status": "failed",
        "task_name": task_name,
        "returncode": code,
        "stdout": _result_text(result, "stdout"),
        "stderr": _result_text(result, "stderr"),
    }


def is_registered(task_name: str = DEFAULT_TASK_NAME, runner=None) -> Optional[bool]:
    """True if the task exists, False if not, None if unknowable.

    Non-Windows (without an injected runner) returns None: claiming
    False there would be a lie, since schtasks cannot even run.
    """
    if runner is None and sys.platform != "win32":
        return None
    try:
        result = _run(["schtasks", "/QUERY", "/TN", task_name], runner=runner)
    except Exception:
        return None
    return getattr(result, "returncode", 1) == 0
