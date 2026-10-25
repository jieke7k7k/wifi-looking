"""Tests for the v0.3.0 cross-review fixes (bugs 1-31), the U1
single-instance guard, and the pre-check items 3-6 and 10.

Each test names the finding it covers so a future regression points
at the review item, not just at a line of code.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import subprocess
import sys
import tempfile
import time
import types
import unittest
from logging.handlers import RotatingFileHandler
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import clashmi_close
from action_executor import ActionExecutor
from clashmi import ClashMiError
from config import ConfigManager, LEGACY_DEFAULT_RULES
from rule_engine import RuleEngine
from ssid_utils import normalize_ssid
from wifi_monitor import WifiMonitor


# ---------------------------------------------------------------- helpers

def _completed(returncode: int):
    return types.SimpleNamespace(returncode=returncode, stdout="", stderr="")


def _rule(sources, actions, enabled=True, event="on_disconnect"):
    return {
        "id": "r",
        "enabled": enabled,
        "when": {"event": event, "previous_ssid": sources},
        "actions": [{"type": a} for a in actions],
        "cooldown_seconds": 10,
    }


try:
    from PySide6.QtWidgets import QApplication
except ImportError:  # pragma: no cover
    QApplication = None


# ------------------------------------------------------- bug 6: SSID norm

class SsidNormalizationTests(unittest.TestCase):
    def test_engine_and_guard_agree_on_case_and_whitespace(self) -> None:
        engine = RuleEngine()
        self.assertTrue(
            engine._matches_ssid_pattern("Xiaomi 14", "xiaomi 14 ")
        )
        if QApplication is not None:
            from main_window import ssid_covered_by_rules

            rules = [_rule("Xiaomi 14", ["clashmi.kill_process"])]
            self.assertTrue(ssid_covered_by_rules("xiaomi 14 ", rules))
            self.assertTrue(ssid_covered_by_rules("Xiaomi 14", rules))
            self.assertFalse(ssid_covered_by_rules("Other", rules))

    def test_normalize(self) -> None:
        self.assertEqual(normalize_ssid("  Xiaomi 14 "), "xiaomi 14")
        self.assertEqual(normalize_ssid(None), "")


# ------------------------------------------------- bugs 1/3/4/5/23: kill

class _FakeClashMiClient:
    """替身 ClashMiClient：API 步骤成功，不触网。"""

    def __init__(self, *args, **kwargs):
        self.settings = types.SimpleNamespace(base_url="http://127.0.0.1:7890")

    def disable_tun(self):
        return None

    def close_connections(self):
        return 0


def _proc(name, pid, ppid=1, exe=None):
    return {
        "name": name,
        "pid": pid,
        "ppid": ppid,
        "exe": exe or ("C:\\ClashMi\\" + name),
    }


class KillProcessReviewTests(unittest.TestCase):
    def run_kill(self, snapshots, tree_kill):
        """snapshots：before/after/final 三次快照；tree_kill：强杀回调。

        v1.0.0 起 clashmi.kill_process 走四步原子关闭（clashmi_close），
        因此 mock clashmi_close 内部的进程快照与进程树强杀，而非旧版的
        action_executor.subprocess.run / ActionExecutor._process_snapshot。
        """
        executor = ActionExecutor()
        patches = [
            mock.patch("action_executor.ClashMiClient", _FakeClashMiClient),
            mock.patch.object(
                clashmi_close,
                "default_process_snapshot",
                side_effect=[list(s) for s in snapshots],
            ),
            mock.patch.object(
                clashmi_close,
                "default_tree_kill",
                side_effect=tree_kill,
            ),
        ]
        for p in patches:
            p.start()
        try:
            return executor.execute_action({"type": "clashmi.kill_process"}, {})
        finally:
            for p in reversed(patches):
                p.stop()

    def test_primary_only_killed_secondary_absent_via_snapshot(self) -> None:
        # bug 1: 快照里根本没有内核进程时，只杀主进程，不凭空按名杀内核。
        snapshots = [
            [_proc("ClashMi.exe", 100)],
            [_proc("ClashMi.exe", 100)],
            [],
        ]
        result = self.run_kill(snapshots, lambda pid, name=None: (True, "killed"))
        self.assertEqual(result["killed"], ["ClashMi.exe"])
        self.assertTrue(result["ok"], result)

    def test_secondary_skipped_when_path_is_not_clashmi(self) -> None:
        # bug 1: 其它客户端（FlClash）的 mihomo.exe 被跳过，绝不无差别按名杀。
        other = _proc("mihomo.exe", 200, ppid=1, exe=r"C:\Users\x\FlClash\mihomo.exe")
        snapshots = [
            [_proc("ClashMi.exe", 100), other],
            [_proc("ClashMi.exe", 100), other],
            [other],
        ]
        result = self.run_kill(snapshots, lambda pid, name=None: (True, "killed"))
        self.assertEqual(result["killed"], ["ClashMi.exe"])
        kill_step = result["steps"][2]
        skipped = kill_step["detail"]["skipped_kernel"]
        self.assertTrue(any(s["pid"] == 200 for s in skipped))
        self.assertTrue(result["ok"], result)

    def test_secondary_killed_when_path_is_clashmi(self) -> None:
        snapshots = [
            [_proc("ClashMi.exe", 100), _proc("mihomo.exe", 101, ppid=100)],
            [_proc("ClashMi.exe", 100), _proc("mihomo.exe", 101, ppid=100)],
            [],
        ]
        result = self.run_kill(snapshots, lambda pid, name=None: (True, "killed"))
        self.assertEqual(sorted(result["killed"]), ["ClashMi.exe", "mihomo.exe"])
        self.assertTrue(result["ok"], result)

    def test_primary_surviving_after_kill_is_not_success(self) -> None:
        # bug 3: 强杀返回 0 不等于 ClashMi 已关，最终快照残留主进程即失败。
        snapshots = [
            [_proc("ClashMi.exe", 100)],
            [_proc("ClashMi.exe", 100)],
            [_proc("ClashMi.exe", 100)],
        ]
        result = self.run_kill(snapshots, lambda pid, name=None: (True, "killed"))
        self.assertFalse(result["ok"])
        self.assertFalse(result["details"]["main_gone"])

    def test_secondary_failure_does_not_block_cooldown(self) -> None:
        # bug 4（v1.0.0 语义）：主进程已消失但 ClashMi 内核强杀失败残留
        # -> ok=False。按需求 4.2，失败/部分失败不计入冷却，可重试，
        # 不再像旧版那样把内核失败吞成成功。
        def kill(pid, name=None):
            return (True, "killed") if pid == 100 else (False, "拒绝访问")

        snapshots = [
            [_proc("ClashMi.exe", 100), _proc("mihomo.exe", 101, ppid=100)],
            [_proc("ClashMi.exe", 100), _proc("mihomo.exe", 101, ppid=100)],
            [_proc("mihomo.exe", 101, ppid=100)],
        ]
        result = self.run_kill(snapshots, kill)
        self.assertFalse(result["ok"])
        self.assertFalse(result["details"]["kernel_gone"])
        self.assertEqual(result["killed"], ["ClashMi.exe"])

    def test_primary_failure_is_failure(self) -> None:
        snapshots = [
            [_proc("ClashMi.exe", 100)],
            [_proc("ClashMi.exe", 100)],
            [_proc("ClashMi.exe", 100)],
        ]
        result = self.run_kill(snapshots, lambda pid, name=None: (False, "taskkill 返回 1"))
        self.assertFalse(result["ok"])

    def test_taskkill_timeout_is_failure_not_hang(self) -> None:
        # bug 5
        executor = ActionExecutor()
        with mock.patch(
            "action_executor.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="taskkill", timeout=3),
        ):
            status, detail = executor._taskkill_image("ClashMi.exe")
        self.assertEqual(status, "failed")
        self.assertIn("timed out", detail)

    def test_taskkill_timeout_kwarg_is_set(self) -> None:
        # bug 5: the subprocess call carries a timeout.
        executor = ActionExecutor()
        with mock.patch(
            "action_executor.subprocess.run", return_value=_completed(0)
        ) as run:
            executor._taskkill_image("ClashMi.exe")
        self.assertEqual(run.call_args.kwargs.get("timeout"), 3)
        self.assertEqual(run.call_args.kwargs.get("errors"), "replace")


# --------------------------------------------- bugs 11/18/19 + opt 9: config

class ConfigReviewTests(unittest.TestCase):
    def load_rules(self, rules):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            config = {
                "monitor": {"interval_seconds": 1.0, "debounce_count": 2},
                "logging": {"level": "INFO", "file": "logs/app.log"},
                "rules": rules,
            }
            path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
            manager = ConfigManager(path)
            loaded = manager.load()
            on_disk = json.loads(path.read_text(encoding="utf-8"))
            return loaded["rules"], on_disk["rules"], manager

    def test_one_bad_rule_does_not_nuke_the_rest(self) -> None:
        # bug 11
        good = {
            "id": "good",
            "enabled": True,
            "when": {"event": "on_disconnect", "previous_ssid": "Home"},
            "actions": [{"type": "clashmi.kill_process"}],
            "cooldown_seconds": 10,
        }
        bad = {"when": {"event": "on_disconnect"}, "actions": []}
        rules, on_disk, manager = self.load_rules([good, bad])
        self.assertEqual([r["id"] for r in rules], ["good"])
        self.assertTrue(manager.last_validation_warnings)

    def test_string_enabled_is_not_treated_as_untouched(self) -> None:
        # bug 18 + bug 11: a string "false" must not fingerprint as
        # untouched, so the set is never migration-cleared. The
        # malformed rule itself is dropped with a warning (it fails
        # enabled-type validation); the untouched sibling survives.
        legacy = copy.deepcopy(LEGACY_DEFAULT_RULES)
        legacy[0]["enabled"] = "false"
        rules, _, manager = self.load_rules(legacy)
        self.assertEqual([r["id"] for r in rules], [LEGACY_DEFAULT_RULES[1]["id"]])
        self.assertTrue(manager.last_validation_warnings)

    def test_single_leftover_legacy_rule_is_kept_by_design(self) -> None:
        # bug 19, deliberately not adopted: migration is whole-set
        # only (see config.py). A user who deleted one shipped rule
        # keeps the other — documented boundary, not a silent clear.
        # The leftover targets Xms networks the user does not own,
        # so it never fires for them, and it can be deleted in the UI.
        legacy = copy.deepcopy(LEGACY_DEFAULT_RULES)
        rules, on_disk, _ = self.load_rules([legacy[0]])
        self.assertEqual(len(rules), 1)
        self.assertEqual(len(on_disk), 1)

    def test_real_v020_config_file_migrates(self) -> None:
        # optimization 9: the migration is anchored on a real v0.2.0
        # file, not on the code's own LEGACY_DEFAULT_RULES constant.
        fixture = Path(__file__).resolve().parent / "fixtures" / "v020_config.json"
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(fixture.read_text(encoding="utf-8"), encoding="utf-8")
            manager = ConfigManager(path)
            loaded = manager.load()
            self.assertEqual(loaded["rules"], [])

    def test_get_rules_returns_a_copy(self) -> None:
        # pre-check item 6
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            manager = ConfigManager(path)
            manager.load()
            manager.set_rules([{"id": "x"}])
            view = manager.get_rules()
            view.append({"id": "y"})
            self.assertEqual([r["id"] for r in manager.get_rules()], ["x"])


# ------------------------------------------------ bugs 12/13: native WLAN

class NativeWifiMonitorTests(unittest.TestCase):
    def test_module_imports_on_non_windows(self) -> None:
        # optimization 8
        import native_wifi_monitor

        self.assertEqual(
            native_wifi_monitor.WLAN_NOTIFICATION_SOURCE_REGISTER,
            native_wifi_monitor.WLAN_NOTIFICATION_SOURCE_ACM
            | native_wifi_monitor.WLAN_NOTIFICATION_SOURCE_MSM,
        )

    def test_register_uses_acm_msm_not_all(self) -> None:
        # bug 12
        import ctypes

        import native_wifi_monitor
        from native_wifi_monitor import NativeWifiMonitor

        calls = {}

        def fake_open(version, reserved, negotiated, handle):
            ctypes.cast(handle, ctypes.POINTER(native_wifi_monitor.HANDLE)).contents.value = 1234
            return 0

        def fake_register(handle, source, ignore_dup, callback, ctx, reserved, prev):
            calls["source"] = source
            calls["callback"] = callback
            return 0

        fake = types.SimpleNamespace(
            WlanOpenHandle=fake_open,
            WlanCloseHandle=lambda handle, reserved: 0,
            WlanRegisterNotification=fake_register,
        )
        monitor = NativeWifiMonitor(interval_seconds=1.0, debounce_count=2)
        monitor._wlanapi = fake
        monitor._open_native_wlan()
        self.assertEqual(
            calls["source"],
            native_wifi_monitor.WLAN_NOTIFICATION_SOURCE_ACM
            | native_wifi_monitor.WLAN_NOTIFICATION_SOURCE_MSM,
        )
        monitor._close_native_wlan()

    def test_close_deregisters_before_closing_handle(self) -> None:
        # bug 13
        import ctypes

        import native_wifi_monitor
        from native_wifi_monitor import NativeWifiMonitor

        order = []

        def fake_open(version, reserved, negotiated, handle):
            ctypes.cast(handle, ctypes.POINTER(native_wifi_monitor.HANDLE)).contents.value = 1234
            return 0

        def fake_register(handle, source, ignore_dup, callback, ctx, reserved, prev):
            order.append(("register", source))
            return 0

        def fake_close(handle, reserved):
            order.append(("close",))
            return 0

        fake = types.SimpleNamespace(
            WlanOpenHandle=fake_open,
            WlanCloseHandle=fake_close,
            WlanRegisterNotification=fake_register,
        )
        monitor = NativeWifiMonitor(interval_seconds=1.0, debounce_count=2)
        monitor._wlanapi = fake
        monitor._open_native_wlan()
        order.clear()
        monitor._close_native_wlan()
        self.assertEqual(order[0], ("register", 0))
        self.assertEqual(order[1], ("close",))


# --------------------------------------- bugs 14/24/25: netsh + monitor loop

class WifiMonitorReviewTests(unittest.TestCase):
    def test_netsh_timeout_becomes_runtime_error(self) -> None:
        # bug 14
        from wifi_monitor import _run_netsh_bytes

        with mock.patch(
            "wifi_monitor.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="netsh", timeout=5),
        ) as run:
            with self.assertRaises(RuntimeError) as ctx:
                _run_netsh_bytes(["wlan", "show", "interfaces"])
        self.assertIn("timed out", str(ctx.exception))
        self.assertEqual(run.call_args.kwargs.get("timeout"), 5)

    def test_pause_clears_event_resume_sets_it(self) -> None:
        # bug 25
        monitor = WifiMonitor(interval_seconds=0.1, debounce_count=1)
        monitor.resume()
        self.assertTrue(monitor._pause_event.is_set())
        monitor.pause()
        self.assertFalse(monitor._pause_event.is_set())
        monitor.resume()
        self.assertTrue(monitor._pause_event.is_set())

    def test_repeated_read_failures_do_not_spin(self) -> None:
        # bug 24: one failure, then backoff — not one exception per poll.
        monitor = WifiMonitor(interval_seconds=0.05, debounce_count=1)
        monitor.read_state = lambda: (_ for _ in ()).throw(RuntimeError("no wifi"))
        monitor.start()
        try:
            time.sleep(0.4)
            self.assertEqual(monitor._consecutive_read_failures, 1)
        finally:
            monitor.stop()
            monitor.join(timeout=3)


# ------------------------------------------------- pre-check 5: monotonic

class MonotonicCooldownTests(unittest.TestCase):
    def test_cooldown_uses_monotonic_clock(self) -> None:
        engine = RuleEngine()
        rule = _rule("Home", ["clashmi.kill_process"])
        rule["cooldown_seconds"] = 3600
        event = {"type": "on_disconnect", "previous_ssid": "Home", "current_ssid": None}
        self.assertEqual(engine.evaluate(event, [rule]), [rule])
        engine.mark_run("r")
        self.assertEqual(engine.evaluate(event, [rule]), [])
        # A wall-clock jump must not end the cooldown: the stored
        # value tracks monotonic(), not time.time().
        self.assertAlmostEqual(engine._last_run["r"], time.monotonic(), delta=5)
        self.assertGreater(abs(engine._last_run["r"] - time.time()), 1000)


# ------------------------------------------------- pre-check 4: log rotation

class LogRotationTests(unittest.TestCase):
    def test_setup_logging_uses_rotating_handler(self) -> None:
        import main as app_main

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            config = {
                "monitor": {"interval_seconds": 1.0, "debounce_count": 2},
                "logging": {"level": "INFO", "file": "logs/app.log"},
                "rules": [],
            }
            path.write_text(json.dumps(config), encoding="utf-8")
            manager = ConfigManager(path)
            manager.load()
            root = logging.getLogger()
            saved = list(root.handlers)
            try:
                app_main.setup_logging(manager)
                rotating = [
                    h for h in root.handlers if isinstance(h, RotatingFileHandler)
                ]
                self.assertEqual(len(rotating), 1)
                self.assertEqual(rotating[0].maxBytes, 1024 * 1024)
                self.assertEqual(rotating[0].backupCount, 5)
            finally:
                for handler in list(root.handlers):
                    try:
                        handler.close()
                    except Exception:
                        pass
                root.handlers = saved


# ------------------------------------------------------------------ U1

@unittest.skipIf(QApplication is None, "PySide6 not installed")
class SingleInstanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_second_instance_is_blocked(self) -> None:
        from single_instance import SingleInstance

        with tempfile.TemporaryDirectory() as tmp:
            lock = Path(tmp) / "WiFi-Looking.lock"
            first = SingleInstance(lock, server_name="WiFi-Looking-test-blocked")
            self.assertTrue(first.try_acquire())
            try:
                second = SingleInstance(lock, server_name="WiFi-Looking-test-blocked")
                self.assertFalse(second.try_acquire())
            finally:
                first.release()
            third = SingleInstance(lock, server_name="WiFi-Looking-test-blocked")
            self.assertTrue(third.try_acquire())
            third.release()

    def test_notify_wakes_first_instance(self) -> None:
        from single_instance import SingleInstance

        with tempfile.TemporaryDirectory() as tmp:
            lock = Path(tmp) / "WiFi-Looking.lock"
            first = SingleInstance(lock, server_name="WiFi-Looking-test-wake")
            self.assertTrue(first.try_acquire())
            try:
                woken = []
                first.show_requested.connect(lambda: woken.append(True))
                second = SingleInstance(lock, server_name="WiFi-Looking-test-wake")
                self.assertTrue(second.notify_existing())
                deadline = time.time() + 3.0
                while time.time() < deadline and not woken:
                    self.app.processEvents()
                    time.sleep(0.02)
                self.assertEqual(woken, [True])
            finally:
                first.release()

    def test_crash_leftover_lock_does_not_block_next_launch(self) -> None:
        # A previous process that died without unlocking (simulated
        # by a child that os._exit()s while holding the lock) must
        # not block the next launch.
        from single_instance import SingleInstance

        with tempfile.TemporaryDirectory() as tmp:
            lock = Path(tmp) / "WiFi-Looking.lock"
            code = (
                "from PySide6.QtCore import QLockFile\n"
                f"lf = QLockFile({str(lock)!r})\n"
                "assert lf.tryLock(100)\n"
                "import os\n"
                "os._exit(0)\n"
            )
            proc = subprocess.run(
                [sys.executable, "-c", code],
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertTrue(lock.exists())
            guard = SingleInstance(lock, server_name="WiFi-Looking-test-stale")
            self.assertTrue(guard.try_acquire())
            guard.release()


# ------------------------------------------------------- GUI review fixes

@unittest.skipIf(QApplication is None, "PySide6 not installed")
class GuiReviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def _make_window(self, ssid):
        import main_window as main_window_module
        from config import DEFAULT_CONFIG
        from main_window import MainWindow
        from wifi_monitor import WifiState

        tmp = tempfile.TemporaryDirectory()
        config_path = Path(tmp.name) / "config.json"
        config_path.write_text(
            json.dumps(copy.deepcopy(DEFAULT_CONFIG), ensure_ascii=False),
            encoding="utf-8",
        )
        manager = ConfigManager(config_path)
        manager.load()

        from PySide6.QtWidgets import QMessageBox

        dialogs = []
        orig = {
            name: getattr(QMessageBox, name)
            for name in ("warning", "information", "critical")
        }
        for name in orig:
            setattr(
                QMessageBox,
                name,
                staticmethod(lambda *a, _n=name, **k: dialogs.append((_n, a))),
            )
        orig_auto = MainWindow._auto_test_clashmi
        MainWindow._auto_test_clashmi = lambda self: None

        class Monitor:
            def __init__(self, state):
                self.current_state = state

        state = (
            WifiState(connected=bool(ssid), ssid=ssid, interface_name="WLAN",
                      state_text="connected" if ssid else "disconnected")
            if ssid is not None
            else None
        )
        window = MainWindow(manager, Monitor(state))
        return tmp, manager, window, dialogs, orig, orig_auto, main_window_module

    def _teardown(self, tmp, window, orig, orig_auto) -> None:
        from PySide6.QtWidgets import QMessageBox

        window.close()
        window.deleteLater()
        self.app.processEvents()
        from main_window import MainWindow

        MainWindow._auto_test_clashmi = orig_auto
        for name, fn in orig.items():
            setattr(QMessageBox, name, fn)
        tmp.cleanup()

    def test_full_action_sequence_duplicate_detection(self) -> None:
        # bug 10: kill_process as the SECOND action still counts as a
        # duplicate of the one-click pair.
        tmp, manager, window, dialogs, orig, orig_auto, _ = self._make_window("Xiaomi 14")
        try:
            manager.set_rules([
                _rule("Xiaomi 14", ["clashmi.close_connections", "clashmi.kill_process"],
                      event="on_disconnect"),
                _rule("Xiaomi 14", ["clashmi.close_connections", "clashmi.kill_process"],
                      event="on_switch"),
            ])
            # The switch rule above lacks current_ssid="*"; fix it to
            # the exact one-click shape.
            manager.get_rules()  # copy only; stored rules edited below
            stored = manager._config["rules"]
            stored[1]["when"]["current_ssid"] = "*"
            added, skipped = window.add_rules_for_current_ssid("Xiaomi 14")
            self.assertEqual((added, skipped), (0, 2))
        finally:
            self._teardown(tmp, window, orig, orig_auto)

    def test_quick_add_state_not_ready_message(self) -> None:
        # bug 22
        tmp, manager, window, dialogs, orig, orig_auto, _ = self._make_window(None)
        try:
            window.monitor.current_state = None
            window._quick_add_current_wifi()
            infos = [a for name, a in dialogs if name == "information"]
            self.assertTrue(infos)
            self.assertIn("尚未读取完成", str(infos[-1]))
        finally:
            self._teardown(tmp, window, orig, orig_auto)

    def test_guard_waits_for_first_state(self) -> None:
        # bug 7: the guard must not silently give up when the first
        # state has not arrived yet at check time.
        import main_window as main_window_module
        from wifi_monitor import WifiState

        tmp, manager, window, dialogs, orig, orig_auto, _ = self._make_window(None)
        try:
            from PySide6.QtWidgets import QMessageBox

            warnings = []
            orig_warning = QMessageBox.warning
            QMessageBox.warning = staticmethod(
                lambda *a, **k: warnings.append(a)
            )
            window.monitor.current_state = None

            def bring_up():
                time.sleep(0.3)
                window.monitor.current_state = WifiState(
                    connected=True, ssid="CafeWiFi", interface_name="WLAN",
                    state_text="connected",
                )

            import threading

            threading.Thread(target=bring_up, daemon=True).start()
            fake_client = types.SimpleNamespace(is_active=lambda: True)
            with mock.patch.object(
                main_window_module, "ClashMiClient", lambda: fake_client
            ):
                window._run_startup_guard_check()
                deadline = time.time() + 5.0
                while time.time() < deadline:
                    self.app.processEvents()
                    if warnings:
                        break
                    time.sleep(0.02)
                self.app.processEvents()
            self.assertEqual(len(warnings), 1)
            QMessageBox.warning = orig_warning
        finally:
            self._teardown(tmp, window, orig, orig_auto)

    def test_target_dropdown_offers_wildcard(self) -> None:
        # optimization 4
        tmp, manager, window, dialogs, orig, orig_auto, _ = self._make_window("Xiaomi 14")
        try:
            window._apply_ssid_candidates(["Xiaomi 14", "Cafe"])
            items = [
                window.target_edit.itemText(i)
                for i in range(window.target_edit.count())
            ]
            self.assertIn("*", items)
            self.assertEqual(items[0], "*")
        finally:
            self._teardown(tmp, window, orig, orig_auto)

    def test_edit_replaces_rule_dict(self) -> None:
        # pre-check item 6
        from PySide6.QtCore import Qt

        tmp, manager, window, dialogs, orig, orig_auto, _ = self._make_window("Xiaomi 14")
        try:
            from main_window import ACTION_CLOSE_CONNECTIONS, EVENT_DISCONNECT

            window.event_combo.setCurrentText("断开 Wi-Fi 时")
            window.source_edit.setText("Library")
            window.action_combo.setCurrentText("断开 ClashMi 连接")
            window._add_rule()
            before = manager.get_rules()[0]
            window.table.selectRow(0)
            item = window.table.item(0, 0)
            item.setCheckState(Qt.CheckState.Unchecked)
            after = manager.get_rules()[0]
            self.assertIsNot(before, after)
            self.assertFalse(after["enabled"])
        finally:
            self._teardown(tmp, window, orig, orig_auto)


# ------------------------------------------------------- installer checks

class InstallerReviewTests(unittest.TestCase):
    def test_installer_script_guards(self) -> None:
        iss = Path(__file__).resolve().parent.parent / "installer.iss"
        raw = iss.read_bytes()
        self.assertTrue(raw.startswith(b"\xef\xbb\xbf"))  # bug 17: UTF-8 BOM
        text = raw.decode("utf-8-sig")
        for needle in (
            "UsePreviousAppDir=yes",  # opt 11
            "ArchitecturesAllowed=x64compatible",  # bug 29
            "VersionInfoVersion",  # bug 29
            "chinesesimplified",  # bug 17
            "dist\\config.json",  # bug 15
            # P0-8：自启统一走 Task Scheduler，卸载时清理任务计划。
            "[UninstallRun]",
            "/DELETE /TN XiaoMaLink /F",
        ):
            self.assertIn(needle, text)
        # P0-8：安装器不再提供启动目录快捷方式自启。
        self.assertNotIn("{userstartup}", text)

    def test_code_version_matches_installer_version(self) -> None:
        # bug 29 / pre-check 9
        import re

        from config import APP_VERSION

        iss = (Path(__file__).resolve().parent.parent / "installer.iss").read_text(
            encoding="utf-8-sig"
        )
        match = re.search(r'#define MyAppVersion "([^"]+)"', iss)
        self.assertIsNotNone(match)
        self.assertEqual(match.group(1), APP_VERSION)


if __name__ == "__main__":
    unittest.main()
