from __future__ import annotations

import logging
import queue
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional


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

        # Events are delivered through a queue drained by a dedicated
        # dispatcher thread, so a slow ``on_event`` handler (HTTP calls,
        # sleeps) never runs under ``_lock`` and never blocks the polling
        # or native-callback threads. When the dispatcher is not running
        # (unit tests driving ``_process_state`` directly), events are
        # delivered synchronously instead.
        self._event_queue: "queue.Queue[Dict[str, object]]" = queue.Queue()
        self._dispatcher: Optional[threading.Thread] = None

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
        self._pause_event.set()

    # -- event dispatch ---------------------------------------------------

    def _start_dispatcher(self) -> None:
        if self._dispatcher is not None and self._dispatcher.is_alive():
            return
        self._dispatcher = threading.Thread(
            target=self._dispatcher_loop,
            name="wifi-event-dispatcher",
            daemon=True,
        )
        self._dispatcher.start()

    def _dispatcher_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                event = self._event_queue.get(timeout=0.2)
            except queue.Empty:
                continue
            self._deliver(event)

    def _deliver(self, event: Dict[str, object]) -> None:
        if not self.on_event:
            return
        try:
            self.on_event(event)
        except Exception:
            logger.exception("Wi-Fi event handler failed")

    def _dispatch_event(self, event: Dict[str, object]) -> None:
        if self._dispatcher is not None and self._dispatcher.is_alive():
            self._event_queue.put(event)
        else:
            self._deliver(event)

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
        self._start_dispatcher()

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

    # netsh prints one block per interface, blocks separated by blank
    # lines. Keys may be localized and the separator may be a full-width
    # colon on some locales.
    _FIELD_SPLIT = re.compile(r"[:：]")

    def _parse_netsh_output(self, raw: bytes) -> WifiState:
        text = self._decode_output(raw)
        blocks = self._parse_interface_blocks(text)

        if not blocks:
            if text.strip():
                logger.warning(
                    "Could not parse netsh output; treating as disconnected: %r",
                    text[:200],
                )
            return WifiState(
                connected=False, ssid=None, interface_name=None, state_text=None
            )

        # Prefer the block that is actually connected; with several
        # adapters (physical + virtual) the fields of different blocks
        # must never be merged into one state.
        chosen = next(
            (block for block in blocks if self._is_connected(block.get("state"))),
            blocks[0],
        )

        connected = self._is_connected(chosen.get("state"))
        ssid: Optional[str] = None
        if connected:
            # Hidden SSID: the SSID line is empty but the interface is
            # connected. Fall back to the BSSID, then a placeholder, so
            # the state key stays stable instead of flapping.
            ssid = chosen.get("ssid") or chosen.get("bssid") or "<hidden>"

        return WifiState(
            connected=connected,
            ssid=ssid,
            interface_name=chosen.get("name"),
            state_text=chosen.get("state"),
        )

    def _parse_interface_blocks(self, text: str) -> List[Dict[str, Optional[str]]]:
        blocks: List[Dict[str, Optional[str]]] = []
        current: Dict[str, Optional[str]] = {}

        def flush() -> None:
            # Only chunks that look like an interface block (they carry a
            # State or SSID line) count; headers/footers are ignored.
            if "state" in current or "ssid" in current:
                blocks.append(dict(current))
            current.clear()

        for line in text.splitlines():
            if not line.strip():
                flush()
                continue

            parts = self._FIELD_SPLIT.split(line, maxsplit=1)
            if len(parts) != 2:
                continue
            key = parts[0].strip()
            value = parts[1].strip() or None

            if key == "SSID":
                current["ssid"] = value
            elif key == "BSSID":
                current["bssid"] = value
            elif key in {"Name", "名称"}:
                current["name"] = value
            elif key in {"State", "状态"}:
                current["state"] = value

        flush()
        return blocks

    def _decode_output(self, raw: bytes) -> str:
        if raw.startswith((b"\xff\xfe", b"\xfe\xff")) or b"\x00" in raw[:64]:
            try:
                return raw.decode("utf-16")
            except UnicodeDecodeError:
                pass

        for encoding in ("utf-8", "gb18030"):
            try:
                return raw.decode(encoding)
            except UnicodeDecodeError:
                continue

        return raw.decode("utf-8", errors="replace")

    def _is_connected(self, state_text: Optional[str]) -> bool:
        if not state_text:
            return False
        lowered = state_text.strip().lower()
        # Exact whitelist: a substring test would also match
        # "disconnected" / "disconnecting".
        return lowered in {"connected", "connecting"} or state_text.strip() in {
            "已连接",
            "正在连接",
        }

    def _process_state(self, state: WifiState) -> None:
        event: Optional[Dict[str, object]] = None

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

        # Dispatch outside the lock: handlers perform HTTP calls and
        # sleeps and must never block state processing.
        if event is not None:
            self._dispatch_event(event)
