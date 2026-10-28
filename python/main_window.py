from __future__ import annotations

import logging
import sys
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from PySide6.QtCore import QPointF, QSignalBlocker, QSize, Qt, QTimer, Signal
from PySide6.QtGui import (
    QColor,
    QIcon,
    QPainter,
    QPen,
    QPixmap,
    QPolygonF,
    QStandardItem,
    QStandardItemModel,
)
from PySide6.QtWidgets import (
    QAbstractButton,
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QStyle,
    QStyleOptionSpinBox,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
)

from clashmi import ClashMiClient, ClashMiError
from ssid_utils import normalize_ssid, normalized_ssid_set
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

# 动作类型分组（添加动作对话框下拉按组展示）。
_ACTION_GROUPS: List[tuple] = [
    ("ClashMi", [ACTION_CLOSE_CONNECTIONS, ACTION_DISABLE_TUN, ACTION_KILL_PROCESS]),
    ("网络", ["wifi.set", "bluetooth.set"]),
    ("显示", ["brightness.set", "dark_mode.set", "wallpaper.set"]),
    ("声音", ["volume.set", "volume.mute", "audio.device.set"]),
    ("电源", ["power.plan.set", "screen.timeout.set", "sleep", "shutdown", "restart"]),
    ("程序", ["program.launch", "program.close", "process.kill"]),
    ("系统", ["dnd.set", "lock_screen"]),
]

# 全局 Qt Style Sheet：灰阶打底、品牌青绿 #0F766E 只用于核心按钮与开关。
APP_STYLESHEET = """
* {
    font-family: "Segoe UI", "Microsoft YaHei", "PingFang SC", sans-serif;
    font-size: 13px;
    color: #3f3f46;
}
QMainWindow { background: #fafafa; }
QDialog { background: #ffffff; }
QWidget#appRoot { background: #fafafa; }
QScrollArea { background: #fafafa; border: none; }
QScrollArea > QWidget > QWidget { background: #fafafa; }

QFrame#card {
    background: #ffffff;
    border: 1px solid #e4e4e7;
    border-radius: 10px;
}
QFrame#pill {
    background: #ffffff;
    border: 1px solid #e4e4e7;
    border-radius: 12px;
}
QFrame#dot {
    background: #d4d4d8;
    border: none;
    border-radius: 4px;
}
QFrame#dot[state="ok"] { background: #16a34a; }
QFrame#dot[state="warn"] { background: #dc2626; }
QFrame#dot[state="idle"] { background: #d4d4d8; }
QLabel#h1 { font-size: 18px; font-weight: 600; color: #18181b; }
QLabel#dialogTitle { font-size: 17px; font-weight: 600; color: #18181b; }
QLabel#subtitle { font-size: 13px; color: #71717a; }
QLabel#hint { font-size: 12px; color: #a1a1aa; }
QLabel#cardTitle { font-size: 15px; font-weight: 600; color: #18181b; }
QLabel#muted { color: #71717a; }
QLabel#faint { color: #a1a1aa; }
QLabel#tag {
    color: #71717a;
    background: #f4f4f5;
    border-radius: 6px;
    padding: 3px 8px;
    font-size: 12px;
}
QLabel#formLabel { color: #3f3f46; }
QLabel#ruleText { color: #18181b; }
QLabel#num {
    background: #f4f4f5;
    color: #52525b;
    border-radius: 11px;
    font-size: 12px;
    font-weight: 600;
}

QPushButton {
    background: #ffffff;
    color: #3f3f46;
    border: 1px solid #d4d4d8;
    border-radius: 8px;
    padding: 0 16px;
    min-height: 32px;
}
QPushButton:hover { border-color: #a1a1aa; background: #fafafa; }
QPushButton:pressed { background: #f4f4f5; }

QPushButton[role="outline"] {
    background: #ffffff;
    color: #3f3f46;
    border: 1px solid #d4d4d8;
}
QPushButton[role="outline"]:hover { border-color: #a1a1aa; background: #fafafa; }
QPushButton[role="outline"]:pressed { background: #f4f4f5; }

QPushButton[role="primary"] {
    background: #0f766e;
    border: 1px solid #0f766e;
    color: #ffffff;
}
QPushButton[role="primary"]:hover { background: #115e59; border-color: #115e59; }
QPushButton[role="primary"]:pressed { background: #0b5d56; border-color: #0b5d56; }

QPushButton[role="tealOutline"] {
    background: #ffffff;
    color: #0f766e;
    border: 1px solid #0f766e;
}
QPushButton[role="tealOutline"]:hover { background: #f0fdfa; }
QPushButton[role="tealOutline"]:pressed { background: #ccfbf1; }

QPushButton[role="dangerText"] {
    background: transparent;
    color: #dc2626;
    border: 1px solid transparent;
}
QPushButton[role="dangerText"]:hover { background: #fef2f2; }

QPushButton[role="icon"] {
    background: transparent;
    color: #71717a;
    border: none;
    border-radius: 6px;
    padding: 0;
    min-width: 30px;
    max-width: 30px;
    min-height: 30px;
    max-height: 30px;
    font-size: 14px;
}
QPushButton[role="icon"]:hover { background: #f4f4f5; color: #18181b; }

QLineEdit, QComboBox, QSpinBox {
    background: #ffffff;
    color: #18181b;
    border: 1px solid #d4d4d8;
    border-radius: 8px;
    padding: 0 10px;
    min-height: 32px;
}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus { border: 1px solid #0f766e; }
QLineEdit:disabled, QComboBox:disabled, QSpinBox:disabled {
    background: #fafafa;
    color: #a1a1aa;
}

QComboBox QAbstractItemView {
    background: #ffffff;
    border: 1px solid #e4e4e7;
    selection-background-color: #f4f4f5;
    selection-color: #18181b;
    outline: 0;
}
QComboBox QAbstractItemView::item { min-height: 30px; padding-left: 8px; }
QComboBox QAbstractItemView::item:disabled {
    color: #a1a1aa;
    background: #fafafa;
}

QSpinBox { padding-right: 22px; }

QListWidget {
    background: #ffffff;
    border: 1px solid #e4e4e7;
    border-radius: 8px;
    outline: 0;
}
QListWidget::item { border: none; }
QListWidget::item:hover { background: #fafafa; }
QListWidget::item:selected { background: #ccfbf1; }

QFrame#dialogSeparator {
    background: #f4f4f5;
    border: none;
    min-height: 1px;
    max-height: 1px;
}

QScrollBar:vertical { background: #ffffff; width: 10px; margin: 0; }
QScrollBar::handle:vertical {
    background: #e4e4e7;
    border-radius: 5px;
    min-height: 24px;
}
QScrollBar::handle:vertical:hover { background: #d4d4d8; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar:horizontal { background: #ffffff; height: 10px; margin: 0; }
QScrollBar::handle:horizontal {
    background: #e4e4e7;
    border-radius: 5px;
    min-width: 24px;
}
QScrollBar::handle:horizontal:hover { background: #d4d4d8; }
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0; }
"""


class ArrowSpinBox(QSpinBox):
    """QSpinBox dressed by the global QSS.

    Once a QSpinBox is styled by a stylesheet Qt stops drawing the native
    spin arrows (only the button frames remain), so the two triangles are
    painted on top of the still-clickable native buttons.
    """

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt override
        super().paintEvent(event)
        if self.buttonSymbols() == QSpinBox.ButtonSymbols.NoButtons:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#71717a" if self.isEnabled() else "#c4c4c8"))
        option = QStyleOptionSpinBox()
        self.initStyleOption(option)
        for sub_control, up in (
            (QStyle.SubControl.SC_SpinBoxUp, True),
            (QStyle.SubControl.SC_SpinBoxDown, False),
        ):
            rect = self.style().subControlRect(
                QStyle.ComplexControl.CC_SpinBox, option, sub_control, self
            )
            if rect.width() <= 4 or rect.height() <= 4:
                continue
            cx = rect.center().x()
            cy = rect.center().y()
            if up:
                triangle = QPolygonF(
                    [
                        QPointF(cx - 3, cy + 2),
                        QPointF(cx + 3, cy + 2),
                        QPointF(cx, cy - 2),
                    ]
                )
            else:
                triangle = QPolygonF(
                    [
                        QPointF(cx - 3, cy - 2),
                        QPointF(cx + 3, cy - 2),
                        QPointF(cx, cy + 2),
                    ]
                )
            painter.drawPolygon(triangle)
        painter.end()


class ToggleSwitch(QAbstractButton):
    """自绘开关：开为青绿、关为灰，圆角轨道 + 白色滑块。"""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedSize(38, 22)

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt override
        return QSize(38, 22)

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt override
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        if not self.isEnabled():
            track = QColor("#e4e4e7") if self.isChecked() else QColor("#f4f4f5")
        else:
            track = QColor("#0f766e") if self.isChecked() else QColor("#d4d4d8")
        painter.setBrush(track)
        radius = self.height() / 2
        painter.drawRoundedRect(0, 0, self.width(), self.height(), radius, radius)
        knob = self.height() - 4
        painter.setBrush(QColor("#ffffff"))
        x = self.width() - knob - 2 if self.isChecked() else 2
        painter.drawEllipse(x, 2, knob, knob)
        painter.end()


class RuleRowWidget(QWidget):
    """规则列表单行：开关 + 自然语言句 + 冷却/退出恢复标签 + 编辑/删除图标。"""

    clicked = Signal()

    def __init__(self, rule_id: str, text: str, cooldown: int, restore: bool) -> None:
        super().__init__(None)
        self._hover = False
        self.rule_id = rule_id
        self.enabled_switch = ToggleSwitch()
        self.text_label = QLabel(text)
        self.text_label.setObjectName("ruleText")
        self.text_label.setWordWrap(True)
        self.text_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.cooldown_tag = QLabel(f"冷却 {cooldown} 秒")
        self.cooldown_tag.setObjectName("tag")
        self.cooldown_tag.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.restore_tag = QLabel("退出恢复")
        self.restore_tag.setObjectName("tag")
        self.restore_tag.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.edit_button = self._icon_button("✎")
        self.delete_button = self._icon_button("✕")

        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(10)
        layout.addWidget(self.enabled_switch)
        layout.addWidget(self.text_label, 1)
        if cooldown > 0:
            layout.addWidget(self.cooldown_tag)
        if restore:
            layout.addWidget(self.restore_tag)
        layout.addWidget(self.edit_button)
        layout.addWidget(self.delete_button)

    @staticmethod
    def _icon_button(text: str) -> QPushButton:
        button = QPushButton(text)
        button.setProperty("role", "icon")
        return button

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt override
        self.clicked.emit()
        super().mousePressEvent(event)

    def enterEvent(self, event) -> None:  # noqa: N802 - Qt override
        self._hover = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802 - Qt override
        self._hover = False
        self.update()
        super().leaveEvent(event)

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt override
        if self._hover:
            painter = QPainter(self)
            painter.fillRect(self.rect(), QColor("#fafafa"))
        super().paintEvent(event)


class ActionStepWidget(QWidget):
    """动作序列单步：编号 + 人话短语 + 上移/下移/删除图标。"""

    def __init__(self, number: int, text: str) -> None:
        super().__init__(None)
        # 整行对鼠标透明（按钮除外），让单击选中、双击编辑直接落到列表项上。
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.number_label = QLabel(str(number))
        self.number_label.setObjectName("num")
        self.number_label.setFixedSize(22, 22)
        self.number_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.number_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.text_label = QLabel(text)
        self.text_label.setObjectName("ruleText")
        self.text_label.setWordWrap(True)
        self.text_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.up_button = self._icon_button("↑")
        self.down_button = self._icon_button("↓")
        self.delete_button = self._icon_button("✕")

        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 6, 12, 6)
        layout.setSpacing(10)
        layout.addWidget(self.number_label)
        layout.addWidget(self.text_label, 1)
        layout.addWidget(self.up_button)
        layout.addWidget(self.down_button)
        layout.addWidget(self.delete_button)

    @staticmethod
    def _icon_button(text: str) -> QPushButton:
        button = QPushButton(text)
        button.setProperty("role", "icon")
        return button


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
        self.setWindowTitle("添加动作")
        self.setMinimumWidth(480)
        self._param_widgets: Dict[str, QWidget] = {}

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        head = QVBoxLayout()
        head.setContentsMargins(24, 20, 24, 0)
        head.setSpacing(4)
        title = QLabel("添加动作")
        title.setObjectName("dialogTitle")
        subtitle = QLabel("选一个动作，填好参数，它会按顺序执行")
        subtitle.setObjectName("subtitle")
        head.addWidget(title)
        head.addWidget(subtitle)
        root.addLayout(head)

        body = QVBoxLayout()
        body.setContentsMargins(24, 20, 24, 20)
        body.setSpacing(16)

        type_label = QLabel("动作类型")
        type_label.setObjectName("formLabel")
        self.type_combo = QComboBox()
        self.type_combo.setModel(self._build_type_model())
        self.type_combo.currentIndexChanged.connect(self._rebuild_params)
        body.addWidget(type_label)
        body.addWidget(self.type_combo)

        self._params_box = QVBoxLayout()
        self._params_box.setSpacing(10)
        body.addLayout(self._params_box)
        body.addStretch(1)
        root.addLayout(body)

        separator = QFrame()
        separator.setObjectName("dialogSeparator")
        separator.setFrameShape(QFrame.Shape.NoFrame)
        root.addWidget(separator)

        foot = QHBoxLayout()
        foot.setContentsMargins(24, 16, 24, 16)
        foot.setSpacing(8)
        foot.addStretch(1)
        cancel_button = QPushButton("取消")
        cancel_button.clicked.connect(self.reject)
        ok_button = QPushButton("确定")
        ok_button.setProperty("role", "primary")
        ok_button.setDefault(True)
        ok_button.clicked.connect(self.accept)
        foot.addWidget(cancel_button)
        foot.addWidget(ok_button)
        root.addLayout(foot)

        if action is not None:
            self._select_type(action.get("type"))
        else:
            first = self._first_action_index()
            if first >= 0:
                self.type_combo.setCurrentIndex(first)
        self._rebuild_params()
        if action is not None:
            self._load_action(action)

    @staticmethod
    def _build_type_model() -> QStandardItemModel:
        """按分组构建动作类型下拉：组标题为禁用项，仅作视觉分组。"""
        model = QStandardItemModel()
        for group_name, action_types in _ACTION_GROUPS:
            header = QStandardItem(group_name)
            header.setEnabled(False)
            header.setSelectable(False)
            model.appendRow(header)
            for action_type in action_types:
                item = QStandardItem(_ACTION_TYPE_LABELS.get(action_type, action_type))
                item.setData(action_type, Qt.ItemDataRole.UserRole)
                model.appendRow(item)
        return model

    def _first_action_index(self) -> int:
        for index in range(self.type_combo.count()):
            if self.type_combo.itemData(index):
                return index
        return -1

    def _select_type(self, action_type: Any) -> None:
        for index in range(self.type_combo.count()):
            if self.type_combo.itemData(index) == action_type:
                self.type_combo.setCurrentIndex(index)
                return

    def _clear_params(self) -> None:
        def clear_layout(layout) -> None:
            while layout.count():
                item = layout.takeAt(0)
                widget = item.widget()
                if widget is not None:
                    # 先摘掉父级再 deleteLater：切换动作类型时旧参数
                    # 控件必须立即消失，不能等下一轮事件循环。
                    widget.hide()
                    widget.setParent(None)
                    widget.deleteLater()
                    continue
                child = item.layout()
                if child is not None:
                    clear_layout(child)

        clear_layout(self._params_box)
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
                widget = ArrowSpinBox()
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
            field = QVBoxLayout()
            field.setSpacing(6)
            field.addWidget(label)
            field.addWidget(widget)
            if kind == "int":
                hint = QLabel(f"范围 {spec.get('min', 0)}–{spec.get('max', 0)}")
                hint.setObjectName("hint")
                field.addWidget(hint)
            self._params_box.addLayout(field)
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
        self._tray_icon = None
        self._rules_by_id: Dict[str, Dict[str, Any]] = {}
        self._startup_guard_started = False
        self._candidates_refreshed_for_state = False

        from config import APP_VERSION

        self.setWindowTitle(f"小马联动 v{APP_VERSION}")
        self.setWindowIcon(self.create_icon())
        self.setStyleSheet(APP_STYLESHEET)
        self.resize(880, 880)
        self.setMinimumSize(760, 620)

        # -- header -----------------------------------------------------
        title_label = QLabel("Wi-Fi 触发规则")
        title_label.setObjectName("h1")
        subtitle_label = QLabel("按网络变化和程序状态，自动执行你定好的动作")
        subtitle_label.setObjectName("subtitle")
        subtitle_label.setWordWrap(True)

        self.status_label = QLabel("当前 Wi-Fi：读取中…")
        self.clashmi_label = QLabel("ClashMi：检测中…")

        header_layout = QHBoxLayout()
        header_left = QVBoxLayout()
        header_left.setSpacing(4)
        header_left.addWidget(title_label)
        header_left.addWidget(subtitle_label)
        header_layout.addLayout(header_left, 1)
        self.status_pill, self.status_dot = self._make_pill(self.status_label)
        self.clashmi_pill, self.clashmi_dot = self._make_pill(self.clashmi_label)
        header_layout.addWidget(self.status_pill)
        header_layout.addWidget(self.clashmi_pill)

        # -- rules card -------------------------------------------------
        rules_card = QFrame()
        rules_card.setObjectName("card")
        rules_card_layout = QVBoxLayout(rules_card)
        rules_card_layout.setContentsMargins(20, 20, 20, 16)
        rules_card_layout.setSpacing(12)

        rules_head = QHBoxLayout()
        rules_head.setSpacing(8)
        rules_title = QLabel("规则")
        rules_title.setObjectName("cardTitle")
        self.rule_count_label = QLabel("")
        self.rule_count_label.setObjectName("muted")
        self.add_rule_button = QPushButton("＋ 添加规则")
        self.add_rule_button.setProperty("role", "primary")
        self.add_rule_button.clicked.connect(self._add_rule)
        rules_head.addWidget(rules_title)
        rules_head.addWidget(self.rule_count_label)
        rules_head.addStretch(1)
        rules_head.addWidget(self.add_rule_button)
        rules_card_layout.addLayout(rules_head)

        self.rule_list = QListWidget()
        self.rule_list.setObjectName("ruleList")
        self.rule_list.setMinimumHeight(140)
        self.rule_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.rule_list.currentRowChanged.connect(self._on_rule_row_changed)
        rules_card_layout.addWidget(self.rule_list)

        self.empty_state = QWidget()
        empty_layout = QVBoxLayout(self.empty_state)
        empty_layout.setContentsMargins(0, 28, 0, 28)
        empty_layout.setSpacing(10)
        empty_title = QLabel("还没有规则")
        empty_title.setObjectName("cardTitle")
        empty_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty_hint = QLabel("连接指定 Wi-Fi 时，自动执行你定好的动作")
        empty_hint.setObjectName("muted")
        empty_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_quick_add_button = QPushButton("一键添加当前 Wi-Fi")
        self.empty_quick_add_button.setProperty("role", "outline")
        self.empty_quick_add_button.clicked.connect(self._quick_add_current_wifi)
        empty_quick_row = QHBoxLayout()
        empty_quick_row.addStretch(1)
        empty_quick_row.addWidget(self.empty_quick_add_button)
        empty_quick_row.addStretch(1)
        empty_layout.addStretch(1)
        empty_layout.addWidget(empty_title)
        empty_layout.addWidget(empty_hint)
        empty_layout.addLayout(empty_quick_row)
        empty_layout.addStretch(1)
        rules_card_layout.addWidget(self.empty_state)

        # -- editor card ------------------------------------------------
        editor_card = QFrame()
        editor_card.setObjectName("card")
        editor_layout = QVBoxLayout(editor_card)
        editor_layout.setContentsMargins(20, 20, 20, 20)
        editor_layout.setSpacing(12)

        editor_head = QHBoxLayout()
        editor_title = QLabel("编辑规则")
        editor_title.setObjectName("cardTitle")
        self.test_rule_button = QPushButton("▶ 立即测试")
        self.test_rule_button.setProperty("role", "tealOutline")
        self.test_rule_button.clicked.connect(self._test_selected_rule)
        editor_head.addWidget(editor_title)
        editor_head.addStretch(1)
        editor_head.addWidget(self.test_rule_button)
        editor_layout.addLayout(editor_head)

        # 触发时机 + 程序名
        trigger_row = QHBoxLayout()
        trigger_row.setSpacing(8)
        event_label = QLabel("触发时机")
        event_label.setObjectName("formLabel")
        self.event_combo = QComboBox()
        self.event_combo.addItems(list(EVENT_LABELS.values()))
        self.event_combo.currentTextChanged.connect(self._on_event_changed)
        program_label = QLabel("程序名")
        program_label.setObjectName("formLabel")
        self.program_edit = SsidComboBox()
        self.program_edit.setEditable(True)
        self.program_edit.lineEdit().setPlaceholderText("例如 wegame.exe / cloudmusic.exe")
        trigger_row.addWidget(event_label)
        trigger_row.addWidget(self.event_combo, 1)
        trigger_row.addSpacing(12)
        trigger_row.addWidget(program_label)
        trigger_row.addWidget(self.program_edit, 1)
        editor_layout.addLayout(trigger_row)

        # 来源 / 目标 SSID（Wi-Fi 类触发条件使用）
        ssid_row = QHBoxLayout()
        ssid_row.setSpacing(8)
        source_label = QLabel("来源 SSID")
        source_label.setObjectName("formLabel")
        self.source_edit = SsidComboBox()
        self.source_edit.setEditable(True)
        self.source_edit.lineEdit().setPlaceholderText("下拉选择或手动输入（多个用逗号分隔）")
        target_label = QLabel("目标 SSID")
        target_label.setObjectName("formLabel")
        self.target_edit = SsidComboBox()
        self.target_edit.setEditable(True)
        self.target_edit.lineEdit().setPlaceholderText("下拉选择，或 *（任意）")
        ssid_row.addWidget(source_label)
        ssid_row.addWidget(self.source_edit, 1)
        ssid_row.addSpacing(12)
        ssid_row.addWidget(target_label)
        ssid_row.addWidget(self.target_edit, 1)
        editor_layout.addLayout(ssid_row)

        # 动作序列
        actions_head = QHBoxLayout()
        actions_head.setSpacing(8)
        actions_label = QLabel("动作序列")
        actions_label.setObjectName("formLabel")
        actions_hint = QLabel("按顺序执行，双击可编辑单条")
        actions_hint.setObjectName("muted")
        actions_head.addWidget(actions_label)
        actions_head.addWidget(actions_hint)
        actions_head.addStretch(1)
        editor_layout.addLayout(actions_head)

        self.action_list = QListWidget()
        self.action_list.setObjectName("steps")
        self.action_list.setMinimumHeight(44)
        self.action_list.itemDoubleClicked.connect(self._edit_action_item)
        editor_layout.addWidget(self.action_list)

        add_action_row = QHBoxLayout()
        self.add_action_button = QPushButton("＋ 添加动作")
        self.add_action_button.setProperty("role", "outline")
        self.add_action_button.clicked.connect(self._add_action_to_list)
        add_action_row.addWidget(self.add_action_button)
        add_action_row.addStretch(1)
        editor_layout.addLayout(add_action_row)

        # 冷却 + 开关
        opt_row = QHBoxLayout()
        opt_row.setSpacing(24)
        cooldown_label = QLabel("冷却（秒）")
        cooldown_label.setObjectName("formLabel")
        self.cooldown_spin = ArrowSpinBox()
        self.cooldown_spin.setRange(0, 86400)
        self.cooldown_spin.setValue(10)
        self.cooldown_spin.setFixedWidth(76)

        self.restore_check = ToggleSwitch()
        self.restore_check.setToolTip("程序退出后，自动还原被本规则修改的系统设置")
        restore_text = QLabel("退出时恢复原设置")
        restore_text.setObjectName("formLabel")

        self.autostart_check = ToggleSwitch()
        self.autostart_check.setToolTip("通过 Windows 任务计划程序实现，避免开机弹出 UAC 确认框")
        self.autostart_check.toggled.connect(self._on_autostart_toggled)
        autostart_text = QLabel("开机自动启动")
        autostart_text.setObjectName("formLabel")

        opt_row.addWidget(cooldown_label)
        opt_row.addWidget(self.cooldown_spin)
        opt_row.addSpacing(8)
        opt_row.addWidget(self.restore_check)
        opt_row.addWidget(restore_text)
        opt_row.addSpacing(8)
        opt_row.addWidget(self.autostart_check)
        opt_row.addWidget(autostart_text)
        opt_row.addStretch(1)
        editor_layout.addLayout(opt_row)

        # 底部操作
        foot = QHBoxLayout()
        foot.setSpacing(8)
        self.delete_rule_button = QPushButton("删除规则")
        self.delete_rule_button.setProperty("role", "dangerText")
        self.delete_rule_button.clicked.connect(self._delete_selected_rule)
        foot.addWidget(self.delete_rule_button)
        foot.addStretch(1)
        self.quick_add_button = QPushButton("一键添加当前 Wi-Fi")
        self.quick_add_button.setProperty("role", "outline")
        self.quick_add_button.clicked.connect(self._quick_add_current_wifi)
        self.test_clashmi_button = QPushButton("测试 ClashMi")
        self.test_clashmi_button.setProperty("role", "outline")
        self.test_clashmi_button.clicked.connect(self._test_clashmi)
        self.refresh_ssid_button = QPushButton("刷新 Wi-Fi 列表")
        self.refresh_ssid_button.setProperty("role", "outline")
        self.refresh_ssid_button.setToolTip("重新扫描当前与附近的 Wi-Fi，更新来源/目标下拉候选")
        self.refresh_ssid_button.clicked.connect(self._refresh_ssid_candidates)
        self.save_rule_button = QPushButton("保存规则")
        self.save_rule_button.setProperty("role", "primary")
        self.save_rule_button.clicked.connect(self._save_rule)
        foot.addWidget(self.quick_add_button)
        foot.addWidget(self.refresh_ssid_button)
        foot.addWidget(self.test_clashmi_button)
        foot.addWidget(self.save_rule_button)
        editor_layout.addLayout(foot)

        # -- assemble ---------------------------------------------------
        root_layout = QVBoxLayout()
        root_layout.setContentsMargins(24, 16, 24, 16)
        root_layout.setSpacing(16)
        root_layout.addLayout(header_layout)
        root_layout.addWidget(rules_card)
        root_layout.addWidget(editor_card)
        root_layout.addStretch(1)

        content = QWidget()
        content.setObjectName("appRoot")
        content.setLayout(root_layout)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(content)
        self.setCentralWidget(scroll)

        self._populate_program_candidates()
        self._on_event_changed(self.event_combo.currentText())
        self._refresh_rule_list()
        self._refresh_status()
        self.status_timer = QTimer(self)
        self.status_timer.timeout.connect(self._refresh_status)
        self.status_timer.start(1000)

        self._clashmi_auto_result.connect(self._set_clashmi_status)
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

    # -- layout helpers ----------------------------------------------------

    def _make_pill(self, text_label: QLabel) -> "tuple[QFrame, QFrame]":
        """把状态文本包进一个白底灰边 pill（带可随状态变色的圆点）。"""
        pill = QFrame()
        pill.setObjectName("pill")
        dot = QFrame()
        dot.setObjectName("dot")
        dot.setProperty("state", "idle")
        dot.setFixedSize(8, 8)
        layout = QHBoxLayout(pill)
        layout.setContentsMargins(12, 6, 12, 6)
        layout.setSpacing(6)
        layout.addWidget(dot)
        layout.addWidget(text_label)
        return pill, dot

    @staticmethod
    def _set_dot_state(dot: QFrame, state: str) -> None:
        if dot.property("state") == state:
            return
        dot.setProperty("state", state)
        dot.style().unpolish(dot)
        dot.style().polish(dot)

    def _set_clashmi_status(self, text: str) -> None:
        self.clashmi_label.setText(text)
        if "失败" in text:
            state = "warn"
        elif "已连接" in text or "连接成功" in text:
            state = "ok"
        else:
            state = "idle"
        self._set_dot_state(self.clashmi_dot, state)

    def _populate_program_candidates(self) -> None:
        """程序名下拉候选：来自 app_detection 的已知程序进程名。"""
        try:
            from app_detection import TARGETS
        except Exception:
            return
        for target in TARGETS.values():
            name = str(target.get("process_name", ""))
            if name:
                self.program_edit.addItem(name)
        # Qt 会把可编辑下拉的首项设为当前文本，Windows 上会预填
        # wegame.exe，用户不改这一栏就会误建监听 WeGame 的规则。
        # 候选只用于下拉选择，输入框保持空白。
        self.program_edit.setCurrentIndex(-1)
        self.program_edit.setEditText("")

    def _on_rule_row_changed(self, row: int) -> None:
        del row
        self._load_selected_rule()

    def _save_rule(self) -> None:
        """「保存规则」：有选中规则则更新，否则作为新规则添加。"""
        if self._selected_rule_id() is not None:
            self._update_selected_rule()
        else:
            self._add_rule()

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
            self._set_dot_state(self.status_dot, "idle")
        else:
            ssid = state.ssid or "未连接"
            self.status_label.setText(f"当前 Wi-Fi：{ssid}")
            connected = bool(getattr(state, "connected", False))
            self._set_dot_state(self.status_dot, "ok" if connected else "idle")
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
        self._set_clashmi_status("ClashMi：检测中…")

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
            self._set_clashmi_status(f"ClashMi：连接失败（{error}）")
            QMessageBox.critical(self, "ClashMi 测试失败", error)
            return

        self._set_clashmi_status(
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
            self._refresh_rule_list()
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

    # -- rule list ----------------------------------------------------------

    def refresh_rules(self) -> None:
        """Rebuild the rule list from the config manager (e.g. after reload)."""
        self._refresh_rule_list()

    def _refresh_rule_list(self, select_rule_id: Optional[str] = None) -> None:
        rules = self.config_manager.get_rules()
        self._rules_by_id = {
            str(rule.get("id")): rule for rule in rules if rule.get("id")
        }
        # 重建期间必须屏蔽 currentRowChanged：clear() 会先发 -1，
        # 把编辑器动作列表清空、选中丢失，之后「保存规则」会误走新增。
        if select_rule_id is None:
            select_rule_id = self._selected_rule_id()
        blocker = QSignalBlocker(self.rule_list)
        try:
            self.rule_list.clear()
            for rule in rules:
                when = rule.get("when", {})
                text = self._describe_rule(
                    when,
                    [a for a in rule.get("actions", []) if isinstance(a, dict)],
                    bool(rule.get("restore_on_exit")),
                )
                rule_id = str(rule.get("id"))
                row = RuleRowWidget(
                    rule_id,
                    text or "无动作",
                    int(rule.get("cooldown_seconds", 0) or 0),
                    bool(rule.get("restore_on_exit")),
                )
                row.enabled_switch.setChecked(bool(rule.get("enabled", True)))
                row.clicked.connect(lambda rid=rule_id: self._select_rule_row(rid))
                row.edit_button.clicked.connect(lambda rid=rule_id: self._edit_rule_row(rid))
                row.delete_button.clicked.connect(
                    lambda rid=rule_id: self._delete_rule_by_id(rid)
                )
                row.enabled_switch.toggled.connect(
                    lambda checked, rid=rule_id: self._rule_enabled_toggled(rid, checked)
                )
                item = QListWidgetItem()
                item.setData(Qt.ItemDataRole.UserRole, rule_id)
                self.rule_list.addItem(item)
                self.rule_list.setItemWidget(item, row)
                item.setSizeHint(row.sizeHint())
        finally:
            blocker.unblock()

        self._update_rule_count(rules)
        self._fit_rule_list_height(bool(rules))
        has_rules = bool(rules)
        self.rule_list.setVisible(has_rules)
        self.empty_state.setVisible(not has_rules)

        # 恢复之前的选中；恢复成功后 setCurrentRow 会重新载入该规则
        # 的编辑器内容，保证选中与动作列表都不丢。
        restored = bool(select_rule_id) and self._select_rule_row(select_rule_id)
        if not restored:
            self._load_selected_rule()

    def _update_rule_count(self, rules: Optional[List[Dict[str, Any]]] = None) -> None:
        if rules is None:
            rules = self.config_manager.get_rules()
        if not rules:
            self.rule_count_label.setText("")
            return
        enabled_count = sum(1 for rule in rules if bool(rule.get("enabled", True)))
        self.rule_count_label.setText(f"· {enabled_count} 条启用中")

    def _fit_rule_list_height(self, has_rules: bool) -> None:
        """按行数自适应高度，避免固定 140 高把最后一行截半。"""
        if not has_rules:
            self.rule_list.setFixedHeight(0)
            return
        visible = min(self.rule_list.count(), 6)
        total = sum(
            max(self.rule_list.item(i).sizeHint().height(), 1) for i in range(visible)
        )
        total += 2 * self.rule_list.frameWidth()
        self.rule_list.setFixedHeight(max(total, 44))

    def _rule_rows(self) -> List[RuleRowWidget]:
        rows: List[RuleRowWidget] = []
        for index in range(self.rule_list.count()):
            widget = self.rule_list.itemWidget(self.rule_list.item(index))
            if isinstance(widget, RuleRowWidget):
                rows.append(widget)
        return rows

    def _select_rule_row(self, rule_id: str) -> bool:
        for index in range(self.rule_list.count()):
            widget = self.rule_list.itemWidget(self.rule_list.item(index))
            if isinstance(widget, RuleRowWidget) and widget.rule_id == rule_id:
                self.rule_list.setCurrentRow(index)
                return True
        return False

    def _edit_rule_row(self, rule_id: str) -> None:
        self._select_rule_row(rule_id)
        self._load_selected_rule()

    def _delete_rule_by_id(self, rule_id: str) -> None:
        rules = [rule for rule in self.config_manager.get_rules() if rule.get("id") != rule_id]
        self.config_manager.set_rules(rules)
        self._save_rules()
        self._refresh_rule_list()

    def _selected_rule_id(self) -> Optional[str]:
        row = self.rule_list.currentRow()
        if row < 0:
            return None
        widget = self.rule_list.itemWidget(self.rule_list.item(row))
        return widget.rule_id if isinstance(widget, RuleRowWidget) else None

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
        self._set_action_list(rule.get("actions", []))
        try:
            self.cooldown_spin.setValue(int(rule.get("cooldown_seconds", 0) or 0))
        except (TypeError, ValueError):
            self.cooldown_spin.setValue(0)

    # -- action sequence editor (P0-1) -----------------------------------

    def _set_action_list(self, actions: Any) -> None:
        self._rebuild_action_list(
            [a for a in (actions or []) if isinstance(a, dict)]
        )

    def _rebuild_action_list(self, actions: List[Dict[str, Any]]) -> None:
        self._clear_action_list()
        for number, action in enumerate(actions, start=1):
            self._add_action_row(action, number, number - 1)
        self._fit_action_list_height()

    def _fit_action_list_height(self) -> None:
        """按动作条数自适应高度，避免空列表占满一大片空白。"""
        visible = min(self.action_list.count(), 6)
        if visible == 0:
            self.action_list.setFixedHeight(44)
            return
        total = sum(
            max(self.action_list.item(i).sizeHint().height(), 1) for i in range(visible)
        )
        total += 2 * self.action_list.frameWidth()
        self.action_list.setFixedHeight(max(total, 44))

    def _clear_action_list(self) -> None:
        for index in range(self.action_list.count()):
            item = self.action_list.item(index)
            widget = self.action_list.itemWidget(item)
            if widget is not None:
                self.action_list.removeItemWidget(item)
                widget.deleteLater()
        self.action_list.clear()

    def _add_action_row(self, action: Dict[str, Any], number: int, index: int) -> None:
        item = QListWidgetItem()
        item.setData(Qt.ItemDataRole.UserRole, dict(action))
        widget = ActionStepWidget(number, self._action_phrase(action))
        widget.up_button.clicked.connect(lambda *_, i=index: self._move_action_row(i, -1))
        widget.down_button.clicked.connect(lambda *_, i=index: self._move_action_row(i, 1))
        widget.delete_button.clicked.connect(lambda *_, i=index: self._delete_action_row(i))
        self.action_list.addItem(item)
        self.action_list.setItemWidget(item, widget)
        item.setSizeHint(widget.sizeHint())

    def _current_action_list(self) -> List[Dict[str, Any]]:
        actions: List[Dict[str, Any]] = []
        for index in range(self.action_list.count()):
            item = self.action_list.item(index)
            data = item.data(Qt.ItemDataRole.UserRole)
            if isinstance(data, dict):
                actions.append(dict(data))
        return actions

    def _read_actions(self, existing_actions: Any = None) -> List[Dict[str, Any]]:
        """返回当前动作列表。

        列表为空时分两种情况：编辑已有规则时返回空列表，由调用方提示
        用户，绝不把规则里旧的动作静默塞回来；新增规则时沿用默认的
        关闭 ClashMi 动作。
        """
        actions = self._current_action_list()
        if actions:
            return actions
        if isinstance(existing_actions, dict) and "actions" in existing_actions:
            return []
        return [{"type": ACTION_KILL_PROCESS}]

    def _add_action_to_list(self) -> None:
        dialog = ActionEditorDialog(self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        action = dialog.build_action()
        if not action.get("type"):
            return
        actions = self._current_action_list()
        actions.append(action)
        self._rebuild_action_list(actions)
        self.action_list.setCurrentRow(len(actions) - 1)

    def _edit_action_item(self, item: QListWidgetItem) -> None:
        data = item.data(Qt.ItemDataRole.UserRole)
        if not isinstance(data, dict):
            return
        dialog = ActionEditorDialog(self, action=data)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        action = dialog.build_action()
        index = self.action_list.row(item)
        actions = self._current_action_list()
        if 0 <= index < len(actions):
            actions[index] = action
        self._rebuild_action_list(actions)
        self.action_list.setCurrentRow(index)

    def _remove_selected_action(self) -> None:
        row = self.action_list.currentRow()
        if row < 0:
            return
        self._delete_action_row(row)

    def _move_action(self, delta: int) -> None:
        row = self.action_list.currentRow()
        if row < 0:
            return
        self._move_action_row(row, delta)

    def _move_action_row(self, index: int, delta: int) -> None:
        actions = self._current_action_list()
        target = index + delta
        if not (0 <= target < len(actions)):
            return
        moved = actions.pop(index)
        actions.insert(target, moved)
        self._rebuild_action_list(actions)
        self.action_list.setCurrentRow(target)

    def _delete_action_row(self, index: int) -> None:
        actions = self._current_action_list()
        if not (0 <= index < len(actions)):
            return
        actions.pop(index)
        self._rebuild_action_list(actions)
        if actions:
            self.action_list.setCurrentRow(min(index, len(actions) - 1))

    def _test_selected_rule(self) -> None:
        """▶ 立即测试：忽略触发条件与冷却，按当前表单/列表动作串试跑。"""
        rule_id = self._selected_rule_id()
        rule = self._find_rule(rule_id)
        actions = self._read_actions(rule if rule else None)
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

    def _rule_enabled_toggled(self, rule_id: str, checked: bool) -> None:
        rule = self._find_rule(rule_id)
        if rule is None:
            return
        # Replace, don't mutate (pre-check item 6): the monitor
        # thread may be evaluating this very dict while the GUI
        # edits it. A fresh dict is swapped into the list and the
        # by-id index together.
        new_rule = dict(rule)
        new_rule["enabled"] = bool(checked)
        rules = self.config_manager.get_rules()
        for index, existing in enumerate(rules):
            if existing is rule or existing.get("id") == rule.get("id"):
                rules[index] = new_rule
                break
        self._rules_by_id[str(new_rule.get("id"))] = new_rule
        self.config_manager.set_rules(rules)
        self._save_rules()
        self._update_rule_count(rules)

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

        actions = self._read_actions()
        action_type = actions[0].get("type") if actions else ACTION_KILL_PROCESS
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
        action_type = form["action_type"]
        action_text = ACTION_LABELS.get(
            action_type, _ACTION_TYPE_LABELS.get(action_type, str(action_type))
        )
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
        source = ", ".join("任意网络" if s == "*" else s for s in _ssids_of(when.get("previous_ssid")))
        target = ", ".join("任意网络" if s == "*" else s for s in _ssids_of(when.get("current_ssid")))
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
        self._refresh_rule_list()
        self._select_last_row()

    def _update_selected_rule(self) -> None:
        rule = self._find_rule(self._selected_rule_id())
        if rule is None:
            QMessageBox.information(self, "小马联动", "请先选择要更新的规则")
            return

        form = self._read_form()
        if form is None:
            return

        actions = self._read_actions(rule)
        if not actions:
            QMessageBox.warning(self, "小马联动", "请至少添加一个动作再保存")
            return
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
        # 保存后重建列表并重选原规则：编辑器显示与保存内容一致，
        # 连续保存两次也不会因为“无选中”而多出一条规则。
        self._refresh_rule_list(select_rule_id=str(new_rule.get("id")))

    def _delete_selected_rule(self) -> None:
        rule_id = self._selected_rule_id()
        if rule_id is None:
            return
        rules = [rule for rule in self.config_manager.get_rules() if rule.get("id") != rule_id]
        self.config_manager.set_rules(rules)
        self._save_rules()
        self._refresh_rule_list()

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
        row = self.rule_list.count() - 1
        if row >= 0:
            self.rule_list.setCurrentRow(row)

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
