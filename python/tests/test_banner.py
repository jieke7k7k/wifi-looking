"""倒计时横幅测试（offscreen 下可测，无需真实等待）。"""
from __future__ import annotations
import os
import sys
import unittest
from pathlib import Path
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication
from banner import CountdownBanner, banner_text
class TestCountdownBanner(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
    def tearDown(self):
        for widget in list(getattr(self, "_banners", [])):
            widget.close()
        self._banners = []
    def _make(self, rule="测试规则", action="关机", seconds=15):
        banner = CountdownBanner(rule, action, seconds)
        self._banners = getattr(self, "_banners", []) + [banner]
        return banner
    def test_text_format(self):
        banner = self._make()
        self.assertEqual(
            banner.text(),
            "规则 测试规则 已触发，将在 15 秒后执行【关机】，点击【取消】中止",
        )
        self.assertEqual(
            banner_text("R", "睡眠", 5),
            "规则 R 已触发，将在 5 秒后执行【睡眠】，点击【取消】中止",
        )
    def test_default_countdown_is_15(self):
        banner = self._make()
        self.assertEqual(banner.total_seconds, 15)
        self.assertEqual(banner.remaining_seconds(), 15)
    def test_tick_counts_down_and_expires(self):
        banner = self._make(seconds=3)
        expired = []
        banner.expired.connect(lambda: expired.append(1))
        banner.start()
        banner.stop()
        banner.tick()
        self.assertEqual(banner.remaining_seconds(), 2)
        self.assertIn("2 秒后", banner.text())
        banner.tick()
        self.assertEqual(banner.remaining_seconds(), 1)
        banner.tick()
        self.assertEqual(banner.remaining_seconds(), 0)
        self.assertEqual(expired, [1])
        self.assertFalse(banner.is_running())
    def test_cancel_emits_signal_and_stops(self):
        banner = self._make(seconds=10)
        cancelled = []
        banner.cancelled.connect(lambda: cancelled.append(1))
        banner.start()
        self.assertTrue(banner.is_running())
        banner.cancel_button.click()
        self.assertEqual(cancelled, [1])
        self.assertFalse(banner.is_running())
    def test_start_stop(self):
        banner = self._make(seconds=10)
        self.assertFalse(banner.is_running())
        banner.start()
        self.assertTrue(banner.is_running())
        banner.stop()
        self.assertFalse(banner.is_running())
    def test_topmost_without_activating_not_modal(self):
        banner = self._make()
        flags = banner.windowFlags()
        self.assertTrue(bool(flags & Qt.WindowStaysOnTopHint))
        self.assertTrue(banner.testAttribute(Qt.WA_ShowWithoutActivating))
        self.assertFalse(banner.isModal())
    def test_invalid_seconds_rejected(self):
        with self.assertRaises(ValueError):
            CountdownBanner("R", "关机", 0)
if __name__ == "__main__":
    unittest.main()
