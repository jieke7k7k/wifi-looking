"""Integration tests: monitor events fed through the rule engine must
match the shipped default rules in the scenarios the app exists for.
"""

from __future__ import annotations

import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import DEFAULT_CONFIG
from rule_engine import RuleEngine
from wifi_monitor import WifiMonitor, WifiState


def make_state(ssid: str | None) -> WifiState:
    return WifiState(
        connected=bool(ssid),
        ssid=ssid,
        interface_name="WLAN",
        state_text="connected" if ssid else "disconnected",
    )


def matched_rule_ids(ssids: list[str | None]) -> list[str]:
    matched: list[str] = []
    engine = RuleEngine()
    rules = copy.deepcopy(DEFAULT_CONFIG["rules"])

    def handle(event: dict) -> None:
        for rule in engine.evaluate(event, rules):
            matched.append(rule["id"])

    monitor = WifiMonitor(interval_seconds=0.1, debounce_count=1, on_event=handle)
    for ssid in ssids:
        monitor._process_state(make_state(ssid))
    return matched


class DefaultRuleMatchingTests(unittest.TestCase):
    def test_leaving_xms_by_switch_matches(self) -> None:
        self.assertEqual(
            matched_rule_ids(["Xms", "Other"]),
            ["xms-to-other-close-clash"],
        )

    def test_leaving_xms_5g_by_switch_matches(self) -> None:
        self.assertEqual(
            matched_rule_ids(["Xms 5G", "Other"]),
            ["xms-to-other-close-clash"],
        )

    def test_disconnecting_from_xms_matches(self) -> None:
        self.assertEqual(
            matched_rule_ids(["Xms", None]),
            ["xms-disconnect-close-clash"],
        )

    def test_disconnecting_from_other_matches_nothing(self) -> None:
        # Regression: Xms -> Other -> disconnect must not fire Xms rules.
        self.assertEqual(
            matched_rule_ids(["Xms", "Other", None]),
            ["xms-to-other-close-clash"],
        )

    def test_connecting_to_xms_matches_nothing(self) -> None:
        self.assertEqual(matched_rule_ids([None, "Xms"]), [])

    def test_cooldown_blocks_immediate_refire(self) -> None:
        engine = RuleEngine()
        rules = copy.deepcopy(DEFAULT_CONFIG["rules"])
        event = {
            "type": "on_disconnect",
            "previous_ssid": "Xms",
            "current_ssid": None,
        }
        first = engine.evaluate(event, rules)
        second = engine.evaluate(event, rules)
        self.assertEqual([r["id"] for r in first], ["xms-disconnect-close-clash"])
        self.assertEqual(second, [])


if __name__ == "__main__":
    unittest.main()
