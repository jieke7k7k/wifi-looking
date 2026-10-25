"""Tests for old_instance: discovery + gentle-then-force takeover."""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

import psutil

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from old_instance import find_candidate_processes, takeover_old_instances


class FakeProc:
    """Minimal psutil-like process for injection."""

    def __init__(self, pid, name, exe, stubborn=False, deny_terminate=False):
        self.pid = pid
        self._name = name
        self._exe = exe
        self._alive = True
        self._stubborn = stubborn
        self._deny_terminate = deny_terminate
        self.terminate_calls = 0
        self.kill_calls = 0
        self.info = {"pid": pid, "name": name, "exe": exe}

    def name(self):
        return self._name

    def exe(self):
        return self._exe

    def is_running(self):
        return self._alive

    def terminate(self):
        self.terminate_calls += 1
        if self._deny_terminate:
            raise psutil.AccessDenied(self.pid)
        if not self._stubborn:
            self._alive = False

    def wait(self, timeout=None):
        if self._alive:
            # psutil 7: TimeoutExpired(seconds, pid=..., name=...)
            raise psutil.TimeoutExpired(timeout, pid=self.pid)
        return 0

    def kill(self):
        self.kill_calls += 1
        self._alive = False


class FindCandidatesTests(unittest.TestCase):
    def test_match_by_exe_name(self):
        procs = [
            FakeProc(111, "WiFi-Looking.exe", r"C:\Old\WiFi-Looking.exe"),
            FakeProc(222, "notepad.exe", r"C:\Windows\notepad.exe"),
        ]
        found = find_candidate_processes(process_iter=procs)
        self.assertEqual([p.pid for p in found], [111])

    def test_match_by_new_exe_name_and_by_install_dir(self):
        procs = [
            FakeProc(111, "XiaoMaLink.exe", r"C:\New\XiaoMaLink.exe"),
            FakeProc(222, "launcher.exe", r"C:\OldApp\launcher.exe"),
        ]
        found = find_candidate_processes(
            process_iter=procs, install_dirs=[r"C:\OldApp"]
        )
        self.assertEqual([p.pid for p in found], [111, 222])

    def test_current_process_is_never_a_candidate(self):
        proc = FakeProc(os.getpid(), "XiaoMaLink.exe", r"C:\New\XiaoMaLink.exe")
        self.assertEqual(find_candidate_processes(process_iter=[proc]), [])


class TakeoverTests(unittest.TestCase):
    def test_gentle_terminate_counts_as_terminated(self):
        proc = FakeProc(111, "WiFi-Looking.exe", r"C:\Old\WiFi-Looking.exe")
        report = takeover_old_instances(candidates=[proc], wait_timeout=0.1)
        self.assertEqual(report["found"], 1)
        self.assertEqual(report["terminated"], [111])
        self.assertEqual(report["killed"], [])
        self.assertEqual(proc.kill_calls, 0)

    def test_stubborn_process_is_killed_after_wait(self):
        proc = FakeProc(111, "WiFi-Looking.exe", r"C:\Old\WiFi-Looking.exe", stubborn=True)
        report = takeover_old_instances(candidates=[proc], wait_timeout=0.1)
        self.assertEqual(report["terminated"], [])
        self.assertEqual(report["killed"], [111])
        self.assertEqual(proc.terminate_calls, 1)
        self.assertEqual(proc.kill_calls, 1)

    def test_access_denied_is_recorded_never_raised(self):
        proc = FakeProc(111, "WiFi-Looking.exe", r"C:\Old\WiFi-Looking.exe", deny_terminate=True)
        report = takeover_old_instances(candidates=[proc], wait_timeout=0.1)
        self.assertEqual(report["access_denied"], [111])
        self.assertEqual(report["terminated"], [])
        self.assertEqual(report["killed"], [])
        self.assertEqual(report["errors"], [])

    def test_dry_run_touches_nothing(self):
        proc = FakeProc(111, "WiFi-Looking.exe", r"C:\Old\WiFi-Looking.exe")
        report = takeover_old_instances(candidates=[proc], dry_run=True)
        self.assertEqual(report["found"], 1)
        self.assertEqual(report["terminated"], [])
        self.assertEqual(proc.terminate_calls, 0)
        self.assertTrue(proc.is_running())


if __name__ == "__main__":
    unittest.main()
