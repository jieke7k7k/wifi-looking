"""Tests for mutex_instance: NamedMutex + fixed_app_id_dir."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mutex_instance import ERROR_ALREADY_EXISTS, MUTEX_NAME, NamedMutex, fixed_app_id_dir


class FakeKernel32:
    """Stands in for ctypes.windll.kernel32; first CreateMutexW wins."""

    _taken = set()

    def __init__(self):
        self.closed = []
        self._handle_counter = 0

    def CreateMutexW(self, _security, _initial_owner, name):
        self._handle_counter += 1
        handle = self._handle_counter
        self._last_error = ERROR_ALREADY_EXISTS if name in FakeKernel32._taken else 0
        FakeKernel32._taken.add(name)
        return handle

    def GetLastError(self):
        return self._last_error

    def CloseHandle(self, handle):
        self.closed.append(handle)
        return True


class MutexWindowsLogicTests(unittest.TestCase):
    def setUp(self):
        FakeKernel32._taken = set()

    def test_first_instance_is_first(self):
        kernel = FakeKernel32()
        mutex = NamedMutex(kernel32=kernel)
        try:
            self.assertTrue(mutex.is_first)
            self.assertEqual(mutex.name, MUTEX_NAME)
        finally:
            mutex.release()

    def test_second_instance_is_not_first(self):
        first = NamedMutex(kernel32=FakeKernel32())
        second = NamedMutex(kernel32=FakeKernel32())
        try:
            self.assertTrue(first.is_first)
            self.assertFalse(second.is_first)
        finally:
            first.release()
            second.release()

    def test_release_closes_handle(self):
        kernel = FakeKernel32()
        mutex = NamedMutex(kernel32=kernel)
        handle = mutex._handle
        mutex.release()
        self.assertIn(handle, kernel.closed)
        mutex.release()  # second release is a safe no-op

    def test_context_manager_releases(self):
        kernel = FakeKernel32()
        with NamedMutex(kernel32=kernel) as mutex:
            self.assertTrue(mutex.is_first)
            handle = mutex._handle
        self.assertIn(handle, kernel.closed)


class MutexFileFallbackTests(unittest.TestCase):
    def test_first_and_second_in_temp_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = NamedMutex(lock_dir=tmp)
            second = NamedMutex(lock_dir=tmp)
            try:
                self.assertTrue(first.is_first)
                self.assertFalse(second.is_first)
            finally:
                first.release()
                second.release()
            third = NamedMutex(lock_dir=tmp)
            try:
                self.assertTrue(third.is_first)
            finally:
                third.release()

    def test_stale_lock_from_dead_pid_is_reclaimed(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock = Path(tmp) / "single_instance.lock"
            lock.write_text("999999999", encoding="utf-8")
            mutex = NamedMutex(lock_dir=tmp)
            try:
                self.assertTrue(mutex.is_first)
            finally:
                mutex.release()

    @unittest.skipIf(
        sys.platform == "win32",
        "Windows uses the system mutex and never reads the lock file, "
        "so the file-fallback 'held' behavior does not apply there.",
    )
    def test_unreadable_lock_is_not_stolen(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock = Path(tmp) / "single_instance.lock"
            lock.write_text("not-a-pid", encoding="utf-8")
            mutex = NamedMutex(lock_dir=tmp)
            try:
                self.assertFalse(mutex.is_first)
            finally:
                mutex.release()


class FixedAppIdDirTests(unittest.TestCase):
    def test_override_returned_verbatim(self):
        self.assertEqual(fixed_app_id_dir("/tmp/xyz"), Path("/tmp/xyz"))

    def test_linux_default_is_fixed_and_name_independent(self):
        with mock.patch.object(sys, "platform", "linux"):
            directory = fixed_app_id_dir()
        self.assertEqual(directory.name, "XiaoMaLink")
        self.assertNotIn("WiFi-Looking", str(directory))
        # Separator-independent: on Windows, str() uses backslashes,
        # so a forward-slash endswith check fails even though the
        # mocked-Linux branch returned the right directory.
        self.assertEqual(directory.parts[-3:], (".local", "share", "XiaoMaLink"))


if __name__ == "__main__":
    unittest.main()
