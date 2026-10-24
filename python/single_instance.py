"""Single-instance protection (U1, user-reported real-machine bug).

Symptom: with the app already resident in the tray, launching it again
from the desktop icon started a second full instance — two monitor
threads, two tray icons, and rules potentially firing twice.

Mechanism (QLockFile + QLocalServer/QLocalSocket):
- The first instance takes a QLockFile in the data directory and then
  listens on a local server socket. The lock is the authority: only
  one process can hold it.
- A second instance fails to take the lock, sends a "show" message to
  the listening server, and exits immediately — it creates no monitor,
  no tray icon, no window.
- The running instance's server handler emits ``show_requested``; the
  GUI connects that to the main window's show_from_tray(), which
  raises and activates the window.
- Crash leftovers: if the lock holder's PID no longer exists (the
  previous process died without unlocking), the lock is treated as
  stale, removed, and the next launch proceeds. QLockFile also has a
  stale timeout, but PID liveness is the decisive check, so a lock
  that is merely old — from a genuinely long-running instance — is
  never stolen.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable, Optional

from PySide6.QtCore import QLockFile, QObject, Signal
from PySide6.QtNetwork import QLocalServer, QLocalSocket


logger = logging.getLogger(__name__)

DEFAULT_SERVER_NAME = "WiFi-Looking-single-instance"


def _pid_alive(pid: int) -> bool:
    """True if a process with ``pid`` is currently running."""
    if pid <= 0:
        return False
    try:
        import psutil

        return psutil.pid_exists(pid)
    except Exception:
        pass
    try:
        import os

        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, we just may not signal it
    except OSError:
        return False
    return True


class SingleInstance(QObject):
    """Guard that ensures only one app instance runs at a time."""

    show_requested = Signal()

    def __init__(
        self,
        lock_path: Path | str,
        server_name: str = DEFAULT_SERVER_NAME,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.lock_path = Path(lock_path)
        self.server_name = server_name
        self._lock = QLockFile(str(self.lock_path))
        self._lock.setStaleLockTime(10000)
        self._server: Optional[QLocalServer] = None
        self._held = False

    # -- first instance -------------------------------------------------

    def try_acquire(self) -> bool:
        """Take the lock and start listening; False if another instance runs."""
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        if self._lock.tryLock(100):
            return self._start_server()
        # Locked by someone else: if the holder is dead, this is a
        # crash leftover, not a running instance.
        pid, _hostname, _appname = self._lock.getLockInfo()
        if pid and not _pid_alive(pid):
            logger.warning(
                "Stale single-instance lock (holder pid %s is gone); removing it",
                pid,
            )
            self._lock.removeStaleLockFile()
            if self.lock_path.exists():
                try:
                    self.lock_path.unlink()
                except OSError:
                    pass
            if self._lock.tryLock(100):
                return self._start_server()
        return False

    def _start_server(self) -> bool:
        server = QLocalServer(self)
        if not server.listen(self.server_name):
            # A leftover socket file from a crashed run can block
            # listen(); the lock is ours, so it is safe to clear it.
            QLocalServer.removeServer(self.server_name)
            if not server.listen(self.server_name):
                logger.warning("Could not listen on %s", self.server_name)
                self._lock.unlock()
                return False
        server.newConnection.connect(self._on_new_connection)
        self._server = server
        self._held = True
        return True

    def _on_new_connection(self) -> None:
        if self._server is None:
            return
        # A connection itself is the wake request. Do NOT block on
        # waitForReadyRead here: on Windows the client may disconnect
        # before the server reads, which drops the payload and loses
        # the wake entirely (CI failure: woken stayed [] even though
        # notify_existing() had returned True). Emit on connect and
        # keep each socket alive until it disconnects.
        pending = []
        while True:
            socket = self._server.nextPendingConnection()
            if socket is None:
                break
            socket.setParent(self)
            socket.disconnected.connect(socket.deleteLater)
            pending.append(socket)
        if pending:
            self.show_requested.emit()

    # -- second instance --------------------------------------------------

    def notify_existing(self, timeout_ms: int = 2000) -> bool:
        """Tell the running instance to show its window. False if unreachable."""
        socket = QLocalSocket()
        socket.connectToServer(self.server_name)
        if not socket.waitForConnected(timeout_ms):
            logger.warning("Could not reach the running instance")
            return False
        socket.write(b"show\n")
        socket.waitForBytesWritten(timeout_ms)
        # Grace period before closing: on Windows an instantly closed
        # pipe can vanish before the server accepts the connection.
        # The second instance exits right after this anyway, so a
        # short delay here is imperceptible.
        import time

        time.sleep(0.1)
        socket.disconnectFromServer()
        return True

    def release(self) -> None:
        if self._server is not None:
            self._server.close()
            self._server = None
        if self._held:
            self._lock.unlock()
            self._held = False
