"""集成接线测试（v1.0.0 第三阶段）。

覆盖 main.py / main_window.py / tray_app.py 接线出来的新逻辑：
- 程序启动触发器的进程名收集；
- 退出恢复开关的「动作前快照」与「程序退出恢复」；
- 托盘「暂停所有规则」的 pause_flag；
- 主窗口的程序触发规则 + 退出恢复开关 + 自启开关接线。

全部在 offscreen / mock 下运行，不触网、不碰真实系统。
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import main as app_main
from main import (
    ACTION_TO_RESTORE_PROPERTY,
    _restore_action,
    BannerConfirmation,
    collect_watched_programs,
    ensure_process_watcher_running,
    make_event_handler,
    make_program_exit_handler,
)


def _program_rule(program="wegame.exe", restore=True):
    return {
        "id": "r-program",
        "enabled": True,
        "when": {"event": "on_program_started", "program": program},
        "actions": [{"type": "brightness.set", "value": 80}],
        "cooldown_seconds": 10,
        "restore_on_exit": restore,
    }


class CollectWatchedProgramsTests(unittest.TestCase):
    def test_collects_only_program_triggers_and_dedupes(self):
        rules = [
            {"when": {"event": "on_program_started", "program": "wegame.exe"}},
            {"when": {"event": "on_program_started", "program": "WEGAME.EXE"}},
            {"when": {"event": "on_program_started", "program": "cloudmusic.exe"}},
            {"when": {"event": "on_switch"}},
            {"when": {"event": "on_program_started", "program": "  "}},
        ]
        self.assertEqual(
            collect_watched_programs(rules),
            ["wegame.exe", "cloudmusic.exe"],
        )


class RestoreActionMappingTests(unittest.TestCase):
    def test_known_properties_map_to_actions(self):
        self.assertEqual(_restore_action("brightness", 80), {"type": "brightness.set", "value": 80})
        self.assertEqual(_restore_action("muted", True), {"type": "volume.mute", "muted": True})
        self.assertEqual(
            _restore_action("screen_timeout_minutes", {"minutes": 5, "plugged": False}),
            {"type": "screen.timeout.set", "minutes": 5, "plugged": False},
        )
        self.assertEqual(ACTION_TO_RESTORE_PROPERTY["bluetooth.set"], "bluetooth_enabled")


class EventHandlerRestoreTests(unittest.TestCase):
    def _handler(self, pause_flag=None):
        config_manager = mock.Mock()
        config_manager.get_rules.return_value = []
        rule_engine = mock.Mock()
        rule_engine.evaluate.return_value = [_program_rule()]
        action_executor = mock.Mock()
        action_executor.execute_action.return_value = {"ok": True}
        restore_stack = mock.Mock()
        restore_stack.record = mock.Mock()
        handler = make_event_handler(
            config_manager,
            rule_engine,
            action_executor,
            restore_stack=restore_stack,
            pause_flag=pause_flag,
        )
        return handler, action_executor, restore_stack, rule_engine

    def test_program_rule_snapshots_value_before_action(self):
        handler, action_executor, restore_stack, rule_engine = self._handler()
        with mock.patch("system_actions.read_property_value", return_value=55):
            handler({"type": "on_program_started", "program": "wegame.exe"})
        restore_stack.record.assert_called_once_with("r-program", "brightness", 55)
        action_executor.execute_action.assert_called_once()
        rule_engine.mark_run.assert_called_once_with("r-program")

    def test_pause_flag_suppresses_events(self):
        handler, action_executor, restore_stack, rule_engine = self._handler(
            pause_flag={"paused": True}
        )
        handler({"type": "on_program_started", "program": "wegame.exe"})
        action_executor.execute_action.assert_not_called()
        restore_stack.record.assert_not_called()


class BannerConfirmationTests(unittest.TestCase):
    """P0-6：高危动作确认桥必须接到规则执行链路。"""

    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([])

    def test_make_event_handler_passes_confirmation_to_executor(self):
        config_manager = mock.Mock()
        config_manager.get_rules.return_value = []
        rule_engine = mock.Mock()
        rule_engine.evaluate.return_value = [
            {
                "id": "r-sleep",
                "name": "测试睡眠规则",
                "enabled": True,
                "when": {"event": "on_switch"},
                "actions": [{"type": "sleep"}],
                "cooldown_seconds": 0,
            }
        ]
        action_executor = mock.Mock()
        action_executor.execute_action.return_value = {"ok": True}
        confirmation = mock.Mock(return_value=True)
        handler = make_event_handler(
            config_manager, rule_engine, action_executor, confirmation=confirmation
        )
        handler({"type": "on_switch", "previous_ssid": "a", "current_ssid": "b"})
        # 确认回调被传入，且动作里带上了规则名供横幅展示。
        call = action_executor.execute_action.call_args
        self.assertEqual(call.kwargs["confirmation"], confirmation)
        self.assertEqual(call.args[0]["_rule_name"], "测试睡眠规则")

    def test_finish_sets_result_and_releases_banner(self):
        bridge = BannerConfirmation()
        holder = {"result": None, "done": mock.Mock()}
        bridge._show_banner({"rule_name": "R", "action_name": "睡眠", "holder": holder})
        self.assertIsNotNone(bridge._active_banner)
        bridge._finish(holder, True)
        self.assertTrue(holder["result"])
        holder["done"].set.assert_called_once_with()
        self.assertIsNone(bridge._active_banner)


class ProgramExitHandlerTests(unittest.TestCase):
    def test_exit_releases_and_restores(self):
        config_manager = mock.Mock()
        config_manager.get_rules.return_value = [_program_rule()]
        action_executor = mock.Mock()
        action_executor.execute_action.return_value = {"ok": True}
        restore_stack = mock.Mock()
        restore_stack.pending_for_release.return_value = {"brightness": 40}
        handler = make_program_exit_handler(config_manager, action_executor, restore_stack)
        handler("wegame.exe")
        restore_stack.pending_for_release.assert_called_once_with("r-program")
        action_executor.execute_action.assert_called_once_with(
            {"type": "brightness.set", "value": 40}, {}
        )
        restore_stack.commit_release.assert_called_once_with(
            "r-program", ["brightness"]
        )

    def test_exit_ignores_unmatched_program(self):
        config_manager = mock.Mock()
        config_manager.get_rules.return_value = [_program_rule()]
        action_executor = mock.Mock()
        restore_stack = mock.Mock()
        handler = make_program_exit_handler(config_manager, action_executor, restore_stack)
        handler("cloudmusic.exe")
        restore_stack.pending_for_release.assert_not_called()
        action_executor.execute_action.assert_not_called()

    def test_only_successful_restore_is_committed(self):
        """P0-4：部分恢复失败时，成功项提交、失败项保留。"""
        config_manager = mock.Mock()
        config_manager.get_rules.return_value = [_program_rule()]
        action_executor = mock.Mock()
        action_executor.execute_action.side_effect = [
            {"ok": True},
            {"ok": False, "error": "拒绝访问"},
        ]
        restore_stack = mock.Mock()
        restore_stack.pending_for_release.return_value = {
            "brightness": 40,
            "bluetooth_enabled": True,
        }
        handler = make_program_exit_handler(config_manager, action_executor, restore_stack)
        handler("wegame.exe")
        restore_stack.commit_release.assert_called_once_with(
            "r-program", ["brightness"]
        )


class MainWindowProgramRuleTests(unittest.TestCase):
    """程序触发规则 + 退出恢复开关的 UI 接线（offscreen）。"""

    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([])

    def _make_window(self):
        from config import ConfigManager, DEFAULT_CONFIG
        from main_window import EVENT_LABELS, EVENT_PROGRAM_START, MainWindow

        tmp = self._tmp
        config_path = Path(tmp.name) / "config.json"
        config_path.write_text(
            '{"monitor": {"interval_seconds": 1.0, "debounce_count": 2}, "rules": []}',
            encoding="utf-8",
        )
        manager = ConfigManager(config_path)
        manager.load()

        class StubMonitor:
            current_state = None

        window = MainWindow(manager, StubMonitor())
        return window, manager, EVENT_PROGRAM_START, EVENT_LABELS

    def setUp(self):
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self._tmp.cleanup()

    def test_add_program_rule_with_restore(self):
        from main_window import EVENT_LABELS, EVENT_PROGRAM_START

        window, manager, event_type, labels = self._make_window()
        window.event_combo.setCurrentText(labels[event_type])
        window.program_edit.setText("wegame.exe")
        window.restore_check.setChecked(True)
        window._add_rule()
        rules = manager.get_rules()
        self.assertEqual(len(rules), 1)
        self.assertEqual(rules[0]["when"]["event"], "on_program_started")
        self.assertEqual(rules[0]["when"]["program"], "wegame.exe")
        self.assertTrue(rules[0]["restore_on_exit"])
        window.close()


class ProcessWatcherStartupTests(unittest.TestCase):
    """P0-3：运行中新增程序规则后，进程监听器必须能被启动。"""

    def _fake_watcher(self):
        class FakeWatcher:
            def __init__(self):
                self.names = []
                self.started = 0

            def set_watch_names(self, names):
                self.names = list(names)

            def start(self):
                self.started += 1

        return FakeWatcher()

    def test_starts_when_names_become_non_empty(self):
        watcher = self._fake_watcher()
        # 模拟「启动时无程序规则、运行中新增一条程序规则」的刷新路径。
        ensure_process_watcher_running(watcher, [])
        self.assertEqual(watcher.started, 0)
        ensure_process_watcher_running(watcher, ["wegame.exe"])
        self.assertEqual(watcher.names, ["wegame.exe"])
        self.assertEqual(watcher.started, 1)

    def test_does_not_start_on_empty_names(self):
        watcher = self._fake_watcher()
        ensure_process_watcher_running(watcher, [])
        ensure_process_watcher_running(watcher, [])
        self.assertEqual(watcher.started, 0)

    def test_start_is_idempotent_for_second_refresh(self):
        watcher = self._fake_watcher()
        ensure_process_watcher_running(watcher, ["cloudmusic.exe"])
        ensure_process_watcher_running(watcher, ["cloudmusic.exe"])
        # start() 会再次被调用，但底层 ProcessWatcher.start() 是幂等的；
        # 这里只断言名单一致，调用次数体现“每次刷新都尝试保持运行”。
        self.assertEqual(watcher.names, ["cloudmusic.exe"])
        self.assertEqual(watcher.started, 2)


if __name__ == "__main__":
    unittest.main()
