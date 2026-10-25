from __future__ import annotations

import time
from typing import Any, Dict, Iterable, List

from ssid_utils import ssid_matches


def _programs_match(expected: str, actual: object) -> bool:
    """程序名是否匹配：忽略大小写，忽略 .exe 后缀有无的差异。"""

    def _norm(value: str) -> str:
        value = value.strip().lower()
        if value.endswith(".exe"):
            value = value[:-4]
        return value

    if not isinstance(actual, str):
        return False
    return _norm(expected) == _norm(actual)


class RuleEngine:
    """Matches Wi-Fi events against configured rules with cooldown."""

    def __init__(self) -> None:
        self._last_run: Dict[str, float] = {}

    def evaluate(
        self,
        event: Dict[str, object],
        rules: Iterable[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Return the rules that match ``event`` and are off cooldown.

        Evaluating never consumes cooldown: callers must report the
        outcome through :meth:`mark_run` once a rule's actions have
        actually succeeded, so a failed attempt (e.g. ClashMi not
        running yet) does not suppress the retry a few seconds later.
        """
        ready: List[Dict[str, Any]] = []
        # Monotonic clock for cooldowns (pre-check item 5): wall-clock
        # time jumps on sleep/wake or a manual clock change, which
        # used to end a cooldown early (double trigger) or stretch it
        # for hours. monotonic() only ever moves forward.
        now = time.monotonic()

        # Snapshot the list (pre-check item 6): the GUI thread edits
        # rules while the monitor thread evaluates them; iterating a
        # copy means an edit can never change the list mid-iteration.
        for rule in list(rules):
            if not self._matches_rule(rule, event):
                continue

            cooldown_seconds = float(rule.get("cooldown_seconds", 0) or 0)
            last_run = self._last_run.get(rule["id"])
            if last_run is not None and now - last_run < cooldown_seconds:
                continue

            ready.append(rule)

        return ready

    def mark_run(self, rule_id: str) -> None:
        """Record that a rule's actions ran successfully (starts cooldown)."""
        self._last_run[rule_id] = time.monotonic()

    def _matches_rule(
        self,
        rule: Dict[str, Any],
        event: Dict[str, object],
    ) -> bool:
        if not rule or rule.get("enabled", True) is False:
            return False

        when = rule.get("when", {})
        if not isinstance(when, dict):
            return False

        expected_event = when.get("event")
        actual_event = event.get("type")
        if expected_event != actual_event:
            return False

        expected_previous = when.get("previous_ssid")
        if not self._matches_ssid_pattern(expected_previous, event.get("previous_ssid")):
            return False

        expected_current = when.get("current_ssid")
        if not self._matches_ssid_pattern(expected_current, event.get("current_ssid")):
            return False

        # v1.0.0: 程序触发条件。规则里写的是 exe 名（如 wegame.exe），
        # 事件里带的也是监听名；比较时忽略大小写与 .exe 后缀差异。
        expected_program = when.get("program")
        if expected_program is not None:
            if not _programs_match(str(expected_program), event.get("program")):
                return False

        # v1.0.0: 电源附加条件。事件自带 power 时优先用事件值（便于
        # 测试注入），否则实时读取当前电源状态；any 表示不限制。
        expected_power = when.get("power")
        if expected_power is not None and str(expected_power) != "any":
            actual_power = event.get("power")
            if actual_power is None:
                try:
                    from power_state import get_power_state

                    actual_power = get_power_state()
                except Exception:
                    actual_power = "unknown"
            if str(actual_power) != str(expected_power):
                return False

        return True

    def _matches_ssid_pattern(
        self,
        patterns: object,
        actual_ssid: object,
    ) -> bool:
        # Shared normalization (ssid_utils) so the engine, the startup
        # guard and duplicate detection can never disagree about
        # whether two SSIDs are "the same" (review bug 6 / opt 2).
        return ssid_matches(patterns, actual_ssid)
