from __future__ import annotations

import time
from typing import Any, Dict, Iterable, List


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
        now = time.time()

        for rule in rules:
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
        self._last_run[rule_id] = time.time()

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

        return True

    def _matches_ssid_pattern(
        self,
        patterns: object,
        actual_ssid: object,
    ) -> bool:
        if patterns is None:
            return True

        if not isinstance(patterns, list):
            patterns = [patterns]

        for pattern in patterns:
            if pattern == "*":
                return True

            if pattern in {"", None}:
                if actual_ssid in {"", None}:
                    return True
                continue

            if isinstance(pattern, str) and isinstance(actual_ssid, str):
                if pattern.strip().lower() == actual_ssid.strip().lower():
                    return True

        return False
