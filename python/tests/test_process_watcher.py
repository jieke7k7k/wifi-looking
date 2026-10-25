"""Tests for process_watcher: single-fire start, debounced exit, fallback."""

from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from process_watcher import (
    ProcessWatcher,
    WmiProcessWatcher,
    create_process_watcher,
    wmi_available,
)


def entry(name: str, pid: int, create_time: float = 1000.0) -> dict:
    return {"name": name, "pid": pid, "create_time": create_time}


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def advance(self, seconds: float) -> None:
        self.now += seconds

    def __call__(self) -> float:
        return self.now


class ProcessWatcherTests(unittest.TestCase):
    def make_watcher(self, current, clock):
        started: list = []
        exited: list = []
        watcher = ProcessWatcher(
            ["wegame.exe"],
            on_started=started.append,
            on_exited=exited.append,
            snapshot_fn=lambda: list(current),
            exit_debounce_seconds=3.0,
            time_fn=clock,
        )
        return watcher, started, exited

    def test_started_fires_once_for_repeated_polls_and_instances(self) -> None:
        clock = FakeClock()
        current = [entry("wegame.exe", 111)]
        watcher, started, _ = self.make_watcher(current, clock)
        watcher.poll_once(now=clock.now)
        watcher.poll_once(now=clock.now)
        # A second instance of the same name is the same lifecycle.
        current.append(entry("wegame.exe", 222))
        watcher.poll_once(now=clock.now)
        self.assertEqual(started, ["wegame.exe"])

    def test_started_is_case_insensitive(self) -> None:
        clock = FakeClock()
        current = [entry("WeGame.EXE", 111)]
        watcher, started, _ = self.make_watcher(current, clock)
        watcher.poll_once(now=clock.now)
        self.assertEqual(started, ["wegame.exe"])

    def test_identity_turnover_without_gap_does_not_refire(self) -> None:
        # Launcher handing over to the main process: old pid gone, new
        # pid present in the same snapshot window, no absent gap.
        clock = FakeClock()
        current = [entry("wegame.exe", 111, 1000.0)]
        watcher, started, exited = self.make_watcher(current, clock)
        watcher.poll_once(now=clock.now)
        current[:] = [entry("wegame.exe", 333, 2000.0)]
        watcher.poll_once(now=clock.now)
        self.assertEqual(started, ["wegame.exe"])
        self.assertEqual(exited, [])

    def test_exit_requires_all_instances_gone(self) -> None:
        clock = FakeClock()
        current = [entry("wegame.exe", 111), entry("wegame.exe", 222)]
        watcher, _, exited = self.make_watcher(current, clock)
        watcher.poll_once(now=clock.now)
        current.pop()  # one instance left: not an exit
        watcher.poll_once(now=clock.now)
        clock.advance(10.0)
        watcher.poll_once(now=clock.now)
        self.assertEqual(exited, [])
        current.clear()  # last instance gone
        watcher.poll_once(now=clock.now)
        clock.advance(3.0)
        watcher.poll_once(now=clock.now)
        self.assertEqual(exited, ["wegame.exe"])

    def test_exit_debounce_reappearance_cancels_exit(self) -> None:
        clock = FakeClock()
        current = [entry("wegame.exe", 111)]
        watcher, started, exited = self.make_watcher(current, clock)
        watcher.poll_once(now=clock.now)
        current.clear()
        watcher.poll_once(now=clock.now)  # gone_since = now
        clock.advance(1.0)
        watcher.poll_once(now=clock.now)  # still inside debounce
        self.assertEqual(exited, [])
        current.append(entry("wegame.exe", 111))
        watcher.poll_once(now=clock.now)  # reappeared within 3 s
        self.assertEqual(exited, [])
        self.assertEqual(started, ["wegame.exe"])  # no second start
        # Now a real exit: gone, then a full debounce window passes.
        current.clear()
        watcher.poll_once(now=clock.now)
        clock.advance(2.9)
        watcher.poll_once(now=clock.now)
        self.assertEqual(exited, [])
        clock.advance(0.2)
        watcher.poll_once(now=clock.now)
        self.assertEqual(exited, ["wegame.exe"])

    def test_snapshot_failure_degrades_to_no_events(self) -> None:
        clock = FakeClock()
        watcher = ProcessWatcher(
            ["wegame.exe"],
            on_started=lambda n: self.fail("must not fire"),
            snapshot_fn=lambda: (_ for _ in ()).throw(RuntimeError("boom")),
            time_fn=clock,
        )
        result = watcher.poll_once(now=clock.now)
        self.assertEqual(result["started"], [])
        self.assertEqual(result["exited"], [])

    def test_start_stop_thread_smoke(self) -> None:
        started: list = []
        current = [entry("wegame.exe", 111)]
        watcher = ProcessWatcher(
            ["wegame.exe"],
            on_started=started.append,
            snapshot_fn=lambda: list(current),
            interval_seconds=0.05,
        )
        watcher.start()
        try:
            deadline = time.time() + 2.0
            while not started and time.time() < deadline:
                time.sleep(0.02)
        finally:
            watcher.stop()
        self.assertEqual(started, ["wegame.exe"])

    def test_factory_falls_back_to_polling_without_wmi(self) -> None:
        watcher = create_process_watcher(["wegame.exe"])
        try:
            if sys.platform != "win32":
                self.assertIsInstance(watcher, ProcessWatcher)
                self.assertNotIsInstance(watcher, WmiProcessWatcher)
                self.assertFalse(wmi_available())
        finally:
            watcher.stop()

    def test_wmi_watcher_constructs_without_wmi_stack(self) -> None:
        # Importing and constructing must never fail on Linux.
        watcher = WmiProcessWatcher(["wegame.exe"])
        try:
            self.assertFalse(watcher.wmi_used)
        finally:
            watcher.stop()

    def test_wmi_watcher_uses_injected_client(self) -> None:
        class FakeWmi:
            def query(self, _sql):
                return [
                    {
                        "Name": "wegame.exe",
                        "ProcessId": 111,
                        "CreationDate": "20261024120000.000000+480",
                    }
                ]

        started: list = []
        watcher = WmiProcessWatcher(
            ["wegame.exe"], on_started=started.append, wmi_client=FakeWmi()
        )
        try:
            self.assertTrue(watcher.wmi_used)
            watcher.poll_once(now=1000.0)
            self.assertEqual(started, ["wegame.exe"])
        finally:
            watcher.stop()


if __name__ == "__main__":
    unittest.main()
