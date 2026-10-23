"""Event-engine tests for WifiMonitor state transitions.

These lock in the behaviour that the handoff review found broken:
events must report the SSID the laptop is actually leaving as
``previous_ssid``, the first stable reading after startup is a silent
baseline, and short flaps below the debounce threshold emit nothing.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wifi_monitor import WifiMonitor, WifiState


def make_state(ssid: str | None) -> WifiState:
    return WifiState(
        connected=bool(ssid),
        ssid=ssid,
        interface_name="WLAN",
        state_text="connected" if ssid else "disconnected",
    )


def run_sequence(ssids: list[str | None], debounce_count: int = 1) -> list[tuple]:
    events: list[dict] = []
    monitor = WifiMonitor(
        interval_seconds=0.1,
        debounce_count=debounce_count,
        on_event=events.append,
    )
    for ssid in ssids:
        state = make_state(ssid)
        for _ in range(debounce_count):
            monitor._process_state(state)
    return [(e["type"], e["previous_ssid"], e["current_ssid"]) for e in events]


class WifiEventTests(unittest.TestCase):
    def test_startup_baseline_is_silent(self) -> None:
        self.assertEqual(run_sequence(["Xms"]), [])
        self.assertEqual(run_sequence([None]), [])

    def test_switch_reports_ssid_being_left(self) -> None:
        self.assertEqual(
            run_sequence(["Xms", "Other"]),
            [("on_switch", "Xms", "Other")],
        )

    def test_disconnect_after_startup_on_xms(self) -> None:
        self.assertEqual(
            run_sequence(["Xms", None]),
            [("on_disconnect", "Xms", None)],
        )

    def test_connect_then_disconnect(self) -> None:
        self.assertEqual(
            run_sequence([None, "Xms", None]),
            [("on_connect", None, "Xms"), ("on_disconnect", "Xms", None)],
        )

    def test_disconnect_reports_actual_ssid_not_stale_one(self) -> None:
        # Regression: Xms -> Other -> disconnect used to report the
        # disconnect as leaving Xms, falsely matching Xms rules.
        self.assertEqual(
            run_sequence(["Xms", "Other", None]),
            [("on_switch", "Xms", "Other"), ("on_disconnect", "Other", None)],
        )

    def test_switch_back_reports_correct_previous(self) -> None:
        self.assertEqual(
            run_sequence(["Xms", "Other", "Xms"]),
            [("on_switch", "Xms", "Other"), ("on_switch", "Other", "Xms")],
        )

    def test_repeated_stable_state_emits_once(self) -> None:
        events: list[dict] = []
        monitor = WifiMonitor(interval_seconds=0.1, debounce_count=1, on_event=events.append)
        for ssid in [None, "Xms", "Xms", "Xms", None, None]:
            monitor._process_state(make_state(ssid))
        self.assertEqual(
            [(e["type"], e["previous_ssid"], e["current_ssid"]) for e in events],
            [("on_connect", None, "Xms"), ("on_disconnect", "Xms", None)],
        )

    def test_flap_below_debounce_emits_nothing(self) -> None:
        events: list[dict] = []
        monitor = WifiMonitor(interval_seconds=0.1, debounce_count=2, on_event=events.append)
        monitor._process_state(make_state("Xms"))
        monitor._process_state(make_state("Xms"))  # baseline confirmed
        monitor._process_state(make_state("Other"))  # single sample: flap
        monitor._process_state(make_state("Xms"))
        monitor._process_state(make_state("Xms"))
        self.assertEqual(events, [])

    def test_debounce_two_still_reports_switch(self) -> None:
        self.assertEqual(
            run_sequence(["Xms", "Other", None], debounce_count=2),
            [("on_switch", "Xms", "Other"), ("on_disconnect", "Other", None)],
        )


if __name__ == "__main__":
    unittest.main()
