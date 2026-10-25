"""Notebook power-state reading for XiaoMaLink trigger rules.

Implements spec section 2.3: the power state is an *optional*
pre-condition filter for a rule (any / AC only / battery only).

``get_power_state()`` returns ``"ac"``, ``"battery"`` or
``"unknown"`` and never raises for an expected failure (no battery
API, no battery present, API error): an unreadable state degrades
to ``"unknown"``, which only matches the ``"any"`` condition.
"""

from __future__ import annotations

import ctypes
import logging
import sys
from typing import Callable, Optional

logger = logging.getLogger(__name__)

AC = "ac"
BATTERY = "battery"
UNKNOWN = "unknown"

VALID_CONDITIONS = ("any", "ac", "battery")
VALID_STATES = ("ac", "battery", "unknown")


def _windows_power_state() -> str:
    """Read Windows GetSystemPowerStatus via ctypes (guarded)."""

    class SYSTEM_POWER_STATUS(ctypes.Structure):
        _fields_ = [
            ("ACLineStatus", ctypes.c_byte),
            ("BatteryFlag", ctypes.c_byte),
            ("BatteryLifePercent", ctypes.c_byte),
            ("Reserved1", ctypes.c_byte),
            ("BatteryLifeTime", ctypes.c_ulong),
            ("BatteryFullLifeTime", ctypes.c_ulong),
        ]

    status = SYSTEM_POWER_STATUS()
    try:
        ok = ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(status))
    except Exception:
        logger.debug("GetSystemPowerStatus failed", exc_info=True)
        return UNKNOWN
    if not ok:
        return UNKNOWN
    # ACLineStatus: 0 = offline (battery), 1 = online (AC), 255 = unknown.
    if status.ACLineStatus == 1:
        return AC
    if status.ACLineStatus == 0:
        return BATTERY
    return UNKNOWN


def _psutil_power_state() -> str:
    try:
        import psutil
    except Exception:
        return UNKNOWN
    try:
        battery = psutil.sensors_battery()
    except Exception:
        logger.debug("psutil sensors_battery failed", exc_info=True)
        return UNKNOWN
    if battery is None:
        return UNKNOWN
    return AC if battery.power_plugged else BATTERY


def get_power_state(
    battery_fn: Optional[Callable[[], object]] = None,
) -> str:
    """Return ``"ac"``, ``"battery"`` or ``"unknown"``.

    ``battery_fn`` is injectable for tests: it must return an object
    with a ``power_plugged`` attribute, or ``None`` for "no battery
    info". When omitted, Windows uses ``GetSystemPowerStatus`` and
    other platforms use ``psutil.sensors_battery()`` when available.
    """
    if battery_fn is not None:
        try:
            battery = battery_fn()
        except Exception:
            logger.debug("injected battery_fn failed", exc_info=True)
            return UNKNOWN
        if battery is None:
            return UNKNOWN
        return AC if getattr(battery, "power_plugged", False) else BATTERY
    if sys.platform == "win32":
        return _windows_power_state()
    return _psutil_power_state()


def matches_power_condition(condition: str, state: str) -> bool:
    """Does ``state`` satisfy the rule's power pre-condition?

    ``condition`` is ``"any"`` / ``"ac"`` / ``"battery"``. An
    unrecognised condition or state never raises: it simply does
    not match (except that ``"any"`` matches every known state,
    including ``"unknown"``).
    """
    if condition not in VALID_CONDITIONS:
        return False
    if condition == "any":
        return state in VALID_STATES
    return condition == state
