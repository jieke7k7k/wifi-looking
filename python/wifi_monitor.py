from __future__ import annotations

import logging
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Optional


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class WifiState:
    connected: bool
    ssid: Optional[str]
    interface_name: Optional[str]
    state_text: Optional[str]

    @property
    def key(self) -> str:
        return f"{self.connected}:{(self.ssid or '').lower()}"


EventCallback = Callable[[Dict[str, object]], None]


class WifiMonitor(threading.Thread):
    """Background Wi-Fi state monitor with debounce."""

    def __init__(
        self,
        interval_seconds: float = 1.0,
        debounce_count: int = 2,
        on_event: Optional[EventCallback] = None,
    ) -> None:
        super().__init__(daemon=True)
        self.interval_seconds = float(interval_seconds)
        self.debounce_count = int(debounce_count)
        self.on_event = on_event

        self._stop_event = threading.Event()
        self._pause_event = threading.Event()
        self._paused = False
        self._lock = threading.RLock()

        self.previous_ssid: Optional[str] = None
        self.current_ssid: Optional[str] = None
        self.current_state: Optional[WifiState] = None
        self._stable_key: Optional[str] = None
        self._stable_count = 0
        # Key of the last state that passed debounce and was accepted as the
        # confirmed state. ``None`` until the first stable reading, which only
        # establishes the baseline and never emits an event.
        self._confirmed_key: Optional[str] = None

    def stop(self) -> None:
        self._stop_event.set()

    def pause(self) -> None:
        self._paused = True

    def resume(self) -> None:
        self._paused = False
        self._pause_event.set()

    @property
    def paused(self) -> bool:
        return self._paused

    def run(self) -> None:
        logger.info("WifiMonitor started")

        while not self._stop_event.is_set():
            if self._paused:
                self._pause_event.wait(timeout=0.5)
                continue

            try:
                state = self.read_state()
                self._process_state(state)
            except Exception:
                logger.exception("WifiMonitor loop failed")

            self._stop_event.wait(timeout=self.interval_seconds)

        logger.info("WifiMonitor stopped")

    def read_state(self) -> WifiState:
        """Read the current Wi-Fi state through netsh."""
        raw = self._run_netsh()
        return self._parse_netsh_output(raw)

    def _run_netsh(self) -> bytes:
        kwargs: Dict[str, object] = {
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
        }

        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

        process = subprocess.run(["netsh", "wlan", "show", "interfaces"], **kwargs)
        if process.returncode != 0:
            raise RuntimeError(f"netsh exited with {process.returncode}")

        assert isinstance(process.stdout, bytes)
        return process.stdout

    def _parse_netsh_output(self, raw: bytes) -> WifiState:
        text = self._decode_output(raw)
        lines = text.splitlines()

        ssid: Optional[str] = None
        interface_name: Optional[str] = None
        state_text: Optional[str] = None

        for line in lines:
            if ":" not in line:
                continue

            key, value = line.split(":", 1)
            key = key.strip()
            value = value.strip()

            if key == "SSID":
                ssid = value or None
            elif key in {"Name", "名称"}:
                interface_name = value or None
            elif key in {"State", "状态"}:
                state_text = value or None

        connected = bool(ssid) and self._is_connected(state_text)
        return WifiState(
            connected=connected,
            ssid=ssid if connected else None,
            interface_name=interface_name,
            state_text=state_text,
        )

    def _decode_output(self, raw: bytes) -> str:
        for encoding in ("utf-8", "gb18030", "utf-16"):
            try:
                return raw.decode(encoding)
            except UnicodeDecodeError:
                continue

        return raw.decode("utf-8", errors="replace")

    def _is_connected(self, state_text: Optional[str]) -> bool:
        if not state_text:
            return False
        lowered = state_text.lower()
        return "connected" in lowered or state_text in {"已连接", "正在连接"}

    def _process_state(self, state: WifiState) -> None:
        with self._lock:
            self.current_state = state

            if self._stable_key is None or self._stable_key != state.key:
                self._stable_key = state.key
                self._stable_count = 1
            else:
                self._stable_count += 1

            if self._stable_count < self.debounce_count:
                return

            # This stable state is already the confirmed one; nothing changed.
            if self._confirmed_key == state.key:
                return

            is_first_stable = self._confirmed_key is None
            self._confirmed_key = state.key

            new_ssid = state.ssid if state.connected else None
            previous_ssid = self.current_ssid
            self.previous_ssid = previous_ssid
            self.current_ssid = new_ssid

            if is_first_stable:
                # The first stable reading after startup only sets the
                # baseline, so launching the app while already connected
                # does not fire rules. Later transitions report this
                # baseline as their previous_ssid.
                return

            if previous_ssid is None and new_ssid is not None:
                event_type = "on_connect"
            elif previous_ssid is not None and new_ssid is None:
                event_type = "on_disconnect"
            elif (
                previous_ssid is not None
                and new_ssid is not None
                and previous_ssid != new_ssid
            ):
                event_type = "on_switch"
            else:
                return

            event = {
                "type": event_type,
                "previous_ssid": previous_ssid,
                "current_ssid": new_ssid,
                "state": state,
                "timestamp": time.time(),
            }

            if self.on_event:
                self.on_event(event)
