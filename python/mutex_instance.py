"""Named-mutex single-instance guard for XiaoMaLink (v1.0.0).

This module complements -- but does not replace or modify --
``single_instance.py`` (QLockFile + QLocalServer wake channel). The
coordinator owns integrating the two; this module only supplies:

* :class:`NamedMutex`: the spec-fixed named mutex
  ``Global\\XiaoMaLink_SingleInstance_Mutex`` (demand doc section 1).
  On Windows it is created with ``kernel32.CreateMutexW`` and
  ``GetLastError() == ERROR_ALREADY_EXISTS`` (183) decides whether
  this process is the first instance. On non-Windows platforms it
  falls back to an exclusive lock file under
  :func:`fixed_app_id_dir` (the lock directory is injectable so tests
  never touch the real user directory).
* :func:`fixed_app_id_dir`: the long-term stable application-ID
  directory. It deliberately depends on neither the program name nor
  the install directory, so renaming WiFi-Looking -> XiaoMaLink (or
  moving the install folder) can never orphan the single-instance
  identity again (see the "old version has no single-instance
  protection" section of the feasibility survey).

All Windows-only imports are guarded: this module must import
cleanly on Linux, where the file-lock fallback is used instead.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional

MUTEX_NAME = r"Global\XiaoMaLink_SingleInstance_Mutex"
APP_ID = "XiaoMaLink"
ERROR_ALREADY_EXISTS = 183


def fixed_app_id_dir(override: Optional[Path | str] = None) -> Path:
    """Return the fixed application-ID directory.

    Windows: ``%LOCALAPPDATA%\\XiaoMaLink``; other platforms:
    ``~/.local/share/XiaoMaLink``. ``override`` is a test seam: when
    given, it is returned verbatim. The ID is fixed forever and must
    never be derived from the exe name or the install directory.
    """
    if override is not None:
        return Path(override)
    if sys.platform == "win32":
        local_app_data = os.environ.get("LOCALAPPDATA")
        if local_app_data:
            return Path(local_app_data) / APP_ID
        return Path.home() / "AppData" / "Local" / APP_ID
    return Path.home() / ".local" / "share" / APP_ID


def _pid_alive(pid: int) -> bool:
    """True if ``pid`` is currently running (fallback-lock stale check)."""
    if pid <= 0:
        return False
    try:
        import psutil

        return psutil.pid_exists(pid)
    except Exception:
        pass
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _real_kernel32():
    """Return the real Win32 kernel32 handle, or None off Windows."""
    if sys.platform != "win32":
        return None
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        kernel32.CreateMutexW.argtypes = [
            ctypes.c_void_p,
            ctypes.c_bool,
            ctypes.c_wchar_p,
        ]
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle.restype = ctypes.c_bool
        kernel32.GetLastError.argtypes = []
        kernel32.GetLastError.restype = ctypes.c_ulong
        return kernel32
    except Exception:
        return None


class NamedMutex:
    """A named mutex; first acquirer wins, everyone else is secondary.

    Parameters
    ----------
    name:
        Mutex name; defaults to the spec-fixed
        ``Global\\XiaoMaLink_SingleInstance_Mutex``.
    lock_dir:
        Directory for the non-Windows fallback lock file. Defaults to
        :func:`fixed_app_id_dir`.
    kernel32:
        Test seam. When given, it is used as the kernel32 object even
        off Windows, so the Windows branch (first vs. second instance
        via ``ERROR_ALREADY_EXISTS``) can be unit-tested on Linux
        with a mock. When omitted, the real kernel32 is used on
        Windows and the file-lock fallback elsewhere.

    The mutex is acquired in ``__init__``; check :attr:`is_first`
    immediately afterwards. Call :meth:`release` (or use the object
    as a context manager) to free it.
    """

    def __init__(
        self,
        name: str = MUTEX_NAME,
        lock_dir: Optional[Path | str] = None,
        kernel32=None,
    ) -> None:
        self.name = name
        self._kernel32 = kernel32 if kernel32 is not None else _real_kernel32()
        self._handle = None
        self._lock_fd: Optional[int] = None
        self._lock_path: Optional[Path] = None
        self.is_first = False
        if self._kernel32 is not None:
            self._acquire_windows()
        else:
            self._acquire_file_lock(lock_dir)

    # -- Windows ------------------------------------------------------

    def _acquire_windows(self) -> None:
        handle = self._kernel32.CreateMutexW(None, False, self.name)
        if not handle:
            err = self._kernel32.GetLastError()
            raise OSError(f"CreateMutexW failed for {self.name!r}: error {err}")
        self._handle = handle
        self.is_first = self._kernel32.GetLastError() != ERROR_ALREADY_EXISTS

    # -- non-Windows fallback ------------------------------------------

    def _acquire_file_lock(self, lock_dir: Optional[Path | str]) -> None:
        directory = Path(lock_dir) if lock_dir is not None else fixed_app_id_dir()
        directory.mkdir(parents=True, exist_ok=True)
        lock_path = directory / "single_instance.lock"
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            # Crash leftover? Only steal the lock if its owner is dead.
            try:
                pid = int(lock_path.read_text(encoding="utf-8").strip())
            except (OSError, ValueError):
                # Unreadable owner: stay conservative and do not steal
                # a lock that might belong to a live instance.
                self._lock_path = lock_path
                self.is_first = False
                return
            if _pid_alive(pid):
                self._lock_path = lock_path
                self.is_first = False
                return
            try:
                lock_path.unlink()
            except OSError:
                self._lock_path = lock_path
                self.is_first = False
                return
            try:
                fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                self._lock_path = lock_path
                self.is_first = False
                return
        self._lock_fd = fd
        self._lock_path = lock_path
        try:
            os.write(fd, str(os.getpid()).encode("ascii"))
        except OSError:
            pass
        self.is_first = True

    # -- lifecycle ------------------------------------------------------

    def release(self) -> None:
        """Release the mutex / fallback lock. Safe to call twice."""
        if self._handle is not None:
            try:
                self._kernel32.CloseHandle(self._handle)
            except Exception:
                pass
            self._handle = None
        if self._lock_fd is not None:
            try:
                os.close(self._lock_fd)
            except OSError:
                pass
            self._lock_fd = None
            if self._lock_path is not None:
                try:
                    self._lock_path.unlink()
                except OSError:
                    pass
            self._lock_path = None
        self.is_first = False

    def __enter__(self) -> "NamedMutex":
        return self

    def __exit__(self, *exc_info) -> None:
        self.release()

    def __del__(self):  # best effort; explicit release() is preferred
        try:
            self.release()
        except Exception:
            pass
