from __future__ import annotations

import logging
import sys
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from PySide6.QtCore import QTimer, Qt, Signal
from PySide6.QtGui import QColor, QIcon, QPixmap, QPainter, QPen
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
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
from ssid_utils import normalize_ssid, normalized_ssid_set
from system_actions import ACTION_TYPES as SYSTEM_ACTION_TYPES
from wifi_monitor import collect_ssid_candidates

if TYPE_CHECKING:
    from config import ConfigManager
    from wifi_monitor import WifiMonitor


def _self_exe_path() -> str:
    """任务计划自启用的可执行路径（打包后为 exe 本身，开发为解释器）。"""
    if getattr(sys, "frozen", False):
        return sys.executable
    return sys.executable


logger = logging.getLogger(__name__)


ACTION_CLOSE_CONNECTIONS = "clashmi.close_connections"
ACTION_DISABLE_TUN = "clashmi.disable_tun"
ACTION_KILL_PROCESS = "clashmi.kill_process"
ACTION_LABELS = {
    ACTION_CLOSE_CONNECTIONS: "断开 ClashMi 连接",
    ACTION_DISABLE_TUN: "关闭 ClashMi TUN",
    ACTION_KILL_PROCESS: "关闭 ClashMi（结束进程）",
}
LABEL_TO_ACTION = {v: k for k, v in ACTION_LABELS.items()}

# P0-1：动作编辑器需要覆盖「全量动作清单」，而不仅是 3 个 ClashMi 动作。
_ACTION_TYPE_LABELS = {
    ACTION_CLOSE_CONNECTIONS: "断开 ClashMi 连接",
    ACTION_DISABLE_TUN: "关闭 ClashMi TUN",
    ACTION_KILL_PROCESS: "关闭 ClashMi（结束进程）",
    "wifi.set": "Wi-Fi 开关",
    "bluetooth.set": "蓝牙开关",
    "brightness.set": "屏幕亮度",
    "dark_mode.set": "深色/浅色模式",
    "wallpaper.set": "更换桌面壁纸",
    "volume.set": "系统音量",
    "volume.mute": "静音开关",
    "audio.device.set": "默认音频设备",
    "power.plan.set": "切换电源计划",
    "screen.timeout.set": "屏幕超时",
    "dnd.set": "勿扰模式",
    "lock_screen": "立即锁屏",
    "sleep": "睡眠",
    "shutdown": "关机",
    "restart": "重启",
    "program.launch": "启动程序",
    "program.close": "关闭程序",
    "process.kill": "结束进程",
}
_LABEL_TO_ACTION_TYPE = {v: k for k, v in _ACTION_TYPE_LABELS.items()}

# 动作参数规格：用于动作编辑器按类型渲染参数表单。
_ACTION_PARAM_SPECS: Dict[str, List[Dict[str, Any]]] = {
    "wifi.set": [{"key": "enabled", "label": "开启", "kind": "bool", "default": True}],
    "bluetooth.set": [{"key": "enabled", "label": "开启", "kind": "bool", "default": False}],
    "brightness.set": [{"key": "value", "label": "亮度(0-100)", "kind": "int", "min": 0, "max": 100, "default": 100}],
    "dark_mode.set": [{"key": "mode", "label": "模式", "kind": "choice", "choices": ["dark", "light"], "default": "dark"}],
    "wallpaper.set": [{"key": "path", "label": "图片绝对路径", "kind": "text", "default": ""}],
    "volume.set": [{"key": "value", "label": "音量(0-100)", "kind": "int", "min": 0, "max": 100, "default": 50}],
    "volume.mute": [{"key": "muted", "label": "静音", "kind": "bool", "default": True}],
    "audio.device.set": [{"key": "device", "label": "设备名", "kind": "text", "default": ""}],
    "power.plan.set": [{"key": "plan", "label": "计划名或 GUID", "kind": "text", "default": ""}],
    "screen.timeout.set": [
        {"key": "minutes", "label": "分钟", "kind": "int", "min": 0, "max": 1440, "default": 10},
        {"key": "plugged", "label": "插电时", "kind": "bool", "default": True},
    ],
    "dnd.set": [{"key": "enabled", "label": "开启", "kind": "bool", "default": True}],
    "program.launch": [
        {"key": "path", "label": "程序路径", "kind": "text", "default": ""},
        {"key": "args", "label": "参数(逗号分隔)", "kind": "text", "default": ""},
    ],
    "program.close": [
        {"key": "name", "label": "进程名", "kind": "text", "default": ""},
        {"key": "graceful", "label": "优雅关闭", "kind": "bool", "default": True},
    ],
    "process.kill": [
        {"key": "name", "label": "进程名", "kind": "text", "default": ""},
        {"key": "path_contains", "label": "路径包含", "kind": "text", "default": ""},
    ],
}

EVENT_SWITCH = "on_switch"
EVENT_DISCONNECT = "on_disconnect"
EVENT_CONNECT = "on_connect"
EVENT_PROGRAM_START = "on_program_started"
EVENT_LABELS = {
    EVENT_SWITCH: "切换 Wi-Fi 时",
    EVENT_DISCONNECT: "断开 Wi-Fi 时",
    EVENT_CONNECT: "连上 Wi-Fi 时",
    EVENT_PROGRAM_START: "打开程序时",
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


class ActionEditorDialog(QDialog):
    """单个动作的参数编辑对话框（P0-1：有序动作序列的一部分）。"""

    def __init__(self, parent=None, action: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("动作")
        self._param_widgets: Dict[str, QWidget] = {}

        root = QVBoxLayout(self)
        type_row = QHBoxLayout()
        type_row.addWidget(QLabel("动作类型"))
        self.type_combo = QComboBox()
        ordered_types = list(ACTION_LABELS.keys()) + [
            t for t in SYSTEM_ACTION_TYPES if t not in ACTION_LABELS
        ]
        for action_type in ordered_types:
            self.type_combo.addItem(
                _ACTION_TYPE_LABELS.get(action_type, action_type), action_type
            )
        self.type_combo.currentIndexChanged.connect(self._rebuild_params)
        type_row.addWidget(self.type_combo, 1)
        root.addLayout(type_row)

        self._params_box = QVBoxLayout()
        root.addLayout(self._params_box)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

        if action is not None:
            index = self.type_combo.findData(action.get("type"))
            if index >= 0:
                self.type_combo.setCurrentIndex(index)
        self._rebuild_params()
        if action is not None:
            self._load_action(action)

    def _clear_params(self) -> None:
        while self._params_box.count():
            item = self._params_box.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self._param_widgets = {}

    def _rebuild_params(self, *args) -> None:
        del args
        self._clear_params()
        action_type = self.type_combo.currentData()
        for spec in _ACTION_PARAM_SPECS.get(action_type, []):
            label = QLabel(spec["label"])
            kind = spec["kind"]
            if kind == "bool":
                widget = QCheckBox("是")
                widget.setChecked(bool(spec.get("default")))
            elif kind == "int":
                widget = QSpinBox()
                widget.setRange(int(spec.get("min", 0)), int(spec.get("max", 100000)))
                widget.setValue(int(spec.get("default", 0)))
            elif kind == "choice":
                widget = QComboBox()
                widget.addItems(spec.get("choices", []))
                default = spec.get("default")
                if default in spec.get("choices", []):
                    widget.setCurrentText(str(default))
            else:
                widget = QLineEdit()
                widget.setText(str(spec.get("default", "")))
            row = QHBoxLayout()
            row.addWidget(label)
            row.addWidget(widget, 1)
            self._params_box.addLayout(row)
            self._param_widgets[spec["key"]] = widget

    def _load_action(self, action: Dict[str, Any]) -> None:
        for key, widget in self._param_widgets.items():
            value = action.get(key)
            if isinstance(widget, QCheckBox):
                widget.setChecked(bool(value))
            elif isinstance(widget, QSpinBox):
                try:
                    widget.setValue(int(value))
                except (TypeError, ValueError):
                    pass
            elif isinstance(widget, QComboBox):
                if value in [widget.itemText(i) for i in range(widget.count())]:
                    widget.setCurrentText(str(value))
            elif isinstance(widget, QLineEdit):
                widget.setText("" if value is None else str(value))

    def build_action(self) -> Dict[str, Any]:
        action: Dict[str, Any] = {"type": self.type_combo.currentData()}
        for key, widget in self._param_widgets.items():
            if isinstance(widget, QCheckBox):
                action[key] = widget.isChecked()
            elif isinstance(widget, QSpinBox):
                action[key] = widget.value()
            elif isinstance(widget, QComboBox):
                action[key] = widget.currentText()
            elif isinstance(widget, QLineEdit):
                text = widget.text().strip()
                if key == "args":
                    action[key] = [part.strip() for part in text.split(",") if part.strip()]
                elif text:
                    action[key] = text
        return action


class SsidComboBox(QComboBox):
    """Editable SSID combo with a QLineEdit-style text() / setText().

    Candidates (current / scanned / saved networks) are offered in the
    dropdown so users pick instead of typing; anything can still be
    typed manually. The text accessors keep the rule form code — and
    its tests — identical to the old plain line edits.
    """

    def text(self) -> str:
        return self.currentText()

    def setText(self, text: str) -> None:
        self.setEditText(text)


def ssid_covered_by_rules(ssid: Optional[str], rules: List[Dict[str, Any]]) -> bool:
    """True if some enabled rule lists ``ssid`` (or ``*``) as a source.

    Comparison uses the shared SSID normalization (review bug 6):
    the guard must agree with the rule engine, which matches
    case-insensitively and ignores surrounding whitespace.
    """
    if not ssid:
        return False
    wanted = normalize_ssid(ssid)
    for rule in rules:
        if not isinstance(rule, dict) or not rule.get("enabled", True):
            continue
        when = rule.get("when", {})
        if not isinstance(when, dict):
            continue
        sources = _ssids_of(when.get("previous_ssid"))
        if "*" in sources:
            return True
        if wanted and wanted in normalized_ssid_set(when.get("previous_ssid")):
            return True
    return False


def startup_guard_should_warn(
    ssid: Optional[str],
    rules: List[Dict[str, Any]],
    clashmi_active: bool,
) -> bool:
    """Startup guard decision: warn only when connected to a network no
    enabled rule covers while ClashMi is on. Not connected → silent;
    ClashMi off → nothing to warn about."""
    if not ssid or not clashmi_active:
        return False
    return not ssid_covered_by_rules(ssid, rules)


class MainWindow(QMainWindow):
    _clashmi_auto_result = Signal(str)
    _clashmi_manual_result = Signal(dict)
    _ssid_candidates_ready = Signal(list)
    _startup_guard_result = Signal(object)
    _rule_test_result = Signal(dict)

    def __init__(
        self,
        config_manager: "ConfigManager",
        monitor: "WifiMonitor",
        parent=None,
        on_rules_changed=None,
    ) -> None:
        super().__init__(parent)
        self.config_manager = config_manager
        self.monitor = monitor
        self._on_rules_changed = on_rules_changed
        self._updating_table = False
        self._tray_icon = None
        self._rules_by_id: Dict[str, Dict[str, Any]] = {}
        self._startup_guard_started = False
        self._candidates_refreshed_for_state = False

        from config import APP_VERSION

        self.setWindowTitle(f"小马联动 v{APP_VERSION}")
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
        self.source_edit = SsidComboBox()
        self.source_edit.setEditable(True)
        self.source_edit.lineEdit().setPlaceholderText("下拉选择或手动输入（多个用逗号分隔）")

        target_label = QLabel("目标 SSID")
        self.target_edit = SsidComboBox()
        self.target_edit.setEditable(True)
        self.target_edit.lineEdit().setPlaceholderText("下拉选择，或 *（任意）")

        program_label = QLabel("程序名")
        self.program_edit = QLineEdit()
        self.program_edit.setPlaceholderText("例如 wegame.exe / cloudmusic.exe")

        action_label = QLabel("动作")
        self.action_combo = QComboBox()
        self.action_combo.addItems(list(ACTION_LABELS.values()))
        # New rules default to turning ClashMi off (v0.3.0): that is
        # the action the one-click button uses too.
        self.action_combo.setCurrentText(ACTION_LABELS[ACTION_KILL_PROCESS])

        # P0-1：有序动作序列编辑器。
        self.action_list = QListWidget()
        self.action_list.setMinimumHeight(72)
        self.action_list.itemDoubleClicked.connect(self._edit_action_item)

        self.add_action_button = QPushButton("添加动作")
        self.add_action_button.clicked.connect(self._add_action_to_list)
        self.remove_action_button = QPushButton("删除动作")
        self.remove_action_button.clicked.connect(self._remove_selected_action)
        self.up_action_button = QPushButton("上移")
        self.up_action_button.clicked.connect(lambda: self._move_action(-1))
        self.down_action_button = QPushButton("下移")
        self.down_action_button.clicked.connect(lambda: self._move_action(1))

        action_buttons = QHBoxLayout()
        action_buttons.addWidget(self.add_action_button)
        action_buttons.addWidget(self.remove_action_button)
        action_buttons.addWidget(self.up_action_button)
        action_buttons.addWidget(self.down_action_button)
        action_buttons.addStretch(1)

        self.test_rule_button = QPushButton("▶ 立即测试")
        self.test_rule_button.clicked.connect(self._test_selected_rule)

        cooldown_label = QLabel("冷却(秒)")
        self.cooldown_spin = QSpinBox()
        self.cooldown_spin.setRange(0, 86400)
        self.cooldown_spin.setValue(10)

        self.restore_check = QCheckBox("退出时恢复原设置")
        self.restore_check.setToolTip("程序退出后，自动还原被本规则修改的系统设置")

        self.autostart_check = QCheckBox("开机自动启动（最高权限，静默进入托盘）")
        self.autostart_check.setToolTip("通过 Windows 任务计划程序实现，避免开机弹出 UAC 确认框")
        self.autostart_check.toggled.connect(self._on_autostart_toggled)

        add_button = QPushButton("添加规则")
        add_button.clicked.connect(self._add_rule)

        quick_add_button = QPushButton("一键添加当前 Wi-Fi")
        quick_add_button.clicked.connect(self._quick_add_current_wifi)

        refresh_candidates_button = QPushButton("刷新 Wi-Fi 列表")
        refresh_candidates_button.clicked.connect(self._refresh_ssid_candidates)

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
        form_layout.addWidget(program_label)
        form_layout.addWidget(self.program_edit)

        form_layout2 = QHBoxLayout()
        form_layout2.addWidget(action_label)
        form_layout2.addWidget(self.action_combo)
        form_layout2.addWidget(cooldown_label)
        form_layout2.addWidget(self.cooldown_spin)
        form_layout2.addWidget(self.restore_check)
        form_layout2.addWidget(add_button)
        form_layout2.addWidget(update_button)
        form_layout2.addWidget(delete_button)
        form_layout2.addWidget(test_button)
        form_layout2.addWidget(quick_add_button)
        form_layout2.addWidget(refresh_candidates_button)
        form_layout2.addStretch(1)

        root_layout = QVBoxLayout()
        root_layout.addWidget(title_label)
        root_layout.addWidget(self.status_label)
        root_layout.addWidget(self.clashmi_label)
        root_layout.addWidget(self.table)
        root_layout.addLayout(form_layout)
        root_layout.addLayout(form_layout2)
        root_layout.addLayout(action_buttons)
        root_layout.addWidget(self.action_list)
        root_layout.addWidget(self.test_rule_button)
        root_layout.addWidget(self.autostart_check)

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
        self._ssid_candidates_ready.connect(self._apply_ssid_candidates)
        self._startup_guard_result.connect(self._on_startup_guard_result)
        self._rule_test_result.connect(self._on_rule_test_result)

        # P0-6/P0-1：GUI 线程持有的高危动作确认桥，供「立即测试」复用。
        try:
            from main import BannerConfirmation

            self._confirmation_bridge = BannerConfirmation(parent=self)
        except Exception:
            self._confirmation_bridge = None
        # P0-7：用窗口持有的单发 QTimer 代替无上下文的 QTimer.singleShot，
        # 窗口销毁时定时器随同一线程生命周期停止，避免跨线程 killTimer。
        self._auto_test_timer = QTimer(self)
        self._auto_test_timer.setSingleShot(True)
        self._auto_test_timer.timeout.connect(self._auto_test_clashmi)
        self._auto_test_timer.start(600)
        self._refresh_ssid_candidates()
        self._refresh_autostart_state()

    # -- autostart (Task Scheduler) ---------------------------------------

    def _refresh_autostart_state(self) -> None:
        """回填自启开关状态；非 Windows 下禁用开关。"""
        try:
            from autostart import is_registered
        except Exception:
            self.autostart_check.setEnabled(False)
            return
        if sys.platform != "win32":
            self.autostart_check.setEnabled(False)
            return
        try:
            registered = is_registered()
        except Exception:
            registered = None
        self.autostart_check.setChecked(bool(registered))

    def _on_autostart_toggled(self, checked: bool) -> None:
        from autostart import register, unregister

        exe_path = _self_exe_path()
        result = register(exe_path) if checked else unregister()
        if result.get("ok"):
            self.autostart_check.setToolTip(
                "已开启开机自动启动" if checked else "已关闭开机自动启动"
            )
            return
        if result.get("status") == "unsupported":
            QMessageBox.information(self, "小马联动", "开机自启仅支持 Windows 系统")
            self.autostart_check.setChecked(False)
            return
        QMessageBox.warning(
            self,
            "小马联动",
            "开机自启设置失败：" + str(result.get("error", "未知错误")),
        )
        self.autostart_check.setChecked(not checked)

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
            "小马联动",
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
            if not self._candidates_refreshed_for_state:
                # The candidate collection at startup usually races
                # the monitor's first read, so the current network —
                # the most important candidate — was missing (review
                # bug 9). Re-collect once the first state is known.
                self._candidates_refreshed_for_state = True
                self._refresh_ssid_candidates()

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

    # -- SSID candidates ----------------------------------------------------

    def _refresh_ssid_candidates(self) -> None:
        """Load dropdown candidates (current / scanned / saved SSIDs).

        netsh calls run on a worker thread and may take a moment; any
        failure degrades to an empty dropdown — manual input always
        works, so the editor never blocks on this.
        """

        def work() -> None:
            current = self._current_ssid()
            try:
                items = collect_ssid_candidates(current)
            except Exception:
                logger.debug("Collecting SSID candidates failed", exc_info=True)
                items = []
            self._ssid_candidates_ready.emit(items)

        threading.Thread(target=work, daemon=True).start()

    def _apply_ssid_candidates(self, items: List[str]) -> None:
        cleaned = [str(item) for item in items]
        for combo, extra in ((self.source_edit, []), (self.target_edit, ["*"])):
            # "*" is a fixed candidate for the target field (review
            # optimization 4): users should pick it, not type it and
            # risk a full-width ＊ that never matches.
            values = extra + [item for item in cleaned if item not in extra]
            text = combo.currentText()
            combo.clear()
            combo.addItems(values)
            combo.setEditText(text)

    def _current_ssid(self) -> Optional[str]:
        state = getattr(self.monitor, "current_state", None)
        if state is not None and getattr(state, "connected", False):
            ssid = getattr(state, "ssid", None)
            if ssid:
                return str(ssid)
        return None

    # -- one-click rules ----------------------------------------------------

    def _quick_add_current_wifi(self) -> None:
        ssid = self._current_ssid()
        if not ssid:
            # Distinguish "state not read yet" from "really not
            # connected" (review bug 22) — the old single message sent
            # users off to debug a healthy network.
            if getattr(self.monitor, "current_state", None) is None:
                message = "Wi-Fi 状态尚未读取完成，请稍等几秒再试"
            else:
                message = "当前未连接 Wi-Fi，无法一键添加规则"
            QMessageBox.information(self, "小马联动", message)
            return

        added, skipped = self.add_rules_for_current_ssid(
            ssid, cooldown=self.cooldown_spin.value()
        )
        if added and skipped:
            message = f"已为「{ssid}」添加 {added} 条规则，另有 {skipped} 条已存在、已跳过"
        elif added:
            message = f"已为「{ssid}」添加 {added} 条规则（断开时 + 切换走时关闭 ClashMi）"
        else:
            message = f"「{ssid}」的断开/切换规则已存在，未重复添加"
        QMessageBox.information(self, "小马联动", message)

    def add_rules_for_current_ssid(
        self,
        ssid: str,
        cooldown: Optional[int] = None,
        action_type: str = ACTION_KILL_PROCESS,
    ) -> "tuple[int, int]":
        """Create the one-click rule pair for ``ssid``.

        Generates an on_disconnect and an on_switch (to ``*``) rule,
        both enabled, both running ``action_type`` (default
        clashmi.kill_process). ``cooldown`` is an explicit parameter
        (review optimization 6) so the logic is testable without the
        form; None falls back to the form's spin value. Rules whose
        signature already exists are skipped. Returns (added, skipped).
        """
        if cooldown is None:
            cooldown = self.cooldown_spin.value()
        forms = [
            {
                "event_type": EVENT_DISCONNECT,
                "sources": [ssid],
                "targets": [],
                "when": {"event": EVENT_DISCONNECT, "previous_ssid": ssid},
                "action_type": action_type,
            },
            {
                "event_type": EVENT_SWITCH,
                "sources": [ssid],
                "targets": ["*"],
                "when": {
                    "event": EVENT_SWITCH,
                    "previous_ssid": ssid,
                    "current_ssid": "*",
                },
                "action_type": action_type,
            },
        ]

        rules = self.config_manager.get_rules()
        added = 0
        skipped = 0
        for form in forms:
            # Coverage, not exact equality (review bug 10): a rule
            # that already runs kill_process on this event/SSID pair —
            # even as its second action — already covers what the
            # one-click pair would do, so adding another kill rule
            # would be redundant. Manual "add rule" keeps the exact
            # full-sequence signature check in _signature.
            if any(
                self._rule_covers_action(rule, form["when"], action_type)
                for rule in rules
            ):
                skipped += 1
                continue
            rules.append(
                {
                    "id": self._make_rule_id(form, rules),
                    "enabled": True,
                    "description": self._describe(form),
                    "when": form["when"],
                    "actions": [{"type": action_type}],
                    "cooldown_seconds": cooldown,
                }
            )
            added += 1

        if added:
            self.config_manager.set_rules(rules)
            self._save_rules()
            self._refresh_table()
        return added, skipped

    # -- startup guard --------------------------------------------------------

    def start_startup_guard(self, delay_ms: int = 4000) -> None:
        """Schedule the one-shot startup guard check (R4).

        Called once from run_gui after the window and tray exist. The
        check itself runs a few seconds later, after the monitor has
        established its baseline.
        """
        if self._startup_guard_started:
            return
        self._startup_guard_started = True
        # P0-7：窗口持有的单发定时器，销毁窗口时不会遗留跨线程定时器。
        self._startup_guard_timer = QTimer(self)
        self._startup_guard_timer.setSingleShot(True)
        self._startup_guard_timer.timeout.connect(self._run_startup_guard_check)
        self._startup_guard_timer.start(delay_ms)

    def _run_startup_guard_check(self) -> None:
        # The ClashMi probe is a network request: worker thread, with
        # the verdict delivered back to the GUI thread via signal (the
        # same pattern as the automatic ClashMi test).
        def work() -> None:
            warn_ssid: Optional[str] = None
            try:
                # Wait (bounded) for the monitor's first state instead
                # of sampling once at a fixed delay (review bug 7):
                # a slow first netsh read used to make the guard
                # silently never happen.
                deadline = time.monotonic() + 15.0
                ssid = self._current_ssid()
                while ssid is None and time.monotonic() < deadline:
                    if getattr(self.monitor, "current_state", None) is not None:
                        break  # state known and not connected: no wait needed
                    time.sleep(0.25)
                    ssid = self._current_ssid()
                if ssid:
                    try:
                        active = ClashMiClient().is_active()
                    except Exception:
                        active = False
                    if startup_guard_should_warn(
                        ssid, self.config_manager.get_rules(), active
                    ):
                        warn_ssid = ssid
            except Exception:
                logger.exception("Startup guard check failed")
            self._startup_guard_result.emit(warn_ssid)

        threading.Thread(target=work, daemon=True).start()

    def _on_startup_guard_result(self, ssid: Optional[str]) -> None:
        if not ssid:
            return
        message = (
            f"当前 Wi-Fi「{ssid}」不在任何规则的来源名单中，"
            "但 ClashMi 仍在开启状态。\n"
            "如果这是不信任的网络，请确认是否需要关闭 ClashMi。"
        )
        logger.warning("Startup guard: connected to uncovered Wi-Fi %r with ClashMi on", ssid)
        # Bring the window forward first (review bug 8): a modal box
        # parented to a tray-hidden window could open underneath other
        # windows with no taskbar flash. The tray bubble stays as the
        # fallback if the dialog is still missed.
        self.show_from_tray()
        if self._tray_icon is not None:
            self._tray_icon.showMessage(
                "小马联动", message, self.windowIcon(), 4000
            )
        QMessageBox.warning(self, "小马联动 提醒", message)

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
            source_text = (
                str(when.get("program", ""))
                if event_type == EVENT_PROGRAM_START
                else ", ".join(_ssids_of(when.get("previous_ssid")))
            )
            self.table.setItem(row, 2, QTableWidgetItem(source_text))
            self.table.setItem(row, 3, QTableWidgetItem(", ".join(_ssids_of(when.get("current_ssid")))))
            action_text = self._describe_rule(
                when,
                [a for a in rule.get("actions", []) if isinstance(a, dict)],
                bool(rule.get("restore_on_exit")),
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
            self._set_action_list([])
            return
        when = rule.get("when", {})
        event_type = str(when.get("event", EVENT_SWITCH))
        if event_type in EVENT_LABELS:
            self.event_combo.setCurrentText(EVENT_LABELS[event_type])
        self.source_edit.setText(", ".join(_ssids_of(when.get("previous_ssid"))))
        self.target_edit.setText(", ".join(_ssids_of(when.get("current_ssid"))))
        self.program_edit.setText(str(when.get("program", "")))
        self.restore_check.setChecked(bool(rule.get("restore_on_exit", False)))
        action_type = self._first_action_type(rule.get("actions", []))
        if action_type in ACTION_LABELS:
            self.action_combo.setCurrentText(ACTION_LABELS[action_type])
        self._set_action_list(rule.get("actions", []))
        try:
            self.cooldown_spin.setValue(int(rule.get("cooldown_seconds", 0) or 0))
        except (TypeError, ValueError):
            self.cooldown_spin.setValue(0)

    # -- action sequence editor (P0-1) -----------------------------------

    @staticmethod
    def _action_display(action: Dict[str, Any]) -> str:
        action_type = str(action.get("type", ""))
        label = _ACTION_TYPE_LABELS.get(action_type, action_type)
        params = []
        for key in ("enabled", "value", "muted", "mode", "path", "name", "plan", "minutes"):
            if key in action:
                params.append(f"{key}={action[key]}")
        if params:
            return f"{label}（{', '.join(params)}）"
        return label

    def _set_action_list(self, actions: Any) -> None:
        self.action_list.clear()
        for action in actions or []:
            if not isinstance(action, dict):
                continue
            item = QListWidgetItem(self._action_display(action))
            item.setData(Qt.ItemDataRole.UserRole, dict(action))
            self.action_list.addItem(item)

    def _current_action_list(self) -> List[Dict[str, Any]]:
        actions: List[Dict[str, Any]] = []
        for index in range(self.action_list.count()):
            item = self.action_list.item(index)
            data = item.data(Qt.ItemDataRole.UserRole)
            if isinstance(data, dict):
                actions.append(dict(data))
        return actions

    def _read_actions(self, existing_actions: Any = None) -> List[Dict[str, Any]]:
        """返回完整动作序列：列表非空取列表，否则保留已有动作或回退单选。"""
        actions = self._current_action_list()
        if actions:
            return actions
        if existing_actions:
            return [dict(a) for a in existing_actions if isinstance(a, dict)]
        return [{"type": LABEL_TO_ACTION[self.action_combo.currentText()]}]

    def _add_action_to_list(self) -> None:
        dialog = ActionEditorDialog(self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        action = dialog.build_action()
        if not action.get("type"):
            return
        item = QListWidgetItem(self._action_display(action))
        item.setData(Qt.ItemDataRole.UserRole, action)
        self.action_list.addItem(item)

    def _edit_action_item(self, item: QListWidgetItem) -> None:
        data = item.data(Qt.ItemDataRole.UserRole)
        if not isinstance(data, dict):
            return
        dialog = ActionEditorDialog(self, action=data)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        action = dialog.build_action()
        item.setText(self._action_display(action))
        item.setData(Qt.ItemDataRole.UserRole, action)

    def _remove_selected_action(self) -> None:
        row = self.action_list.currentRow()
        if row < 0:
            return
        self.action_list.takeItem(row)

    def _move_action(self, delta: int) -> None:
        row = self.action_list.currentRow()
        if row < 0:
            return
        target = row + delta
        if target < 0 or target >= self.action_list.count():
            return
        item = self.action_list.takeItem(row)
        self.action_list.insertItem(target, item)
        self.action_list.setCurrentRow(target)

    def _test_selected_rule(self) -> None:
        """▶ 立即测试：忽略触发条件与冷却，按当前表单/列表动作串试跑。"""
        rule_id = self._selected_rule_id()
        rule = self._find_rule(rule_id)
        actions = self._read_actions(rule.get("actions", []) if rule else None)
        if not actions:
            QMessageBox.information(self, "小马联动", "请先添加至少一个动作")
            return
        self.status_label.setText("正在执行规则测试…")
        # 动作执行放后台线程，高危动作横幅才能在 GUI 事件循环中显示。
        def work() -> None:
            try:
                from action_executor import ActionExecutor

                executor = ActionExecutor()
                for action in actions:
                    result = executor.execute_action(
                        action, {}, confirmation=self._confirm_action
                    )
                    if not result.get("ok"):
                        self._rule_test_result.emit(
                            {"ok": False, "error": result.get("error") or str(result)}
                        )
                        return
                self._rule_test_result.emit({"ok": True})
            except Exception as exc:
                self._rule_test_result.emit({"ok": False, "error": str(exc)})

        threading.Thread(target=work, daemon=True).start()

    def _on_rule_test_result(self, result: Dict[str, Any]) -> None:
        if not result.get("ok"):
            QMessageBox.warning(
                self, "规则测试失败", str(result.get("error", "未知错误"))
            )
            self.status_label.setText("规则测试失败")
            return
        QMessageBox.information(self, "小马联动", "规则测试执行完成")
        self.status_label.setText("规则测试完成")

    def _confirm_action(self, action: Dict[str, Any]) -> bool:
        """立即测试时的高危动作确认：复用 GUI 线程持有的倒计时横幅桥。"""
        if self._confirmation_bridge is None:
            return False
        return self._confirmation_bridge.confirm(action)

    def _table_item_changed(self, item: QTableWidgetItem) -> None:
        if self._updating_table or item.column() != 0:
            return
        rule = self._find_rule(item.data(Qt.ItemDataRole.UserRole))
        if rule is not None:
            # Replace, don't mutate (pre-check item 6): the monitor
            # thread may be evaluating this very dict while the GUI
            # edits it. A fresh dict is swapped into the list and the
            # by-id index together.
            new_rule = dict(rule)
            new_rule["enabled"] = item.checkState() == Qt.CheckState.Checked
            rules = self.config_manager.get_rules()
            for index, existing in enumerate(rules):
                if existing is rule or existing.get("id") == rule.get("id"):
                    rules[index] = new_rule
                    break
            self._rules_by_id[str(new_rule.get("id"))] = new_rule
            self.config_manager.set_rules(rules)
            self._save_rules()

    # -- rule editing -----------------------------------------------------

    def _on_event_changed(self, label: str) -> None:
        event_type = LABEL_TO_EVENT.get(label, EVENT_SWITCH)
        is_program = event_type == EVENT_PROGRAM_START
        self.source_edit.setEnabled(event_type in {EVENT_SWITCH, EVENT_DISCONNECT})
        self.target_edit.setEnabled(event_type in {EVENT_SWITCH, EVENT_CONNECT})
        self.program_edit.setEnabled(is_program)
        self.restore_check.setEnabled(is_program)

    def _read_form(self) -> Optional[Dict[str, Any]]:
        """Validate the form and build the rule's ``when``/action parts."""
        event_type = LABEL_TO_EVENT.get(self.event_combo.currentText(), EVENT_SWITCH)
        sources = _parse_ssid_list(self.source_edit.text())
        targets = _parse_ssid_list(self.target_edit.text())
        program = self.program_edit.text().strip()

        if event_type in {EVENT_SWITCH, EVENT_DISCONNECT} and not sources:
            QMessageBox.warning(self, "小马联动", "请填写来源 SSID")
            return None
        if event_type in {EVENT_SWITCH, EVENT_CONNECT} and not targets:
            QMessageBox.warning(self, "小马联动", "请填写目标 SSID")
            return None
        if event_type == EVENT_PROGRAM_START and not program:
            QMessageBox.warning(self, "小马联动", "请填写要监听的程序名（如 wegame.exe）")
            return None

        when: Dict[str, Any] = {"event": event_type}
        if event_type in {EVENT_SWITCH, EVENT_DISCONNECT}:
            when["previous_ssid"] = _ssid_value(sources)
        if event_type in {EVENT_SWITCH, EVENT_CONNECT}:
            when["current_ssid"] = _ssid_value(targets)
        if event_type == EVENT_PROGRAM_START:
            when["program"] = program

        action_type = LABEL_TO_ACTION[self.action_combo.currentText()]
        return {
            "when": when,
            "action_type": action_type,
            "sources": sources,
            "targets": targets,
            "program": program,
            "restore_on_exit": bool(self.restore_check.isChecked()),
            "event_type": event_type,
        }

    @staticmethod
    def _actions_signature(actions: Any) -> tuple:
        """Normalized full action sequence for a rule/form.

        Accepts a single action type (str), a one-item form value or a
        rule's actions list. Duplicate detection must consider *all*
        actions, not just the first (review bug 10): a rule that runs
        kill_process as its second action is still a duplicate of the
        one-click pair.
        """
        if isinstance(actions, str):
            return (actions,)
        if isinstance(actions, list):
            return tuple(
                str(action.get("type", ""))
                for action in actions
                if isinstance(action, dict)
            )
        return ()

    def _signature(self, when: Dict[str, Any], action_type: Any) -> tuple:
        # SSID order and case must not affect duplicate detection: the
        # same set typed in a different order (or case) is the same
        # rule. SSIDs are compared normalized, actions as the full
        # normalized sequence; program triggers compare by normalized
        # program name.
        program = str(when.get("program", "") or "").strip().lower()
        return (
            when.get("event"),
            tuple(sorted(normalized_ssid_set(when.get("previous_ssid")))),
            tuple(sorted(normalized_ssid_set(when.get("current_ssid")))),
            program,
            self._actions_signature(action_type),
        )

    def _rule_signature(self, rule: Dict[str, Any]) -> tuple:
        return self._signature(rule.get("when", {}), rule.get("actions", []))

    @staticmethod
    def _rule_covers_action(
        rule: Dict[str, Any], when: Dict[str, Any], action_type: str
    ) -> bool:
        """True if ``rule`` already performs ``action_type`` for the
        same event and SSID pair (normalized). Used by one-click add
        so a kill already present as a later action is not duplicated."""
        if not isinstance(rule, dict):
            return False
        rule_when = rule.get("when", {})
        if not isinstance(rule_when, dict):
            return False
        if rule_when.get("event") != when.get("event"):
            return False
        if normalized_ssid_set(rule_when.get("previous_ssid")) != normalized_ssid_set(
            when.get("previous_ssid")
        ):
            return False
        if normalized_ssid_set(rule_when.get("current_ssid")) != normalized_ssid_set(
            when.get("current_ssid")
        ):
            return False
        return any(
            isinstance(action, dict) and action.get("type") == action_type
            for action in rule.get("actions", [])
        )

    def _describe(self, form: Dict[str, Any]) -> str:
        action_text = ACTION_LABELS[form["action_type"]]
        source = ", ".join(form["sources"])
        target = ", ".join(form["targets"])
        if form["event_type"] == EVENT_PROGRAM_START:
            program = form.get("program", "")
            restore = "（退出后恢复原状）" if form.get("restore_on_exit") else ""
            return f"当打开 [{program}] 时{action_text}{restore}"
        if form["event_type"] == EVENT_SWITCH:
            return f"从 {source} 切换到 {target} 时{action_text}"
        if form["event_type"] == EVENT_DISCONNECT:
            return f"从 {source} 断开时{action_text}"
        return f"连上 {target} 时{action_text}"

    @staticmethod
    def _action_phrase(action: Dict[str, Any]) -> str:
        """把单个动作渲染成中文人话短语，用于列表展示。"""
        action_type = str(action.get("type", ""))
        if action_type == "bluetooth.set":
            return "打开蓝牙" if action.get("enabled") else "关闭蓝牙"
        if action_type == "brightness.set":
            return f"亮度调至 {action.get('value', '?')}%"
        if action_type == "volume.set":
            return f"音量调至 {action.get('value', '?')}%"
        if action_type == "volume.mute":
            return "静音" if action.get("muted") else "取消静音"
        if action_type == "program.launch":
            path = str(action.get("path", ""))
            name = Path(path).name if path else "程序"
            return f"打开 [{name}]"
        if action_type == "program.close":
            return f"关闭 [{action.get('name', '程序')}]"
        if action_type == "process.kill":
            return f"结束 [{action.get('name', '进程')}]"
        return _ACTION_TYPE_LABELS.get(action_type, action_type)

    def _describe_rule(
        self, when: Dict[str, Any], actions: List[Dict[str, Any]], restore_on_exit: bool = False
    ) -> str:
        action_text = "、".join(self._action_phrase(a) for a in actions) or "无动作"
        event = when.get("event")
        if event == EVENT_PROGRAM_START:
            restore = "（退出后恢复原状）" if restore_on_exit else ""
            return f"当打开 [{when.get('program', '')}] 时{action_text}{restore}"
        source = ", ".join(_ssids_of(when.get("previous_ssid")))
        target = ", ".join(_ssids_of(when.get("current_ssid")))
        if event == EVENT_SWITCH:
            return f"从 {source} 切换到 {target} 时{action_text}"
        if event == EVENT_DISCONNECT:
            return f"从 {source} 断开时{action_text}"
        if event == EVENT_CONNECT:
            return f"连上 {target} 时{action_text}"
        return action_text

    def _add_rule(self) -> None:
        form = self._read_form()
        if form is None:
            return

        actions = self._read_actions()
        rules = self.config_manager.get_rules()
        signature = self._signature(form["when"], actions)
        if any(self._rule_signature(rule) == signature for rule in rules):
            QMessageBox.information(self, "小马联动", "这条规则已存在")
            return

        restore_on_exit = bool(form.get("restore_on_exit"))
        rule = {
            "id": self._make_rule_id(form, rules),
            "enabled": True,
            "description": self._describe_rule(form["when"], actions, restore_on_exit),
            "when": form["when"],
            "actions": actions,
            "cooldown_seconds": self.cooldown_spin.value(),
        }
        if restore_on_exit:
            rule["restore_on_exit"] = True
        rules.append(rule)
        self.config_manager.set_rules(rules)
        self._save_rules()
        self._refresh_table()
        self._select_last_row()

    def _update_selected_rule(self) -> None:
        rule = self._find_rule(self._selected_rule_id())
        if rule is None:
            QMessageBox.information(self, "小马联动", "请先选择要更新的规则")
            return

        form = self._read_form()
        if form is None:
            return

        actions = self._read_actions(rule.get("actions", []))
        signature = self._signature(form["when"], actions)
        for other in self.config_manager.get_rules():
            if other is not rule and self._rule_signature(other) == signature:
                QMessageBox.information(self, "小马联动", "修改后会与另一条规则重复")
                return

        restore_on_exit = bool(form.get("restore_on_exit"))
        # Replace, don't mutate (pre-check item 6): see
        # _table_item_changed for why the dict is swapped, not edited.
        new_rule = dict(rule)
        new_rule["description"] = self._describe_rule(
            form["when"], actions, restore_on_exit
        )
        new_rule["when"] = form["when"]
        new_rule["actions"] = actions
        new_rule["cooldown_seconds"] = self.cooldown_spin.value()
        if restore_on_exit:
            new_rule["restore_on_exit"] = True
        else:
            new_rule.pop("restore_on_exit", None)
        rules = self.config_manager.get_rules()
        for index, existing in enumerate(rules):
            if existing is rule or existing.get("id") == rule.get("id"):
                rules[index] = new_rule
                break
        self._rules_by_id[str(new_rule.get("id"))] = new_rule
        self.config_manager.set_rules(rules)
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
            return
        if self._on_rules_changed is not None:
            try:
                self._on_rules_changed()
            except Exception:
                logger.debug("规则变更回调失败", exc_info=True)

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
            EVENT_PROGRAM_START: "program",
        }[form["event_type"]]
        parts = [event_short]
        if form["sources"]:
            parts.append(safe("_".join(form["sources"])))
        if form["targets"]:
            parts.append(safe("_".join(form["targets"])))
        if form.get("program"):
            parts.append(safe(form["program"]))
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
        # Color matches generate_icon.py's teal #0E7C66 (review
        # optimization 14) so the dev-run icon and the shipped icon
        # are the same color.
        pixmap = QPixmap(64, 64)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        pen = QPen(QColor(14, 124, 102), 4)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        painter.drawArc(14, 8, 36, 36, 30 * 16, 120 * 16)
        painter.drawArc(22, 16, 20, 20, 30 * 16, 120 * 16)
        painter.setBrush(QColor(14, 124, 102))
        painter.drawEllipse(29, 34, 6, 6)
        painter.end()
        return QIcon(pixmap)
