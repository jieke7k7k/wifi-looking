"""Headless tests for the MainWindow rule editor.

Runs with QT_QPA_PLATFORM=offscreen; needs PySide6 installed. Skipped
automatically when PySide6 is unavailable (e.g. plain CI lint env).
"""

from __future__ import annotations

import copy
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication, QMessageBox
except ImportError:  # pragma: no cover
    QApplication = None

if QApplication is not None:
    from config import DEFAULT_CONFIG, ConfigManager
    from main_window import (
        EVENT_LABELS,
        EVENT_CONNECT,
        EVENT_DISCONNECT,
        EVENT_SWITCH,
        MainWindow,
    )


class _StubMonitor:
    current_state = None


@unittest.skipIf(QApplication is None, "PySide6 not installed")
class MainWindowRuleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        config_path = Path(self._tmp.name) / "config.json"
        config_path.write_text(
            json.dumps(copy.deepcopy(DEFAULT_CONFIG), ensure_ascii=False),
            encoding="utf-8",
        )
        self.config_manager = ConfigManager(config_path)
        self.config_manager.load()

        self._orig_dialogs = {
            name: getattr(QMessageBox, name)
            for name in ("warning", "information", "critical")
        }
        for name in self._orig_dialogs:
            setattr(QMessageBox, name, staticmethod(lambda *a, **k: None))

        self._orig_auto = MainWindow._auto_test_clashmi
        MainWindow._auto_test_clashmi = lambda self: None
        self.window = MainWindow(self.config_manager, _StubMonitor())

    def tearDown(self) -> None:
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()
        MainWindow._auto_test_clashmi = self._orig_auto
        for name, fn in self._orig_dialogs.items():
            setattr(QMessageBox, name, fn)
        self._tmp.cleanup()

    # -- helpers ----------------------------------------------------------

    def rules(self):
        return self.config_manager.get_rules()

    def set_form(self, event: str, source: str = "", target: str = "") -> None:
        self.window.event_combo.setCurrentText(EVENT_LABELS[event])
        self.window.source_edit.setText(source)
        self.window.target_edit.setText(target)

    # -- tests ------------------------------------------------------------

    def test_default_rules_shown_with_full_ssid_lists(self) -> None:
        self.assertEqual(self.window.table.rowCount(), 2)
        self.assertEqual(self.window.table.item(0, 1).text(), EVENT_LABELS[EVENT_DISCONNECT])
        self.assertEqual(self.window.table.item(0, 2).text(), "Xms, Xms 5G")
        self.assertEqual(self.window.table.item(1, 1).text(), EVENT_LABELS[EVENT_SWITCH])
        self.assertEqual(self.window.table.item(1, 3).text(), "*")

    def test_add_switch_rule_with_multiple_ssids(self) -> None:
        self.set_form(EVENT_SWITCH, "Home, Home 5G", "*")
        self.window._add_rule()
        self.assertEqual(len(self.rules()), 3)
        rule = self.rules()[-1]
        self.assertEqual(rule["when"]["event"], EVENT_SWITCH)
        self.assertEqual(rule["when"]["previous_ssid"], ["Home", "Home 5G"])
        self.assertEqual(rule["when"]["current_ssid"], "*")

    def test_add_disconnect_rule_has_no_target(self) -> None:
        self.set_form(EVENT_DISCONNECT, "Dorm")
        self.window._add_rule()
        rule = self.rules()[-1]
        self.assertEqual(rule["when"]["event"], EVENT_DISCONNECT)
        self.assertEqual(rule["when"]["previous_ssid"], "Dorm")
        self.assertNotIn("current_ssid", rule["when"])

    def test_add_connect_rule_has_no_source(self) -> None:
        self.set_form(EVENT_CONNECT, "", "Cafe")
        self.window._add_rule()
        rule = self.rules()[-1]
        self.assertEqual(rule["when"]["event"], EVENT_CONNECT)
        self.assertEqual(rule["when"]["current_ssid"], "Cafe")
        self.assertNotIn("previous_ssid", rule["when"])

    def test_update_preserves_ssid_list(self) -> None:
        self.window.table.selectRow(0)
        self.assertEqual(self.window.source_edit.text(), "Xms, Xms 5G")
        self.window.cooldown_spin.setValue(30)
        self.window._update_selected_rule()
        rule = self.rules()[0]
        self.assertEqual(rule["when"]["previous_ssid"], ["Xms", "Xms 5G"])
        self.assertEqual(rule["cooldown_seconds"], 30)

    def test_update_event_type_removes_stale_keys(self) -> None:
        self.window.table.selectRow(0)  # disconnect rule
        self.set_form(EVENT_CONNECT, "", "Xms")
        self.window._update_selected_rule()
        rule = self.rules()[0]
        self.assertEqual(rule["when"]["event"], EVENT_CONNECT)
        self.assertEqual(rule["when"]["current_ssid"], "Xms")
        self.assertNotIn("previous_ssid", rule["when"])

    def test_duplicate_rule_not_added(self) -> None:
        self.set_form(EVENT_SWITCH, "Xms, Xms 5G", "*")
        self.window._add_rule()
        self.assertEqual(len(self.rules()), 2)

    def test_rules_persist_to_disk(self) -> None:
        self.set_form(EVENT_DISCONNECT, "Library")
        self.window._add_rule()
        reloaded = ConfigManager(self.config_manager.config_path)
        reloaded.load()
        ids = [rule["id"] for rule in reloaded.get_rules()]
        self.assertEqual(len(ids), 3)
        self.assertTrue(any("Library" in rule_id for rule_id in ids))

    def test_update_preserves_extra_actions(self) -> None:
        # A hand-edited multi-action rule must not lose its extra
        # actions when its first action is edited in the GUI.
        self.rules()[0]["actions"] = [
            {"type": "clashmi.close_connections"},
            {"type": "clashmi.disable_tun"},
        ]
        self.window.table.selectRow(0)
        self.window.cooldown_spin.setValue(45)
        self.window._update_selected_rule()
        rule = self.rules()[0]
        self.assertEqual(
            [action["type"] for action in rule["actions"]],
            ["clashmi.close_connections", "clashmi.disable_tun"],
        )
        self.assertEqual(rule["cooldown_seconds"], 45)

    def test_action_column_lists_all_actions(self) -> None:
        self.rules()[0]["actions"] = [
            {"type": "clashmi.close_connections"},
            {"type": "clashmi.disable_tun"},
        ]
        self.window.refresh_rules()
        self.assertIn("关闭 ClashMi TUN", self.window.table.item(0, 4).text())

    def test_duplicate_detection_ignores_ssid_order(self) -> None:
        self.set_form(EVENT_SWITCH, "Xms 5G, Xms", "*")
        self.window._add_rule()
        self.assertEqual(len(self.rules()), 2)

    def test_uncheck_disables_rule_and_saves(self) -> None:
        item = self.window.table.item(0, 0)
        item.setCheckState(Qt.CheckState.Unchecked)
        self.assertFalse(self.rules()[0]["enabled"])
        reloaded = ConfigManager(self.config_manager.config_path)
        reloaded.load()
        self.assertFalse(reloaded.get_rules()[0]["enabled"])


if __name__ == "__main__":
    unittest.main()
