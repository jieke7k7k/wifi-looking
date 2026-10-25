"""Tests for power_state: condition matching and state reading."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from power_state import get_power_state, matches_power_condition


class PowerConditionTests(unittest.TestCase):
    def test_any_matches_everything_including_unknown(self) -> None:
        for state in ("ac", "battery", "unknown"):
            self.assertTrue(matches_power_condition("any", state))

    def test_ac_and_battery_are_exact(self) -> None:
        self.assertTrue(matches_power_condition("ac", "ac"))
        self.assertFalse(matches_power_condition("ac", "battery"))
        self.assertFalse(matches_power_condition("ac", "unknown"))
        self.assertTrue(matches_power_condition("battery", "battery"))
        self.assertFalse(matches_power_condition("battery", "ac"))
        self.assertFalse(matches_power_condition("battery", "unknown"))

    def test_unknown_condition_or_state_never_raises(self) -> None:
        self.assertFalse(matches_power_condition("solar", "ac"))
        self.assertFalse(matches_power_condition("ac", "solar"))


class GetPowerStateTests(unittest.TestCase):
    def test_plugged_and_unplugged(self) -> None:
        self.assertEqual(
            get_power_state(lambda: SimpleNamespace(power_plugged=True)), "ac"
        )
        self.assertEqual(
            get_power_state(lambda: SimpleNamespace(power_plugged=False)),
            "battery",
        )

    def test_no_battery_info_is_unknown(self) -> None:
        self.assertEqual(get_power_state(lambda: None), "unknown")

    def test_battery_fn_failure_is_unknown_not_exception(self) -> None:
        def boom():
            raise RuntimeError("no sensor")

        self.assertEqual(get_power_state(boom), "unknown")

    def test_default_path_returns_a_known_value(self) -> None:
        # On this Linux VM there is no battery, so "unknown" is
        # expected; the point is a valid value, never an exception.
        self.assertIn(get_power_state(), ("ac", "battery", "unknown"))


if __name__ == "__main__":
    unittest.main()
