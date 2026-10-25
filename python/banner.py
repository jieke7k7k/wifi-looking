"""高危动作倒计时横幅（小马联动 v1.0.0）。"""
from __future__ import annotations
import ctypes
import sys
from typing import Optional
from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QWidget
IS_WINDOWS = sys.platform == "win32"
WS_EX_NOACTIVATE = 0x08000000
DEFAULT_COUNTDOWN_SECONDS = 15
LBRACKET = "【"
RBRACKET = "】"
def banner_text(rule_name: str, action_name: str, seconds: int) -> str:
    """横幅文案（定死格式，括号为六角括号）。"""
    return (
        f"规则 {rule_name} 已触发，将在 {seconds} 秒后执行"
        f"{LBRACKET}{action_name}{RBRACKET}，点击{LBRACKET}取消{RBRACKET}中止"
    )
class CountdownBanner(QWidget):
    """置顶无焦点倒计时横幅。"""
    cancelled = Signal()
    expired = Signal()
    def __init__(
        self,
        rule_name: str,
        action_name: str,
        seconds: int = DEFAULT_COUNTDOWN_SECONDS,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        if seconds < 1:
            raise ValueError("seconds 必须不小于 1")
        self.rule_name = rule_name
        self.action_name = action_name
        self.total_seconds = seconds
        self.remaining = seconds
        self.noactivate_applied = False
        self.setWindowFlags(
            Qt.WindowStaysOnTopHint | Qt.Tool | Qt.FramelessWindowHint
        )
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.label = QLabel(banner_text(rule_name, action_name, self.remaining), self)
        self.label.setWordWrap(True)
        self.cancel_button = QPushButton("取消", self)
        self.cancel_button.clicked.connect(self._on_cancel)
        layout = QHBoxLayout(self)
        layout.addWidget(self.label, 1)
        layout.addWidget(self.cancel_button)
        layout.setContentsMargins(16, 12, 16, 12)
        self.setLayout(layout)
        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self.tick)
    def text(self) -> str:
        """当前横幅文案（便于测试断言）。"""
        return self.label.text()
    def remaining_seconds(self) -> int:
        return self.remaining
    def is_running(self) -> bool:
        return self._timer.isActive()
    def start(self) -> None:
        """从 total_seconds 重新开始倒计时（不抢焦点地显示）。"""
        self.remaining = self.total_seconds
        self.label.setText(banner_text(self.rule_name, self.action_name, self.remaining))
        self._place_bottom_right()
        self.show()
        self._timer.start()
    def stop(self) -> None:
        self._timer.stop()
    def tick(self) -> None:
        """走一秒：测试可直接调用，无需真实等待。"""
        if self.remaining <= 0:
            return
        self.remaining -= 1
        if self.remaining <= 0:
            self._timer.stop()
            self.expired.emit()
            self.hide()
        else:
            self.label.setText(
                banner_text(self.rule_name, self.action_name, self.remaining)
            )
    def _on_cancel(self) -> None:
        self._timer.stop()
        self.cancelled.emit()
        self.hide()
    def _place_bottom_right(self) -> None:
        try:
            from PySide6.QtGui import QGuiApplication
            screen = QGuiApplication.primaryScreen()
            if screen is None:
                return
            geometry = screen.availableGeometry()
            self.adjustSize()
            margin = 16
            x = geometry.right() - self.width() - margin
            y = geometry.bottom() - self.height() - margin
            self.move(x, y)
        except Exception:
            pass
    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._apply_noactivate()
    def _apply_noactivate(self) -> None:
        if not IS_WINDOWS:
            return
        try:
            user32 = ctypes.windll.user32
            hwnd = int(self.winId())
            GWL_EXSTYLE = -20
            get = user32.GetWindowLongPtrW
            set_ = user32.SetWindowLongPtrW
            get.restype = ctypes.c_ssize_t
            set_.restype = ctypes.c_ssize_t
            style = get(hwnd, GWL_EXSTYLE)
            if style & WS_EX_NOACTIVATE == 0:
                set_(hwnd, GWL_EXSTYLE, style | WS_EX_NOACTIVATE)
            self.noactivate_applied = True
        except Exception:
            self.noactivate_applied = False
__all__ = ["CountdownBanner", "DEFAULT_COUNTDOWN_SECONDS", "banner_text"]
