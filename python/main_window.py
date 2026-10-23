from __future__ import annotations

import sys
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from PySide6.QtCore import QTimer, Qt, Signal
from PySide6.QtGui import QColor, QIcon, QPixmap, QPainter, QPen
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QSystemTrayIcon,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from clashmi import ClashMiClient, ClashMiError

if TYPE_CHECKING:
    from config import ConfigManager
    from wifi_monitor import WifiMonitor


ACTION_CLOSE_CONNECTIONS = "clashmi.close_connections"
ACTION_DISABLE_TUN = "clashmi.disable_tun"
ACTION_LABELS = {
    ACTION_CLOSE_CONNECTIONS: "断开 ClashMi 连接",
    ACTION_DISABLE_TUN: "关闭 ClashMi TUN",
}
LABEL_TO_ACTION = {v: k for k, v in ACTION_LABELS.items()}

EVENT_SWITCH = "on_switch"
EVENT_DISCONNECT = "on_disconnect"
EVENT_CONNECT = "on_connect"
EVENT_LABELS = {
    EVENT_SWITCH: "切换 Wi-Fi 时",
    EVENT_DISCONNECT: "断开 Wi-Fi 时",
    EVENT_CONNECT: "连上 Wi-Fi 时",
}
LABEL_TO_EVENT = {v: k for k, v in EVENT_LABELS.items()}


def _base_dir() -> Path:
    """Directory that bundled data files (assets/) are read from."""
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return Path(meipass)
    return Path(__file__).resolve().parent


def _parse_ssid_list(text: str) -> List[str]:
    """Parse a comma separated SSID field, keeping order, dropping dupes."""
    result: List[str] = []
    for part in text.replace("，", ",").split(","):
        ssid = part.strip()
        if ssid and ssid not in result:
            result.append(ssid)
    return result


def _ssid_value(ssids: List[str]) -> Any:
    """Store a single SSID as a plain string, several as a list."""
    if len(ssids) == 1:
        return ssids[0]
    return ssids


def _ssids_of(value: Any) -> List[str]:
    if isinstance(value, list):
        return [str(item) for item in value if str(item)]
    if value:
        return [str(value)]
    return []


class MainWindow(QMainWindow):
    _clashmi_auto_result = Signal(str)
    _clashmi_manual_result = Signal(dict)

    def __init__(
        self,
        config_manager: "ConfigManager",
        monitor: "WifiMonitor",
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.config_manager = config_manager
        self.monitor = monitor
        self._updating_table = False
        self._tray_icon = None
        self._rules_by_id: Dict[str, Dict[str, Any]] = {}

        self.setWindowTitle("WiFi-Looking")
        self.setWindowIcon(self.create_icon())
        self.resize(880, 540)

        title_label = QLabel("Wi-Fi 触发规则")
        title_label.setStyleSheet("font-size: 18px; font-weight: 600;")

        self.status_label = QLabel("当前 Wi-Fi：读取中…")
        self.clashmi_label = QLabel("ClashMi：检测中…")

        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(
            ["启用", "触发时机", "来源 SSID", "目标 SSID", "动作", "冷却(秒)"]
        )
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.table.itemSelectionChanged.connect(self._load_selected_rule)
        self.table.itemChanged.connect(self._table_item_changed)

        event_label = QLabel("触发时机")
        self.event_combo = QComboBox()
        self.event_combo.addItems(list(EVENT_LABELS.values()))
        self.event_combo.currentTextChanged.connect(self._on_event_changed)

        source_label = QLabel("来源 SSID")
        self.source_edit = QLineEdit()
        self.source_edit.setPlaceholderText("如：Xms, Xms 5G（多个用逗号分隔）")

        target_label = QLabel("目标 SSID")
        self.target_edit = QLineEdit()
        self.target_edit.setPlaceholderText("如：Other 或 *（任意）")

        action_label = QLabel("动作")
        self.action_combo = QComboBox()
        self.action_combo.addItems(list(ACTION_LABELS.values()))

        cooldown_label = QLabel("冷却(秒)")
        self.cooldown_spin = QSpinBox()
        self.cooldown_spin.setRange(0, 86400)
        self.cooldown_spin.setValue(10)

        add_button = QPushButton("添加规则")
        add_button.clicked.connect(self._add_rule)

        update_button = QPushButton("更新选中规则")
        update_button.clicked.connect(self._update_selected_rule)

        delete_button = QPushButton("删除选中规则")
        delete_button.clicked.connect(self._delete_selected_rule)

        test_button = QPushButton("测试 ClashMi")
        test_button.clicked.connect(self._test_clashmi)

        form_layout = QHBoxLayout()
        form_layout.addWidget(event_label)
        form_layout.addWidget(self.event_combo)
        form_layout.addWidget(source_label)
        form_layout.addWidget(self.source_edit)
        form_layout.addWidget(target_label)
        form_layout.addWidget(self.target_edit)

        form_layout2 = QHBoxLayout()
        form_layout2.addWidget(action_label)
        form_layout2.addWidget(self.action_combo)
        form_layout2.addWidget(cooldown_label)
        form_layout2.addWidget(self.cooldown_spin)
        form_layout2.addWidget(add_button)
        form_layout2.addWidget(update_button)
        form_layout2.addWidget(delete_button)
        form_layout2.addWidget(test_button)
        form_layout2.addStretch(1)

        root_layout = QVBoxLayout()
        root_layout.addWidget(title_label)
        root_layout.addWidget(self.status_label)
        root_layout.addWidget(self.clashmi_label)
        root_layout.addWidget(self.table)
        root_layout.addLayout(form_layout)
        root_layout.addLayout(form_layout2)

        central_widget = QWidget()
        central_widget.setLayout(root_layout)
        self.setCentralWidget(central_widget)

        self._on_event_changed(self.event_combo.currentText())
        self._refresh_table()
        self._refresh_status()
        self.status_timer = QTimer(self)
        self.status_timer.timeout.connect(self._refresh_status)
        self.status_timer.start(1000)

        self._clashmi_auto_result.connect(self.clashmi_label.setText)
        self._clashmi_manual_result.connect(self._on_manual_test_result)
        QTimer.singleShot(600, self._auto_test_clashmi)

    # -- tray integration -------------------------------------------------

    def set_tray_icon(self, tray_icon) -> None:
        self._tray_icon = tray_icon

    def show_from_tray(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def closeEvent(self, event) -> None:
        tray_available = (
            self._tray_icon is not None
            and self._tray_icon.isVisible()
            and QSystemTrayIcon.isSystemTrayAvailable()
        )
        if not tray_available:
            # Without a working tray there is no way to bring the window
            # back; hiding it would strand the app. Close for real.
            event.accept()
            app = QApplication.instance()
            if app is not None:
                app.quit()
            return

        event.ignore()
        self.hide()
        self._tray_icon.showMessage(
            "WiFi-Looking",
            "已最小化到托盘，右键托盘可打开主界面或退出。",
            self.windowIcon(),
            1500,
        )

    # -- status -----------------------------------------------------------

    def _refresh_status(self) -> None:
        state = self.monitor.current_state
        if state is None:
            self.status_label.setText("当前 Wi-Fi：读取中…")
        else:
            ssid = state.ssid or "未连接"
            self.status_label.setText(f"当前 Wi-Fi：{ssid}")

    def _auto_test_clashmi(self) -> None:
        def work() -> None:
            try:
                client = ClashMiClient()
                count = client.connection_count()
                text = (
                    f"ClashMi：已连接，活动连接 {count} 个"
                    f"（{client.settings.base_url}）"
                )
            except ClashMiError as exc:
                text = f"ClashMi：连接失败（{exc}）"
            self._clashmi_auto_result.emit(text)

        threading.Thread(target=work, daemon=True).start()

    def _test_clashmi(self) -> None:
        # The probe can block for seconds (discovery + request timeouts);
        # run it on a worker thread like the automatic startup check so
        # the GUI never freezes or gets flagged "not responding".
        self.clashmi_label.setText("ClashMi：检测中…")

        def work() -> None:
            try:
                client = ClashMiClient()
                count = client.connection_count()
                self._clashmi_manual_result.emit(
                    {"ok": True, "base_url": client.settings.base_url, "count": count}
                )
            except ClashMiError as exc:
                self._clashmi_manual_result.emit({"ok": False, "error": str(exc)})

        threading.Thread(target=work, daemon=True).start()

    def _on_manual_test_result(self, result: Dict[str, Any]) -> None:
        if not result.get("ok"):
            error = str(result.get("error", "未知错误"))
            self.clashmi_label.setText(f"ClashMi：连接失败（{error}）")
            QMessageBox.critical(self, "ClashMi 测试失败", error)
            return

        self.clashmi_label.setText(
            f"ClashMi：连接成功，当前活动连接 {result['count']} 个（{result['base_url']}）"
        )
        QMessageBox.information(
            self,
            "ClashMi 测试成功",
            f"External Controller：{result['base_url']}\n当前活动连接：{result['count']}",
        )

    # -- table ------------------------------------------------------------

    def refresh_rules(self) -> None:
        """Rebuild the table from the config manager (e.g. after reload)."""
        self._refresh_table()

    def _refresh_table(self) -> None:
        self._updating_table = True
        rules = self.config_manager.get_rules()
        self._rules_by_id = {
            str(rule.get("id")): rule for rule in rules if rule.get("id")
        }
        self.table.setRowCount(len(rules))
        for row, rule in enumerate(rules):
            when = rule.get("when", {})
            enabled_item = QTableWidgetItem()
            enabled_item.setFlags(
                Qt.ItemFlag.ItemIsEnabled
                | Qt.ItemFlag.ItemIsSelectable
                | Qt.ItemFlag.ItemIsUserCheckable
            )
            enabled_item.setCheckState(
                Qt.CheckState.Checked if rule.get("enabled", True) else Qt.CheckState.Unchecked
            )
            enabled_item.setData(Qt.ItemDataRole.UserRole, rule.get("id"))
            self.table.setItem(row, 0, enabled_item)
            event_type = str(when.get("event", ""))
            self.table.setItem(row, 1, QTableWidgetItem(EVENT_LABELS.get(event_type, event_type or "-")))
            self.table.setItem(row, 2, QTableWidgetItem(", ".join(_ssids_of(when.get("previous_ssid")))))
            self.table.setItem(row, 3, QTableWidgetItem(", ".join(_ssids_of(when.get("current_ssid")))))
            action_types = [
                str(action.get("type", ""))
                for action in rule.get("actions", [])
                if isinstance(action, dict)
            ]
            action_text = ", ".join(
                ACTION_LABELS.get(action_type, action_type or "-")
                for action_type in action_types
            )
            self.table.setItem(row, 4, QTableWidgetItem(action_text or "-"))
            self.table.setItem(row, 5, QTableWidgetItem(str(rule.get("cooldown_seconds", 0))))
        self._updating_table = False

    def _selected_rule_id(self) -> Optional[str]:
        row = self.table.currentRow()
        if row < 0:
            return None
        item = self.table.item(row, 0)
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def _find_rule(self, rule_id: Optional[str]) -> Optional[Dict[str, Any]]:
        if not rule_id:
            return None
        rule = self._rules_by_id.get(rule_id)
        if rule is not None:
            return rule
        for rule in self.config_manager.get_rules():
            if rule.get("id") == rule_id:
                return rule
        return None

    def _load_selected_rule(self) -> None:
        rule = self._find_rule(self._selected_rule_id())
        if rule is None:
            return
        when = rule.get("when", {})
        event_type = str(when.get("event", EVENT_SWITCH))
        if event_type in EVENT_LABELS:
            self.event_combo.setCurrentText(EVENT_LABELS[event_type])
        self.source_edit.setText(", ".join(_ssids_of(when.get("previous_ssid"))))
        self.target_edit.setText(", ".join(_ssids_of(when.get("current_ssid"))))
        action_type = self._first_action_type(rule.get("actions", []))
        if action_type in ACTION_LABELS:
            self.action_combo.setCurrentText(ACTION_LABELS[action_type])
        try:
            self.cooldown_spin.setValue(int(rule.get("cooldown_seconds", 0) or 0))
        except (TypeError, ValueError):
            self.cooldown_spin.setValue(0)

    def _table_item_changed(self, item: QTableWidgetItem) -> None:
        if self._updating_table or item.column() != 0:
            return
        rule = self._find_rule(item.data(Qt.ItemDataRole.UserRole))
        if rule is not None:
            rule["enabled"] = item.checkState() == Qt.CheckState.Checked
            self._save_rules()

    # -- rule editing -----------------------------------------------------

    def _on_event_changed(self, label: str) -> None:
        event_type = LABEL_TO_EVENT.get(label, EVENT_SWITCH)
        self.source_edit.setEnabled(event_type in {EVENT_SWITCH, EVENT_DISCONNECT})
        self.target_edit.setEnabled(event_type in {EVENT_SWITCH, EVENT_CONNECT})

    def _read_form(self) -> Optional[Dict[str, Any]]:
        """Validate the form and build the rule's ``when``/action parts."""
        event_type = LABEL_TO_EVENT.get(self.event_combo.currentText(), EVENT_SWITCH)
        sources = _parse_ssid_list(self.source_edit.text())
        targets = _parse_ssid_list(self.target_edit.text())

        if event_type in {EVENT_SWITCH, EVENT_DISCONNECT} and not sources:
            QMessageBox.warning(self, "WiFi-Looking", "请填写来源 SSID")
            return None
        if event_type in {EVENT_SWITCH, EVENT_CONNECT} and not targets:
            QMessageBox.warning(self, "WiFi-Looking", "请填写目标 SSID")
            return None

        when: Dict[str, Any] = {"event": event_type}
        if event_type in {EVENT_SWITCH, EVENT_DISCONNECT}:
            when["previous_ssid"] = _ssid_value(sources)
        if event_type in {EVENT_SWITCH, EVENT_CONNECT}:
            when["current_ssid"] = _ssid_value(targets)

        action_type = LABEL_TO_ACTION[self.action_combo.currentText()]
        return {
            "when": when,
            "action_type": action_type,
            "sources": sources,
            "targets": targets,
            "event_type": event_type,
        }

    def _signature(self, when: Dict[str, Any], action_type: str) -> tuple:
        # SSID order must not affect duplicate detection: the same set
        # typed in a different order is the same rule.
        return (
            when.get("event"),
            tuple(sorted(_ssids_of(when.get("previous_ssid")))),
            tuple(sorted(_ssids_of(when.get("current_ssid")))),
            action_type,
        )

    def _rule_signature(self, rule: Dict[str, Any]) -> tuple:
        return self._signature(rule.get("when", {}), self._first_action_type(rule.get("actions", [])))

    def _describe(self, form: Dict[str, Any]) -> str:
        action_text = ACTION_LABELS[form["action_type"]]
        source = ", ".join(form["sources"])
        target = ", ".join(form["targets"])
        if form["event_type"] == EVENT_SWITCH:
            return f"从 {source} 切换到 {target} 时{action_text}"
        if form["event_type"] == EVENT_DISCONNECT:
            return f"从 {source} 断开时{action_text}"
        return f"连上 {target} 时{action_text}"

    def _add_rule(self) -> None:
        form = self._read_form()
        if form is None:
            return

        rules = self.config_manager.get_rules()
        signature = self._signature(form["when"], form["action_type"])
        if any(self._rule_signature(rule) == signature for rule in rules):
            QMessageBox.information(self, "WiFi-Looking", "这条规则已存在")
            return

        rule = {
            "id": self._make_rule_id(form, rules),
            "enabled": True,
            "description": self._describe(form),
            "when": form["when"],
            "actions": [{"type": form["action_type"]}],
            "cooldown_seconds": self.cooldown_spin.value(),
        }
        rules.append(rule)
        self.config_manager.set_rules(rules)
        self._save_rules()
        self._refresh_table()
        self._select_last_row()

    def _update_selected_rule(self) -> None:
        rule = self._find_rule(self._selected_rule_id())
        if rule is None:
            QMessageBox.information(self, "WiFi-Looking", "请先选择要更新的规则")
            return

        form = self._read_form()
        if form is None:
            return

        signature = self._signature(form["when"], form["action_type"])
        for other in self.config_manager.get_rules():
            if other is not rule and self._rule_signature(other) == signature:
                QMessageBox.information(self, "WiFi-Looking", "修改后会与另一条规则重复")
                return

        rule["description"] = self._describe(form)
        rule["when"] = form["when"]
        # The form edits only the first action. Any additional actions
        # (from a hand-edited config) must survive the update instead
        # of being silently dropped and written back to disk.
        existing_actions = [
            action for action in rule.get("actions", []) if isinstance(action, dict)
        ]
        extras = [dict(action) for action in existing_actions[1:]]
        rule["actions"] = [{"type": form["action_type"]}, *extras]
        rule["cooldown_seconds"] = self.cooldown_spin.value()

        self.config_manager.set_rules(self.config_manager.get_rules())
        self._save_rules()
        self._refresh_table()

    def _delete_selected_rule(self) -> None:
        rule_id = self._selected_rule_id()
        if rule_id is None:
            return
        rules = [rule for rule in self.config_manager.get_rules() if rule.get("id") != rule_id]
        self.config_manager.set_rules(rules)
        self._save_rules()
        self._refresh_table()

    def _save_rules(self) -> None:
        try:
            self.config_manager.save()
        except Exception as exc:
            QMessageBox.critical(self, "保存失败", str(exc))

    def _select_last_row(self) -> None:
        row = self.table.rowCount() - 1
        if row >= 0:
            self.table.selectRow(row)

    def _make_rule_id(self, form: Dict[str, Any], existing: List[Dict[str, Any]]) -> str:
        def safe(text: str) -> str:
            cleaned = "".join(ch if ch.isalnum() else "_" for ch in text).strip("_")
            return cleaned or "any"

        event_short = {
            EVENT_SWITCH: "switch",
            EVENT_DISCONNECT: "disconnect",
            EVENT_CONNECT: "connect",
        }[form["event_type"]]
        parts = [event_short]
        if form["sources"]:
            parts.append(safe("_".join(form["sources"])))
        if form["targets"]:
            parts.append(safe("_".join(form["targets"])))
        parts.append(form["action_type"].split(".")[-1])
        base = "_".join(parts)
        taken = {rule.get("id") for rule in existing}
        candidate = base
        counter = 2
        while candidate in taken:
            candidate = f"{base}_{counter}"
            counter += 1
        return candidate

    # -- helpers ----------------------------------------------------------

    @staticmethod
    def _first_action_type(value: Any) -> str:
        if isinstance(value, list) and value:
            return str(value[0].get("type", ""))
        return ""

    @staticmethod
    def create_icon() -> QIcon:
        ico_path = _base_dir() / "assets" / "app.ico"
        if ico_path.exists():
            return QIcon(str(ico_path))

        # Fallback (dev runs without assets): simple line-style fan.
        pixmap = QPixmap(64, 64)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        pen = QPen(QColor(25, 135, 84), 4)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        painter.drawArc(14, 8, 36, 36, 30 * 16, 120 * 16)
        painter.drawArc(22, 16, 20, 20, 30 * 16, 120 * 16)
        painter.setBrush(QColor(25, 135, 84))
        painter.drawEllipse(29, 34, 6, 6)
        painter.end()
        return QIcon(pixmap)
