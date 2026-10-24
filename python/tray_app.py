from __future__ import annotations

import threading
from typing import TYPE_CHECKING

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QAction, QIcon
from PySide6.QtWidgets import QMenu, QSystemTrayIcon

from clashmi import ClashMiClient, ClashMiError
from main_window import MainWindow

if TYPE_CHECKING:
    from PySide6.QtWidgets import QApplication
    from config import ConfigManager
    from wifi_monitor import WifiMonitor


class _TraySignals(QObject):
    clashmi_result = Signal(object)


class TrayApp:
    def __init__(
        self,
        app: "QApplication",
        config_manager: "ConfigManager",
        monitor: "WifiMonitor",
        main_window: MainWindow,
    ) -> None:
        self.app = app
        self.config_manager = config_manager
        self.monitor = monitor
        self.main_window = main_window

        self.icon = QSystemTrayIcon(MainWindow.create_icon())
        self.icon.setToolTip("WiFi-Looking")
        self.icon.activated.connect(self._on_activated)

        self.menu = QMenu()
        self.open_action = QAction("打开主界面", self.menu)
        self.open_action.triggered.connect(self.main_window.show_from_tray)
        self.menu.addAction(self.open_action)

        self.status_action = QAction("当前 Wi-Fi 状态", self.menu)
        self.status_action.triggered.connect(self._show_status)
        self.menu.addAction(self.status_action)

        self.test_action = QAction("测试 ClashMi", self.menu)
        self.test_action.triggered.connect(self._test_clashmi)
        self.menu.addAction(self.test_action)

        self.reload_action = QAction("重新加载配置", self.menu)
        self.reload_action.triggered.connect(self._reload_config)
        self.menu.addAction(self.reload_action)

        self.menu.addSeparator()

        self.quit_action = QAction("退出程序", self.menu)
        self.quit_action.triggered.connect(self._quit)
        self.menu.addAction(self.quit_action)

        self.icon.setContextMenu(self.menu)
        self.main_window.set_tray_icon(self.icon)
        self._signals = _TraySignals()
        self._signals.clashmi_result.connect(self._on_test_result)

    def show(self) -> None:
        self.icon.show()

    def hide(self) -> None:
        self.icon.hide()

    def show_message(self, title: str, message: str) -> None:
        self.icon.showMessage(title, message, self.icon.icon(), 1500)

    def _on_activated(self, reason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self.main_window.show_from_tray()

    def _show_status(self) -> None:
        state = self.monitor.current_state
        if state is None:
            message = "当前未读取到 Wi-Fi 状态"
        else:
            message = f"已连接：{state.connected}  SSID：{state.ssid or '-'}"
        self.show_message("WiFi-Looking", message)

    def _test_clashmi(self) -> None:
        # Probe on a worker thread (review bug 21): discovery + the
        # request (with its 401 retry) can block for seconds, and doing
        # it on the GUI thread froze the UI and got it flagged
        # "not responding". The verdict returns via a Qt signal.
        def work() -> None:
            try:
                client = ClashMiClient()
                count = client.connection_count()
                self._signals.clashmi_result.emit(
                    {"ok": True, "count": count, "base_url": client.settings.base_url}
                )
            except ClashMiError as exc:
                self._signals.clashmi_result.emit({"ok": False, "error": str(exc)})

        threading.Thread(target=work, daemon=True).start()

    def _on_test_result(self, result) -> None:
        if not result.get("ok"):
            self.show_message("ClashMi 测试失败", str(result.get("error", "未知错误")))
            return
        self.show_message(
            "ClashMi 测试成功",
            f"活动连接：{result['count']}  地址：{result['base_url']}",
        )

    def _reload_config(self) -> None:
        try:
            self.config_manager.reload()
        except Exception as exc:
            self.show_message("配置加载失败", str(exc))
            return

        # The window must show the reloaded rules (otherwise a later
        # edit would overwrite the file with stale in-memory data), and
        # the monitor picks up hot-updatable timing parameters.
        self.main_window.refresh_rules()
        monitor_config = self.config_manager.get_monitor_config()
        try:
            self.monitor.interval_seconds = float(
                monitor_config.get("interval_seconds", self.monitor.interval_seconds)
            )
            self.monitor.debounce_count = int(
                monitor_config.get("debounce_count", self.monitor.debounce_count)
            )
        except (TypeError, ValueError):
            pass
        self.show_message("WiFi-Looking", "配置已重新加载")

    def _quit(self) -> None:
        self.monitor.stop()
        self.icon.hide()
        self.app.quit()
