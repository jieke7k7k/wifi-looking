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
        confirm_delay_seconds: float = 0.0,
        time_fn: Optional[Callable[[], float]] = None,
    ) -> None:
        super().__init__(daemon=True)
        self.interval_seconds = float(interval_seconds)
        self.debounce_count = int(debounce_count)
        self.on_event = on_event
        # v1.0.0: 变化确认延迟（秒）。候选事件须在该时长后重读仍
        # 与承诺的最终 SSID 一致才真正派发，用于睡眠唤醒与信号边缘
        # 的瞬断防抖。生产路径由监测配置传入 3.0；0 表示立即派发
        #（单元测试用默认构造保持同步派发）。
        self.confirm_delay_seconds = float(confirm_delay_seconds)
        self._time_fn = time_fn or time.monotonic
        self._pending_event: Optional[Dict[str, object]] = None
        self._pending_since: Optional[float] = None

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
        # Consecutive read_state() failures, for log throttling: a dead
        # wireless service must not flood app.log once per second
        # (review bug 24).
        self._consecutive_read_failures = 0

    def stop(self) -> None:
        """请求停止并等待监控线程与事件分发线程退出（P0-7 修复）。

        超时采用有限等待：队列 get 最多 0.2s 轮询一次，正常会很快退出；
        若线程仍在执行耗时的 on_event 回调，保留线程引用、不销毁 Qt 对象，
        由上层退出流程继续等待或放弃窗口销毁顺序。
        """
        self._stop_event.set()
        self._pause_event.set()
        dispatcher = self._dispatcher
        if dispatcher is not None and dispatcher.is_alive():
            dispatcher.join(timeout=5.0)
        if self.is_alive() and threading.current_thread() is not self:
            self.join(timeout=5.0)

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
        # Clear the event so wait() in the run loop actually blocks
        # while paused; resume()/stop() set it again (review bug 25 —
        # previously a pause after a resume busy-looped a full core).
        self._pause_event.clear()

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
                self._consecutive_read_failures = 0
            except Exception:
                self._consecutive_read_failures += 1
                failures = self._consecutive_read_failures
                if failures == 1:
                    logger.exception("WifiMonitor loop failed")
                elif failures % 30 == 0:
                    logger.warning(
                        "WifiMonitor state read still failing (%s consecutive attempts)",
                        failures,
                    )
                # Back off instead of spinning at the poll interval
                # while the wireless service is down (review bug 24).
                backoff = min(2 ** min(failures, 3), 8)
                self._stop_event.wait(timeout=backoff)
                continue

            self._stop_event.wait(timeout=self.interval_seconds)

        logger.info("WifiMonitor stopped")

    def read_state(self) -> WifiState:
        """Read the current Wi-Fi state through netsh."""
        raw = self._run_netsh()
        return self._parse_netsh_output(raw)

    def _run_netsh(self) -> bytes:
        return _run_netsh_bytes(["wlan", "show", "interfaces"])

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

    @staticmethod
    def _decode_output(raw: bytes) -> str:
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

    # -- v1.0.0 变化确认延迟 -------------------------------------------
    # 候选事件先挂起，延迟满后重读（即后续稳定读数）仍与承诺的最终
    # SSID 一致才派发；中途变卦（回到旧 SSID 并稳定下来）则丢弃。
    def _confirm_due(self) -> bool:
        if self.confirm_delay_seconds <= 0:
            return True
        if self._pending_since is None:
            return True
        return self._time_fn() - self._pending_since >= self.confirm_delay_seconds

    @staticmethod
    def _same_change(left: Dict[str, object], right: Dict[str, object]) -> bool:
        return (
            left.get("type") == right.get("type")
            and left.get("previous_ssid") == right.get("previous_ssid")
            and left.get("current_ssid") == right.get("current_ssid")
        )

    def _hold_or_release(
        self, candidate: Dict[str, object]
    ) -> Optional[Dict[str, object]]:
        """挂起候选事件，到期返回它，否则返回 None。"""
        if self.confirm_delay_seconds <= 0:
            return candidate
        if self._pending_event is not None and self._same_change(
            self._pending_event, candidate
        ):
            if self._confirm_due():
                self._pending_event = None
                self._pending_since = None
                return candidate
            return None
        self._pending_event = candidate
        self._pending_since = self._time_fn()
        return None

    def _process_state(self, state: WifiState) -> None:
        to_dispatch: Optional[Dict[str, object]] = None

        with self._lock:
            self.current_state = state

            if self._stable_key is None or self._stable_key != state.key:
                self._stable_key = state.key
                self._stable_count = 1
            else:
                self._stable_count += 1

            if self._stable_count < self.debounce_count:
                return

            # This stable state is already the confirmed one; nothing
            # changed — but a pending change may now be due for release
            # if the current reading still matches what it promised.
            if self._confirmed_key == state.key:
                if self._pending_event is not None:
                    now_ssid = state.ssid if state.connected else None
                    if self._pending_event.get("current_ssid") == now_ssid:
                        if self._confirm_due():
                            to_dispatch = self._pending_event
                            self._pending_event = None
                            self._pending_since = None
                    else:
                        # 变化没能稳定住（又回到了旧状态），丢弃挂起事件。
                        self._pending_event = None
                        self._pending_since = None
                # Fall through to dispatch outside the lock.
            else:
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

                candidate = {
                    "type": event_type,
                    "previous_ssid": previous_ssid,
                    "current_ssid": new_ssid,
                    "state": state,
                    "timestamp": time.time(),
                }
                to_dispatch = self._hold_or_release(candidate)

        # Dispatch outside the lock: handlers perform HTTP calls and
        # sleeps and must never block state processing.
        if to_dispatch is not None:
            self._dispatch_event(to_dispatch)


# -- netsh SSID listings (rule-editor candidates) -------------------------
#
# These helpers back the editable SSID dropdowns in the rule editor:
# users pick a network instead of typing its name. All fetchers degrade
# to an empty list on any failure (netsh missing, non-zero exit, odd
# locale) so the editor always stays usable with manual input.

_SSID_INDEX_KEY = re.compile(r"^SSID\s+\d+$")


def _run_netsh_bytes(arguments: List[str]) -> bytes:
    kwargs: Dict[str, object] = {
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
    }

    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

    try:
        process = subprocess.run(["netsh", *arguments], timeout=5, **kwargs)
    except subprocess.TimeoutExpired as exc:
        # A hung netsh (wedged wireless service/driver) must degrade to
        # one failed read, never block the monitor thread forever
        # (review bug 14).
        raise RuntimeError("netsh timed out after 5s") from exc
    if process.returncode != 0:
        raise RuntimeError(f"netsh exited with {process.returncode}")

    assert isinstance(process.stdout, bytes)
    return process.stdout


def _run_netsh_text(arguments: List[str]) -> str:
    return WifiMonitor._decode_output(_run_netsh_bytes(arguments))


def _split_field(line: str) -> "tuple[str, str] | None":
    parts = WifiMonitor._FIELD_SPLIT.split(line, maxsplit=1)
    if len(parts) != 2:
        return None
    return parts[0].strip(), parts[1].strip()


def parse_visible_ssids(text: str) -> List[str]:
    """Extract SSIDs from ``netsh wlan show networks`` output.

    Only the ``SSID <n> :`` lines count (BSSID lines must not match);
    hidden networks have an empty value and are skipped. The SSID label
    itself is not localized, so this works for English and Chinese
    output alike.
    """
    ssids: List[str] = []
    for line in text.splitlines():
        field = _split_field(line)
        if field is None:
            continue
        key, value = field
        if value and _SSID_INDEX_KEY.match(key) and value not in ssids:
            ssids.append(value)
    return ssids


def parse_saved_profile_names(text: str) -> List[str]:
    """Extract profile names from ``netsh wlan show profiles`` output.

    Profile lines are the only ones whose label mentions a profile
    (English "All User Profile"/"Current User Profile", Chinese
    "所有用户配置文件" etc.); placeholders like ``<None>``/``<无>`` are
    skipped.
    """
    names: List[str] = []
    for line in text.splitlines():
        field = _split_field(line)
        if field is None:
            continue
        key, value = field
        if not value or value in {"<None>", "<无>"}:
            continue
        if "Profile" in key or "配置文件" in key:
            if value not in names:
                names.append(value)
    return names


def list_visible_ssids() -> List[str]:
    """SSIDs from a live scan; [] on any failure."""
    try:
        return parse_visible_ssids(_run_netsh_text(["wlan", "show", "networks"]))
    except Exception:
        logger.debug("netsh network scan failed", exc_info=True)
        return []


def list_saved_profile_names() -> List[str]:
    """Saved Wi-Fi profile names; [] on any failure."""
    try:
        return parse_saved_profile_names(_run_netsh_text(["wlan", "show", "profiles"]))
    except Exception:
        logger.debug("netsh profile listing failed", exc_info=True)
        return []


def collect_ssid_candidates(current_ssid: Optional[str]) -> List[str]:
    """Candidate SSIDs for the rule editor, de-duplicated, order kept.

    The currently connected network comes first (it is what a one-click
    rule is about), then scan results, then saved profiles.
    """
    candidates: List[str] = []

    def add(value: Optional[str]) -> None:
        if value and value not in candidates:
            candidates.append(value)

    add(current_ssid)
    for ssid in list_visible_ssids():
        add(ssid)
    for name in list_saved_profile_names():
        add(name)
    return candidates
