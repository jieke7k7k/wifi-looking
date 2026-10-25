"""P0-1：有序动作编辑器与「立即测试」的可离线验证测试。"""

from __future__ import annotations

import copy
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QMessageBox

from config import ConfigManager, DEFAULT_CONFIG
from main_window import EVENT_LABELS, EVENT_PROGRAM_START, MainWindow


class _StubMonitor:
    current_state = None


class ActionEditorP0Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        config_path = Path(self._tmp.name) / "config.json"
        config = copy.deepcopy(DEFAULT_CONFIG)
        config["rules"] = []
        config_path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
        self.config_manager = ConfigManager(config_path)
        self.config_manager.load()
        self._dialogs = {
            name: getattr(QMessageBox, name)
            for name in ("warning", "information", "critical")
        }
        for name in self._dialogs:
            setattr(QMessageBox, name, staticmethod(lambda *a, **k: None))
        self._orig_auto = MainWindow._auto_test_clashmi
        MainWindow._auto_test_clashmi = lambda self: None
        self.window = MainWindow(self.config_manager, _StubMonitor())

    def tearDown(self):
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()
        MainWindow._auto_test_clashmi = self._orig_auto
        for name, fn in self._dialogs.items():
            setattr(QMessageBox, name, fn)
        self._tmp.cleanup()

    def _program_form(self):
        self.window.event_combo.setCurrentText(EVENT_LABELS[EVENT_PROGRAM_START])
        self.window.program_edit.setText("wegame.exe")
        self.window.restore_check.setChecked(True)

    def test_add_rule_persists_full_ordered_action_sequence(self):
        self._program_form()
        actions = [
            {"type": "bluetooth.set", "enabled": False},
            {"type": "brightness.set", "value": 100},
            {"type": "program.launch", "path": r"C:\Program Files\NetEase\CloudMusic\cloudmusic.exe"},
        ]
        self.window._set_action_list(actions)
        self.window._add_rule()

        rules = self.config_manager.get_rules()
        self.assertEqual(len(rules), 1)
        self.assertEqual(rules[0]["when"]["program"], "wegame.exe")
        self.assertTrue(rules[0]["restore_on_exit"])
        self.assertEqual(rules[0]["actions"], actions)

    def test_action_list_populates_from_selected_rule(self):
        self._program_form()
        self.window._set_action_list([
            {"type": "bluetooth.set", "enabled": False},
            {"type": "brightness.set", "value": 100},
        ])
        self.window._add_rule()
        self.window._set_action_list([])
        self.window.table.clearSelection()
        self.window.table.selectRow(0)
        self.window._load_selected_rule()
        self.assertEqual(
            self.window._current_action_list(),
            [
                {"type": "bluetooth.set", "enabled": False},
                {"type": "brightness.set", "value": 100},
            ],
        )

    def test_move_action_reorders_list(self):
        self.window._set_action_list([
            {"type": "brightness.set", "value": 100},
            {"type": "bluetooth.set", "enabled": False},
        ])
        self.window.action_list.setCurrentRow(1)
        self.window._move_action(-1)
        self.assertEqual(
            self.window._current_action_list(),
            [
                {"type": "bluetooth.set", "enabled": False},
                {"type": "brightness.set", "value": 100},
            ],
        )

    def test_describe_rule_renders_natural_language(self):
        when = {"event": EVENT_PROGRAM_START, "program": "wegame.exe"}
        actions = [
            {"type": "bluetooth.set", "enabled": False},
            {"type": "brightness.set", "value": 100},
            {"type": "program.launch", "path": r"C:\cloudmusic.exe"},
        ]
        description = self.window._describe_rule(when, actions, True)
        self.assertIn("当打开 [wegame.exe] 时", description)
        self.assertIn("关闭蓝牙", description)
        self.assertIn("亮度调至 100%", description)
        self.assertIn("退出后恢复原状", description)

    def test_immediate_test_result_success(self):
        with mock.patch.object(QMessageBox, "information") as info:
            self.window._on_rule_test_result({"ok": True})
            info.assert_called_once()
        self.assertEqual(self.window.status_label.text(), "规则测试完成")

    def test_immediate_test_button_present(self):
        self.assertEqual(self.window.test_rule_button.text(), "▶ 立即测试")


if __name__ == "__main__":
    unittest.main()
