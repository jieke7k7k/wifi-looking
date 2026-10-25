"""P0-7：跨线程托盘与退出生命周期的可离线验证测试。"""

from __future__ import annotations

import os
import sys
import threading
import time
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from process_watcher import ProcessWatcher
from wifi_monitor import WifiMonitor


class _FastMonitor(WifiMonitor):
    """返回固定状态、不做 netsh 的快速监控器。"""

    def read_state(self):
        from wifi_monitor import WifiState

        return WifiState(connected=False, ssid=None, interface_name=None, state_text=None)


class TestWifiMonitorStopP0(unittest.TestCase):
    def test_stop_joins_monitor_and_dispatcher(self):
        monitor = _FastMonitor(interval_seconds=0.02, debounce_count=1)
        monitor.start()
        # 等监控线程与 dispatcher 都起来。
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            dispatcher = monitor._dispatcher
            if (
                monitor.is_alive()
                and dispatcher is not None
                and dispatcher.is_alive()
            ):
                break
            time.sleep(0.02)
        self.assertTrue(monitor.is_alive())
        self.assertIsNotNone(dispatcher)
        self.assertTrue(dispatcher.is_alive())

        monitor.stop()
        self.assertFalse(monitor.is_alive())
        self.assertFalse(dispatcher.is_alive())


class TestProcessWatcherStopP0(unittest.TestCase):
    def test_stop_returns_true_when_thread_exits(self):
        watcher = ProcessWatcher([], snapshot_fn=lambda: [])
        watcher.start()
        self.assertTrue(watcher.stop(timeout=3.0))
        self.assertIsNone(watcher._thread)

    def test_stop_keeps_thread_reference_when_timeout(self):
        started = threading.Event()

        def slow_snapshot():
            started.set()
            time.sleep(2.0)
            return []

        watcher = ProcessWatcher([], snapshot_fn=slow_snapshot, interval_seconds=0.01)
        watcher.start()
        self.assertTrue(started.wait(timeout=2.0))
        # 用 0 超时强制“线程尚未退出”路径，引用必须被保留。
        self.assertFalse(watcher.stop(timeout=0.0))
        self.assertIsNotNone(watcher._thread)
        # 清理：让线程跑完后再次 stop。
        self.assertTrue(watcher.stop(timeout=5.0))


class TestTrayNotifyBridgeP0(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([])

    def test_notify_routes_through_signal_to_gui_thread(self):
        from unittest import mock

        from tray_app import TrayApp

        class FakeWindow:
            def set_tray_icon(self, icon):
                pass

            def show_from_tray(self):
                pass

            @staticmethod
            def create_icon():
                from PySide6.QtGui import QIcon

                return QIcon()

        fake_app = mock.Mock()
        fake_app.quit = mock.Mock()
        tray = TrayApp(
            app=fake_app,
            config_manager=mock.Mock(),
            monitor=_FastMonitor(),
            main_window=FakeWindow(),
        )
        with mock.patch.object(tray, "show_message") as show_message:
            tray.notify("标题", "正文")
            self.app.processEvents()
            show_message.assert_called_once_with("标题", "正文")


if __name__ == "__main__":
    unittest.main()
