"""Table-driven tests for netsh output parsing (WifiMonitor).

Covers the review findings that had zero coverage: exact connected
state matching (a substring test matched "Disconnected"), per-interface
blocks with several adapters, hidden SSIDs, full-width colons and
non-UTF-8 output.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wifi_monitor import WifiMonitor


def block(lines: list[str]) -> str:
    return "\n".join("    " + line for line in lines)


CONNECTED_EN = block(
    [
        "Name                   : Wi-Fi",
        "Description            : Intel(R) Wireless-AC 9560",
        "State                  : connected",
        "SSID                   : Xms",
        "BSSID                  : aa:bb:cc:dd:ee:ff",
        "Network type           : Infrastructure",
    ]
)

DISCONNECTED_EN = block(
    [
        "Name                   : Wi-Fi",
        "Description            : Intel(R) Wireless-AC 9560",
        "State                  : disconnected",
        "Radio status           : Hardware On",
    ]
)

VIRTUAL_DISCONNECTED = block(
    [
        "Name                   : Local Area Connection* 2",
        "Description            : Microsoft Wi-Fi Direct Virtual Adapter",
        "State                  : disconnected",
    ]
)


def netsh(*blocks: str) -> bytes:
    text = "There is 1 interface on the system:\n\n" + "\n\n".join(blocks) + "\n"
    return text.encode("utf-8")


class IsConnectedTests(unittest.TestCase):
    def setUp(self) -> None:
        self.monitor = WifiMonitor()

    def test_exact_states(self) -> None:
        cases = [
            ("connected", True),
            ("Connected", True),
            ("connecting", True),
            ("Disconnected", False),  # substring trap from the review
            ("disconnecting", False),
            ("disconnected", False),
            ("authenticated", False),
            ("已连接", True),
            ("正在连接", True),
            ("", False),
            (None, False),
        ]
        for text, expected in cases:
            with self.subTest(text=text):
                self.assertEqual(self.monitor._is_connected(text), expected)


class ParseNetshOutputTests(unittest.TestCase):
    def setUp(self) -> None:
        self.monitor = WifiMonitor()

    def parse(self, raw: bytes):
        return self.monitor._parse_netsh_output(raw)

    def test_english_connected(self) -> None:
        state = self.parse(netsh(CONNECTED_EN))
        self.assertTrue(state.connected)
        self.assertEqual(state.ssid, "Xms")
        self.assertEqual(state.interface_name, "Wi-Fi")

    def test_english_disconnected(self) -> None:
        state = self.parse(netsh(DISCONNECTED_EN))
        self.assertFalse(state.connected)
        self.assertIsNone(state.ssid)

    def test_disconnected_with_residual_ssid_line(self) -> None:
        # During a disconnect netsh can still print the SSID line; the
        # State field is authoritative.
        residual = block(
            [
                "Name                   : Wi-Fi",
                "State                  : Disconnected",
                "SSID                   : Xms",
            ]
        )
        state = self.parse(netsh(residual))
        self.assertFalse(state.connected)
        self.assertIsNone(state.ssid)

    def test_chinese_connected_gb18030(self) -> None:
        chinese = block(
            [
                "名称                   : WLAN",
                "说明                   : Intel(R) Wireless-AC 9560",
                "状态                   : 已连接",
                "SSID                   : Xms",
                "BSSID                  : aa:bb:cc:dd:ee:ff",
            ]
        )
        raw = ("系统上有 1 个接口:\n\n" + chinese + "\n").encode("gb18030")
        state = self.parse(raw)
        self.assertTrue(state.connected)
        self.assertEqual(state.ssid, "Xms")
        self.assertEqual(state.interface_name, "WLAN")

    def test_multi_interface_connected_block_wins(self) -> None:
        state = self.parse(netsh(VIRTUAL_DISCONNECTED, CONNECTED_EN))
        self.assertTrue(state.connected)
        self.assertEqual(state.ssid, "Xms")
        self.assertEqual(state.interface_name, "Wi-Fi")

    def test_multi_interface_connected_first(self) -> None:
        # The old flat parser took the *last* SSID/State lines and would
        # have reported this as disconnected.
        state = self.parse(netsh(CONNECTED_EN, VIRTUAL_DISCONNECTED))
        self.assertTrue(state.connected)
        self.assertEqual(state.ssid, "Xms")

    def test_multi_interface_none_connected(self) -> None:
        state = self.parse(netsh(VIRTUAL_DISCONNECTED, DISCONNECTED_EN))
        self.assertFalse(state.connected)
        self.assertIsNone(state.ssid)

    def test_hidden_ssid_uses_bssid(self) -> None:
        hidden = block(
            [
                "Name                   : Wi-Fi",
                "State                  : connected",
                "SSID                   : ",
                "BSSID                  : aa:bb:cc:dd:ee:ff",
            ]
        )
        state = self.parse(netsh(hidden))
        self.assertTrue(state.connected)
        self.assertEqual(state.ssid, "aa:bb:cc:dd:ee:ff")

    def test_full_width_colon(self) -> None:
        full_width = block(
            [
                "Name                   ： Wi-Fi",
                "State                  ： connected",
                "SSID                   ： Xms",
            ]
        )
        state = self.parse(netsh(full_width))
        self.assertTrue(state.connected)
        self.assertEqual(state.ssid, "Xms")

    def test_empty_output(self) -> None:
        state = self.parse(b"")
        self.assertFalse(state.connected)
        self.assertIsNone(state.ssid)

    def test_garbage_output(self) -> None:
        state = self.parse(b"not netsh output at all")
        self.assertFalse(state.connected)
        self.assertIsNone(state.ssid)


if __name__ == "__main__":
    unittest.main()
