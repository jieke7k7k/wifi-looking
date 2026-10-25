"""Process start/exit watcher for XiaoMaLink trigger rules.

Implements spec section 2.2 of ``需求-小马联动-v1.0.0.md``:

* a watched process name firing ``on_started`` exactly once per
  start lifecycle, no matter how many instances of that name appear;
* ``on_exited`` firing only after *all* instances of the name have
  disappeared continuously for ``exit_debounce_seconds`` (default
  3.0 s), so launcher processes and multi-child-process apps that
  flap briefly do not trigger a false "exit" (and therefore no
  premature restore);
* processes identified by ``(pid, create_time)`` so a recycled pid
  is never mistaken for the same process, and a launcher handing
  over to the main process (old identity gone, new identity present
  with no absent gap) does not re-fire ``on_started``.

All Windows-only imports are guarded: this module imports and runs
on Linux, where the factory silently falls back to psutil polling.
"""

from __future__ import annotations

import logging
import sys
import threading
import time
from typing import Callable, Dict, Iterable, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Guarded optional imports
# ---------------------------------------------------------------------------

try:
    import psutil
except Exception:  # pragma: no cover - psutil absent
    psutil = None  # type: ignore[assignment]

_WMI_AVAILABLE = False
_wmi_module = None
if sys.platform == "win32":
    try:
        import wmi as _wmi_module  # type: ignore[import-not-found]

        _WMI_AVAILABLE = _wmi_module is not None
    except Exception:
        _wmi_module = None
        _WMI_AVAILABLE = False


def wmi_available() -> bool:
    """True only on Windows with an importable WMI client library."""
    return bool(_WMI_AVAILABLE)


# ---------------------------------------------------------------------------
# Snapshot normalisation
# ---------------------------------------------------------------------------

# A process identity: (pid, create_time). ``create_time`` disambiguates
# pid reuse; two entries with the same pid but different create_time
# are different processes.
ProcessIdentity = Tuple[int, float]


class ProcessEntry:
    """One observed process: normalised name + identity."""

    __slots__ = ("name", "pid", "create_time")

    def __init__(self, name: str, pid: int, create_time: float) -> None:
        self.name = name
        self.pid = pid
        self.create_time = create_time

    @property
    def identity(self) -> ProcessIdentity:
        return (self.pid, self.create_time)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"ProcessEntry(name={self.name!r}, pid={self.pid})"


def _entry_name(entry: object) -> Optional[str]:
    if isinstance(entry, dict):
        name = entry.get("name")
    else:
        name = getattr(entry, "name", None)
        if callable(name):
            try:
                name = name()
            except Exception:
                return None
    if not name:
        return None
    return str(name)


def _entry_identity(entry: object) -> Optional[ProcessIdentity]:
    if isinstance(entry, dict):
        pid = entry.get("pid")
        create_time = entry.get("create_time", 0.0)
    else:
        pid = getattr(entry, "pid", None)
        create_time = getattr(entry, "create_time", 0.0)
        if callable(create_time):
            try:
                create_time = create_time()
            except Exception:
                create_time = 0.0
    try:
        return (int(pid), float(create_time))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def normalize_snapshot(snapshot: Iterable[object]) -> List[ProcessEntry]:
    """Normalise raw snapshot rows (dicts, psutil objects, test fakes)."""
    entries: List[ProcessEntry] = []
    for entry in snapshot or []:
        name = _entry_name(entry)
        identity = _entry_identity(entry)
        if name is None or identity is None:
            continue
        entries.append(ProcessEntry(name, identity[0], identity[1]))
    return entries


def _default_psutil_snapshot() -> List[ProcessEntry]:
    if psutil is None:
        return []
    try:
        rows = psutil.process_iter(["pid", "name", "create_time"])
        return normalize_snapshot(list(rows))
    except Exception:
        logger.debug("psutil snapshot failed", exc_info=True)
        return []


def _call_snapshot_fn(snapshot_fn: Callable[..., Iterable[object]]) -> List[ProcessEntry]:
    try:
        return normalize_snapshot(snapshot_fn())
    except TypeError:
        # Tolerate a snapshot_fn that expects no particular signature
        # by retrying plain; any other failure degrades to empty.
        raise
    except Exception:
        logger.debug("injected snapshot_fn failed", exc_info=True)
        return []


SnapshotFn = Callable[[], Iterable[object]]
Callback = Callable[[str], None]


# ---------------------------------------------------------------------------
# Polling watcher
# ---------------------------------------------------------------------------

class ProcessWatcher:
    """Polling process watcher with exit debounce.

    ``watch_names`` is matched case-insensitively (Windows process
    names are case-insensitive). Callbacks receive just the watched
    name as written in ``watch_names`` (first spelling wins).
    """

    def __init__(
        self,
        watch_names: Iterable[str],
        on_started: Optional[Callback] = None,
        on_exited: Optional[Callback] = None,
        snapshot_fn: Optional[SnapshotFn] = None,
        interval_seconds: float = 1.0,
        exit_debounce_seconds: float = 3.0,
        time_fn: Optional[Callable[[], float]] = None,
    ) -> None:
        self._display_names: Dict[str, str] = {}
        self.watch_names: Set[str] = set()
        for name in watch_names:
            lowered = name.lower()
            self._display_names.setdefault(lowered, name)
            self.watch_names.add(lowered)
        self.on_started = on_started
        self.on_exited = on_exited
        self.snapshot_fn = snapshot_fn or _default_psutil_snapshot
        self.interval_seconds = float(interval_seconds)
        self.exit_debounce_seconds = float(exit_debounce_seconds)
        self._time_fn = time_fn or time.monotonic

        self._lock = threading.RLock()
        # Names currently considered running (started fired, exit not
        # yet fired). Presence alone is not enough: a name that
        # vanished but is still inside its exit-debounce window stays
        # "active" until the debounce elapses or it reappears.
        self._active: Set[str] = set()
        # Last observed identities per watched name.
        self._identities: Dict[str, Set[ProcessIdentity]] = {}
        # monotonic time when a name was first observed fully absent
        # while active; None when present or not active.
        self._gone_since: Dict[str, Optional[float]] = {}

        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def set_watch_names(self, names: Iterable[str]) -> None:
        """Replace the watched name set (e.g. rules changed).

        Names removed from the set drop their active/exit state, so a
        later exit is never fired for a program no rule watches anymore.
        """
        with self._lock:
            new_lowered: Set[str] = set()
            new_display: Dict[str, str] = {}
            for name in names:
                lowered = name.lower()
                new_display.setdefault(lowered, name)
                new_lowered.add(lowered)
            for removed in set(self.watch_names) - new_lowered:
                self._active.discard(removed)
                self._identities.pop(removed, None)
                self._gone_since.pop(removed, None)
            self.watch_names = new_lowered
            self._display_names = new_display

    # -- core logic ------------------------------------------------------

    def poll_once(self, now: Optional[float] = None) -> Dict[str, object]:
        """Take one snapshot and fire due callbacks.

        Returns a structured summary dict (never raises for an
        expected failure such as a dead snapshot source)::

            {"started": [...], "exited": [...], "active": [...]}
        """
        if now is None:
            now = self._time_fn()
        try:
            entries = _call_snapshot_fn(self.snapshot_fn)
        except Exception:
            logger.debug("snapshot_fn failed", exc_info=True)
            entries = []

        present: Dict[str, Set[ProcessIdentity]] = {}
        for entry in entries:
            lowered = entry.name.lower()
            if lowered in self.watch_names:
                present.setdefault(lowered, set()).add(entry.identity)

        started: List[str] = []
        exited: List[str] = []
        with self._lock:
            for lowered in sorted(self.watch_names):
                display = self._display_names[lowered]
                identities = present.get(lowered, set())
                if identities:
                    # Present: cancel any pending exit. A reappearance
                    # inside the debounce window means the exit never
                    # happened, so no new on_started either.
                    self._gone_since[lowered] = None
                    if lowered not in self._active:
                        self._active.add(lowered)
                        self._identities[lowered] = set(identities)
                        started.append(display)
                        self._fire(self.on_started, display)
                    else:
                        # Identity turnover with no absent gap (e.g.
                        # launcher handing over to the main process)
                        # is one continuous lifecycle: update the
                        # identity set, do not re-fire.
                        self._identities[lowered] = set(identities)
                else:
                    if lowered not in self._active:
                        self._gone_since[lowered] = None
                        continue
                    # All instances gone: start/continue the debounce.
                    gone_since = self._gone_since.get(lowered)
                    if gone_since is None:
                        self._gone_since[lowered] = now
                    elif now - gone_since >= self.exit_debounce_seconds:
                        self._active.discard(lowered)
                        self._identities.pop(lowered, None)
                        self._gone_since[lowered] = None
                        exited.append(display)
                        self._fire(self.on_exited, display)
            active = sorted(self._display_names[n] for n in self._active)
        return {"started": started, "exited": exited, "active": active}

    @staticmethod
    def _fire(callback: Optional[Callback], name: str) -> None:
        if callback is None:
            return
        try:
            callback(name)
        except Exception:
            logger.exception("process watcher callback failed for %s", name)

    # -- background thread ------------------------------------------------

    def start(self) -> None:
        """Start the daemon polling thread (idempotent)."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run, name="process-watcher", daemon=True
        )
        self._thread.start()

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.poll_once()
            except Exception:
                logger.exception("process watcher poll failed")
            self._stop_event.wait(timeout=self.interval_seconds)

    def stop(self, timeout: float = 8.0) -> bool:
        """Stop the polling thread; safe to call when not started.

        P0-7 修复：不再在 2 秒后丢弃线程引用。线程未能在超时内退出时
        保留 ``_thread`` 引用并返回 False，上层退出流程据此延迟销毁 Qt
        对象，避免残留线程在退出后继续触发回调。
        """
        self._stop_event.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)
        if thread is not None and thread.is_alive():
            return False
        self._thread = None
        return True

    @property
    def active_names(self) -> Set[str]:
        with self._lock:
            return {self._display_names[n] for n in self._active}


# ---------------------------------------------------------------------------
# WMI watcher (Windows only; silently falls back to polling)
# ---------------------------------------------------------------------------

class WmiProcessWatcher(ProcessWatcher):
    """Process watcher whose snapshot source is a WMI query.

    On Windows with an importable WMI client this queries
    ``Win32_Process`` (Name / ProcessId / CreationDate) instead of
    psutil, so it follows the spec's "WMI event or high-performance
    polling" option on the WMI side. Where WMI is unavailable (any
    non-Windows platform, missing client library) it silently falls
    back to the psutil polling snapshot -- constructing this class
    never raises for a missing WMI stack.

    ``wmi_client`` is injectable for tests: an object with a
    ``query()`` method returning rows with Name/ProcessId/
    CreationDate attributes or dict keys.
    """

    def __init__(
        self,
        watch_names: Iterable[str],
        on_started: Optional[Callback] = None,
        on_exited: Optional[Callback] = None,
        interval_seconds: float = 1.0,
        exit_debounce_seconds: float = 3.0,
        time_fn: Optional[Callable[[], float]] = None,
        wmi_client: object = None,
    ) -> None:
        self._wmi_client = wmi_client
        self.wmi_used = False
        if wmi_client is not None:
            self.wmi_used = True
        elif _WMI_AVAILABLE and _wmi_module is not None:
            try:
                self._wmi_client = _wmi_module.WMI()
                self.wmi_used = True
            except Exception:
                self._wmi_client = None
                self.wmi_used = False
        super().__init__(
            watch_names,
            on_started=on_started,
            on_exited=on_exited,
            snapshot_fn=self._wmi_snapshot,
            interval_seconds=interval_seconds,
            exit_debounce_seconds=exit_debounce_seconds,
            time_fn=time_fn,
        )

    def _wmi_snapshot(self) -> List[ProcessEntry]:
        if self._wmi_client is None:
            return _default_psutil_snapshot()
        try:
            rows = self._wmi_client.query(  # type: ignore[attr-defined]
                "SELECT Name, ProcessId, CreationDate FROM Win32_Process"
            )
        except Exception:
            logger.debug("WMI process query failed", exc_info=True)
            return _default_psutil_snapshot()
        entries: List[ProcessEntry] = []
        for row in rows or []:
            if isinstance(row, dict):
                name = row.get("Name")
                pid = row.get("ProcessId")
                created = row.get("CreationDate")
            else:
                name = getattr(row, "Name", None)
                pid = getattr(row, "ProcessId", None)
                created = getattr(row, "CreationDate", None)
            try:
                # WMI CreationDate looks like "20261024120000.000000+480".
                create_time = time.mktime(
                    time.strptime(str(created)[:14], "%Y%m%d%H%M%S")
                ) if created else 0.0
            except Exception:
                create_time = 0.0
            if name and pid is not None:
                try:
                    entries.append(
                        ProcessEntry(str(name), int(pid), float(create_time))
                    )
                except (TypeError, ValueError):
                    continue
        return entries


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def create_process_watcher(
    watch_names: Iterable[str],
    on_started: Optional[Callback] = None,
    on_exited: Optional[Callback] = None,
    snapshot_fn: Optional[SnapshotFn] = None,
    interval_seconds: float = 1.0,
    exit_debounce_seconds: float = 3.0,
    time_fn: Optional[Callable[[], float]] = None,
) -> ProcessWatcher:
    """Return a WMI watcher on Windows when WMI is available.

    Otherwise return the psutil polling watcher. An explicitly
    injected ``snapshot_fn`` always wins and yields a polling
    watcher, since the injected source already defines the transport.
    """
    if snapshot_fn is not None:
        return ProcessWatcher(
            watch_names,
            on_started=on_started,
            on_exited=on_exited,
            snapshot_fn=snapshot_fn,
            interval_seconds=interval_seconds,
            exit_debounce_seconds=exit_debounce_seconds,
            time_fn=time_fn,
        )
    if sys.platform == "win32" and wmi_available():
        return WmiProcessWatcher(
            watch_names,
            on_started=on_started,
            on_exited=on_exited,
            interval_seconds=interval_seconds,
            exit_debounce_seconds=exit_debounce_seconds,
            time_fn=time_fn,
        )
    return ProcessWatcher(
        watch_names,
        on_started=on_started,
        on_exited=on_exited,
        snapshot_fn=None,
        interval_seconds=interval_seconds,
        exit_debounce_seconds=exit_debounce_seconds,
        time_fn=time_fn,
    )
