from __future__ import annotations

import os
import sys
import threading
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import QObject, Qt, QUrl, Signal
from PySide6.QtGui import QAction, QDesktopServices, QIcon
from PySide6.QtWidgets import QMenu, QSystemTrayIcon

from clashmi import ClashMiClient, ClashMiError
from main_window import MainWindow

if TYPE_CHECKING:
    from PySide6.QtWidgets import QApplication
    from config import ConfigManager
    from wifi_monitor import WifiMonitor


class _TraySignals(QObject):
    clashmi_result = Signal(object)
    notify_requested = Signal(str, str)


class TrayApp:
    def __init__(
        self,
        app: "QApplication",
        config_manager: "ConfigManager",
        monitor: "WifiMonitor",
        main_window: MainWindow,
        on_reload=None,
        pause_flag: "dict | None" = None,
        on_quit=None,
    ) -> None:
        self.app = app
        self.config_manager = config_manager
        self.monitor = monitor
        self.main_window = main_window
        self.on_reload = on_reload
        self.pause_flag = pause_flag
        self.on_quit = on_quit

        self.icon = QSystemTrayIcon(MainWindow.create_icon())
        self.icon.setToolTip("小马联动")
        self.icon.activated.connect(self._on_activated)

        self.menu = QMenu()
        self.open_action = QAction("打开主界面", self.menu)
        self.open_action.triggered.connect(self.main_window.show_from_tray)
        self.menu.addAction(self.open_action)

        # 需求 5.3：暂停所有规则（勾选态 Toggle）。
        self.pause_action = QAction("暂停所有规则", self.menu)
        self.pause_action.setCheckable(True)
        self.pause_action.toggled.connect(self._on_pause_toggled)
        self.menu.addAction(self.pause_action)

        # 需求 5.3：打开日志目录。
        self.logs_action = QAction("打开日志目录", self.menu)
        self.logs_action.triggered.connect(self._open_log_dir)
        self.menu.addAction(self.logs_action)

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
        # P0-7：后台线程只 emit 信号，Qt 负责切回 GUI 线程后再调用
        # QSystemTrayIcon.showMessage()，杜绝跨线程直接操作托盘。
        self._signals.notify_requested.connect(
            self._on_notify_requested, Qt.ConnectionType.QueuedConnection
        )

    def show(self) -> None:
        self.icon.show()

    def hide(self) -> None:
        self.icon.hide()

    def show_message(self, title: str, message: str) -> None:
        self.icon.showMessage(title, message, self.icon.icon(), 1500)

    def notify(self, title: str, message: str) -> None:
        """线程安全的通知入口：任何线程都可调用，内部经信号回 GUI 线程。"""
        self._signals.notify_requested.emit(title, message)

    def _on_notify_requested(self, title: str, message: str) -> None:
        self.show_message(title, message)

    def _on_activated(self, reason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self.main_window.show_from_tray()

    def _on_pause_toggled(self, checked: bool) -> None:
        if self.pause_flag is not None:
            self.pause_flag["paused"] = checked
        self.show_message(
            "小马联动",
            "已暂停所有规则" if checked else "已恢复所有规则",
        )

    def _open_log_dir(self) -> None:
        log_file = self.config_manager.get_logging_config().get("file", "logs/app.log")
        log_path = Path(log_file)
        if not log_path.is_absolute():
            log_path = self.config_manager.config_path.resolve().parent / log_path
        log_dir = log_path.parent
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        try:
            if sys.platform == "win32":
                os.startfile(str(log_dir))  # type: ignore[attr-defined]
            else:
                QDesktopServices.openUrl(QUrl.fromLocalFile(str(log_dir)))
        except Exception as exc:
            self.show_message("小马联动", f"无法打开日志目录：{exc}")

    def _show_status(self) -> None:
        state = self.monitor.current_state
        if state is None:
            message = "当前未读取到 Wi-Fi 状态"
        else:
            message = f"已连接：{state.connected}  SSID：{state.ssid or '-'}"
        self.show_message("小马联动", message)

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
            self.monitor.confirm_delay_seconds = float(
                monitor_config.get("confirm_delay_seconds", self.monitor.confirm_delay_seconds)
            )
        except (TypeError, ValueError):
            pass
        if self.on_reload is not None:
            try:
                self.on_reload()
            except Exception:
                pass
        self.show_message("小马联动", "配置已重新加载")

    def _quit(self) -> None:
        # P0-7：先停止回调与后台线程、等线程退出，再隐藏托盘并退出事件循环。
        if self.on_quit is not None:
            try:
                self.on_quit()
            except Exception:
                pass
        self.icon.hide()
        self.app.quit()
