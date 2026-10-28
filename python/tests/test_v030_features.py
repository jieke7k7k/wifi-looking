"""Headless tests for the v0.3.0 rework (R1–R4).

R1: SSID candidate parsing (netsh networks/profiles, EN + CN samples).
R2: clashmi.kill_process action semantics with taskkill mocked.
R3: default config has no rules; the v0.2.0 factory rule set is
    migrated away only when it is completely untouched.
R4: startup guard decision function and ClashMiClient.is_active.
Plus GUI tests for the one-click add and the guard flow.
"""

from __future__ import annotations

import copy
import json
import os
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import clashmi_close
import wifi_monitor
from action_executor import CLASHMI_PROCESS_NAMES, ActionExecutor
from clashmi import ClashMiClient, ClashMiError, ClashMiSettings
from config import LEGACY_DEFAULT_RULES, ConfigManager
from wifi_monitor import (
    collect_ssid_candidates,
    parse_saved_profile_names,
    parse_visible_ssids,
)


# -- R1: parsing ----------------------------------------------------------

NETWORKS_EN = """\
Interface name : Wi-Fi
There are 3 networks currently visible.

SSID 1 : Xiaomi 14
    Network type            : Infrastructure
    Authentication          : WPA2-Personal
    Encryption              : CCMP
    BSSID 1                 : aa:bb:cc:dd:ee:ff

SSID 2 : 15-225-5G
    Network type            : Infrastructure
    Authentication          : WPA2-Personal
    Encryption              : CCMP
    BSSID 1                 : 11:22:33:44:55:66

SSID 3 :
    Network type            : Infrastructure
    Authentication          : Open
"""

NETWORKS_CN = """\
接口名称 : WLAN
当前有 2 个网络可见。

SSID 1 : Xiaomi 14
    网络类型                : 基础结构
    身份验证                : WPA2 - 个人
    加密                    : CCMP

SSID 2 : 15-225-2.4G
    网络类型                : 基础结构
    身份验证                : WPA2 - 个人
    加密                    : CCMP
"""

PROFILES_EN = """\
Profiles on interface Wi-Fi:

Group policy profiles (read only)
---------------------------------
    <None>

User profiles
-------------
    All User Profile     : Xiaomi 14
    All User Profile     : 15-225-5G
    Current User Profile : DormNet
"""

PROFILES_CN = """\
接口 WLAN 上的配置文件:

组策略配置文件(只读)
-------------------
    <无>

用户配置文件
------------
    所有用户配置文件 : Xiaomi 14
    所有用户配置文件 : 15-225-5G
"""


class ParseNetworksTests(unittest.TestCase):
    def test_english_networks(self) -> None:
        self.assertEqual(parse_visible_ssids(NETWORKS_EN), ["Xiaomi 14", "15-225-5G"])

    def test_chinese_networks(self) -> None:
        self.assertEqual(parse_visible_ssids(NETWORKS_CN), ["Xiaomi 14", "15-225-2.4G"])

    def test_bssid_lines_do_not_match(self) -> None:
        self.assertNotIn("aa:bb:cc:dd:ee:ff", parse_visible_ssids(NETWORKS_EN))

    def test_empty_output(self) -> None:
        self.assertEqual(parse_visible_ssids(""), [])
        self.assertEqual(parse_visible_ssids("garbage without colons"), [])


class ParseProfilesTests(unittest.TestCase):
    def test_english_profiles(self) -> None:
        self.assertEqual(
            parse_saved_profile_names(PROFILES_EN),
            ["Xiaomi 14", "15-225-5G", "DormNet"],
        )

    def test_chinese_profiles(self) -> None:
        self.assertEqual(
            parse_saved_profile_names(PROFILES_CN),
            ["Xiaomi 14", "15-225-5G"],
        )

    def test_empty_output(self) -> None:
        self.assertEqual(parse_saved_profile_names(""), [])


class CollectCandidatesTests(unittest.TestCase):
    def test_order_and_dedupe(self) -> None:
        with mock.patch.object(
            wifi_monitor, "list_visible_ssids", return_value=["Xiaomi 14", "Cafe"]
        ), mock.patch.object(
            wifi_monitor,
            "list_saved_profile_names",
            return_value=["Cafe", "Home", "Xiaomi 14"],
        ):
            self.assertEqual(
                collect_ssid_candidates("Xiaomi 14"),
                ["Xiaomi 14", "Cafe", "Home"],
            )

    def test_no_current_ssid(self) -> None:
        with mock.patch.object(
            wifi_monitor, "list_visible_ssids", return_value=["Cafe"]
        ), mock.patch.object(wifi_monitor, "list_saved_profile_names", return_value=[]):
            self.assertEqual(collect_ssid_candidates(None), ["Cafe"])

    def test_fetch_failures_degrade_to_empty(self) -> None:
        # The real fetchers swallow netsh errors themselves; the
        # collector must still return just the current SSID when both
        # listings come back empty.
        with mock.patch.object(
            wifi_monitor, "list_visible_ssids", return_value=[]
        ), mock.patch.object(wifi_monitor, "list_saved_profile_names", return_value=[]):
            self.assertEqual(collect_ssid_candidates("Home"), ["Home"])


# -- R2: kill process action ----------------------------------------------
# v1.0.0: clashmi.kill_process 走四步原子关闭 close_clashmi_atomic()，
# 返回 {ok, steps, details, killed}；mock clashmi_close 内部的进程快照
# 与进程树强杀（不再是旧版的 action_executor.subprocess.run）。

def _completed(returncode: int):
    return types.SimpleNamespace(returncode=returncode, stdout="", stderr="")


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
        "exe": exe or (r"C:\ClashMi\\" + name),
    }


class KillProcessActionTests(unittest.TestCase):
    def run_kill(self, snapshots, tree_kill):
        """snapshots：before/after/final 三次快照；tree_kill：强杀回调。"""
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

    def test_all_killed(self) -> None:
        # 主进程 + ClashMi 路径内核进程都被杀 -> ok，killed 含两者。
        snapshots = [
            [_proc("ClashMi.exe", 100), _proc("mihomo.exe", 101, ppid=100)],
            [_proc("ClashMi.exe", 100), _proc("mihomo.exe", 101, ppid=100)],
            [],
        ]
        result = self.run_kill(snapshots, lambda pid, name=None: (True, "killed"))
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["killed"], ["ClashMi.exe", "mihomo.exe"])
        self.assertIn("steps", result)
        self.assertIn("details", result)
        self.assertTrue(result["details"]["main_gone"])

    def test_taskkill_args_have_force_image_no_tree(self) -> None:
        # 需求 3.1 第 3 步：进程树强杀等同 taskkill /F /T。
        with mock.patch.object(clashmi_close, "IS_WINDOWS", True), \
                mock.patch.object(
                    clashmi_close.subprocess, "CREATE_NO_WINDOW", 0, create=True
                ), \
                mock.patch.object(
                    clashmi_close.subprocess, "run", return_value=_completed(0)
                ) as run:
            ok, _detail = clashmi_close.default_tree_kill(100)
        self.assertTrue(ok)
        argv = run.call_args.args[0]
        self.assertEqual(argv[0], "taskkill")
        self.assertIn("/F", argv)
        self.assertIn("/T", argv)
        self.assertIn("/PID", argv)

    def test_absent_process_counts_as_success(self) -> None:
        # 本来就没有 ClashMi 进程 -> ok，killed 为空。
        result = self.run_kill([[], [], []], lambda pid, name=None: (True, "killed"))
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["killed"], [])

    def test_other_failure_reported_not_raised(self) -> None:
        # 强杀返回失败（taskkill 返回非 0）绝不抛异常；主进程仍在 -> ok=False。
        snapshots = [
            [_proc("ClashMi.exe", 100)],
            [_proc("ClashMi.exe", 100)],
            [_proc("ClashMi.exe", 100)],
        ]
        result = self.run_kill(
            snapshots, lambda pid, name=None: (False, "taskkill 返回 1")
        )
        self.assertFalse(result["ok"])
        self.assertIn("failed", result)

    def test_taskkill_unavailable_does_not_raise(self) -> None:
        # 强杀抛异常（如 taskkill 不存在）绝不冒出 execute_action。
        def boom(pid, name=None):
            raise FileNotFoundError("taskkill")

        snapshots = [
            [_proc("ClashMi.exe", 100)],
            [_proc("ClashMi.exe", 100)],
            [_proc("ClashMi.exe", 100)],
        ]
        result = self.run_kill(snapshots, boom)
        self.assertFalse(result["ok"])

    def test_mixed_outcome(self) -> None:
        # 主进程杀掉、ClashMi 内核强杀失败残留 -> 部分失败，ok=False。
        def kill(pid, name=None):
            return (True, "killed") if pid == 100 else (False, "拒绝访问")

        snapshots = [
            [_proc("ClashMi.exe", 100), _proc("mihomo.exe", 101, ppid=100)],
            [_proc("ClashMi.exe", 100), _proc("mihomo.exe", 101, ppid=100)],
            [_proc("mihomo.exe", 101, ppid=100)],
        ]
        result = self.run_kill(snapshots, kill)
        self.assertFalse(result["ok"])
        self.assertTrue(result["details"]["main_gone"])
        self.assertFalse(result["details"]["kernel_gone"])
        self.assertEqual(result["killed"], ["ClashMi.exe"])

    def test_dry_run_calls_nothing(self) -> None:
        executor = ActionExecutor()
        with mock.patch("clashmi_close.close_clashmi_atomic") as close:
            result = executor.execute_action(
                {"type": "clashmi.kill_process"}, {}, dry_run=True
            )
        self.assertTrue(result["ok"])
        self.assertTrue(result["dry_run"])
        close.assert_not_called()


# -- R3: default rules + migration ------------------------------------------

def _write_config(tmp: str, rules) -> Path:
    path = Path(tmp) / "config.json"
    config = {
        "monitor": {"interval_seconds": 1.0, "debounce_count": 2},
        "logging": {"level": "INFO", "file": "logs/app.log"},
        "rules": rules,
    }
    path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
    return path


class MigrationTests(unittest.TestCase):
    def load_rules(self, rules):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_config(tmp, rules)
            manager = ConfigManager(path)
            loaded = manager.load()
            on_disk = json.loads(path.read_text(encoding="utf-8"))
            return loaded["rules"], on_disk["rules"]

    def test_default_config_ships_no_rules(self) -> None:
        from config import DEFAULT_CONFIG

        self.assertEqual(DEFAULT_CONFIG["rules"], [])

    def test_untouched_legacy_rules_are_cleared_and_persisted(self) -> None:
        rules, on_disk = self.load_rules(copy.deepcopy(LEGACY_DEFAULT_RULES))
        self.assertEqual(rules, [])
        self.assertEqual(on_disk, [])

    def test_legacy_rules_in_other_order_are_cleared(self) -> None:
        rules, _ = self.load_rules(list(reversed(copy.deepcopy(LEGACY_DEFAULT_RULES))))
        self.assertEqual(rules, [])

    def test_legacy_rules_with_renamed_ids_are_cleared(self) -> None:
        legacy = copy.deepcopy(LEGACY_DEFAULT_RULES)
        legacy[0]["id"] = "user-renamed-1"
        legacy[1]["id"] = "user-renamed-2"
        legacy[0]["description"] = "改了描述也不影响判定"
        rules, _ = self.load_rules(legacy)
        self.assertEqual(rules, [])

    def test_edited_cooldown_is_preserved(self) -> None:
        legacy = copy.deepcopy(LEGACY_DEFAULT_RULES)
        legacy[0]["cooldown_seconds"] = 30
        rules, _ = self.load_rules(legacy)
        self.assertEqual(len(rules), 2)

    def test_edited_action_is_preserved(self) -> None:
        legacy = copy.deepcopy(LEGACY_DEFAULT_RULES)
        legacy[1]["actions"] = [{"type": "clashmi.disable_tun"}]
        rules, _ = self.load_rules(legacy)
        self.assertEqual(len(rules), 2)

    def test_edited_ssid_is_preserved(self) -> None:
        legacy = copy.deepcopy(LEGACY_DEFAULT_RULES)
        legacy[0]["when"]["previous_ssid"] = ["Xms"]
        rules, _ = self.load_rules(legacy)
        self.assertEqual(len(rules), 2)

    def test_disabled_legacy_rule_is_preserved(self) -> None:
        legacy = copy.deepcopy(LEGACY_DEFAULT_RULES)
        legacy[0]["enabled"] = False
        rules, _ = self.load_rules(legacy)
        self.assertEqual(len(rules), 2)

    def test_legacy_plus_user_rule_is_preserved(self) -> None:
        # Whole-set migration only (review bug 19, per-rule removal
        # deliberately not adopted): a set that is not exactly the
        # factory set is preserved in full.
        legacy = copy.deepcopy(LEGACY_DEFAULT_RULES)
        legacy.append(
            {
                "id": "my-rule",
                "enabled": True,
                "when": {"event": "on_connect", "current_ssid": "Cafe"},
                "actions": [{"type": "clashmi.test_connection"}],
                "cooldown_seconds": 0,
            }
        )
        rules, _ = self.load_rules(legacy)
        self.assertEqual(len(rules), 3)

    def test_empty_rules_stay_empty(self) -> None:
        rules, on_disk = self.load_rules([])
        self.assertEqual(rules, [])
        self.assertEqual(on_disk, [])

    def test_kill_process_action_validates(self) -> None:
        rules = [
            {
                "id": "r1",
                "enabled": True,
                "when": {"event": "on_disconnect", "previous_ssid": "Home"},
                "actions": [{"type": "clashmi.kill_process"}],
                "cooldown_seconds": 10,
            }
        ]
        loaded, _ = self.load_rules(rules)
        self.assertEqual(len(loaded), 1)


# -- R4: guard decision + is_active -----------------------------------------

try:
    from PySide6.QtWidgets import QApplication
except ImportError:  # pragma: no cover
    QApplication = None

if QApplication is not None:
    from main_window import ssid_covered_by_rules, startup_guard_should_warn


def _rule(sources, enabled=True, event="on_disconnect"):
    return {
        "id": "r",
        "enabled": enabled,
        "when": {"event": event, "previous_ssid": sources},
        "actions": [{"type": "clashmi.kill_process"}],
        "cooldown_seconds": 10,
    }


@unittest.skipIf(QApplication is None, "PySide6 not installed")
class GuardDecisionTests(unittest.TestCase):
    def test_uncovered_ssid_with_clashmi_on_warns(self) -> None:
        self.assertTrue(
            startup_guard_should_warn("Cafe", [_rule("Xiaomi 14")], True)
        )

    def test_covered_ssid_is_silent(self) -> None:
        rules = [_rule(["Xiaomi 14", "Home"])]
        self.assertFalse(startup_guard_should_warn("Home", rules, True))
        self.assertTrue(ssid_covered_by_rules("Xiaomi 14", rules))

    def test_wildcard_source_covers_everything(self) -> None:
        self.assertFalse(startup_guard_should_warn("Cafe", [_rule("*")], True))

    def test_disabled_rule_does_not_cover(self) -> None:
        rules = [_rule("Cafe", enabled=False)]
        self.assertTrue(startup_guard_should_warn("Cafe", rules, True))

    def test_not_connected_is_silent(self) -> None:
        self.assertFalse(startup_guard_should_warn(None, [], True))
        self.assertFalse(startup_guard_should_warn("", [], True))

    def test_clashmi_off_is_silent(self) -> None:
        self.assertFalse(startup_guard_should_warn("Cafe", [], False))


class ClashMiActiveTests(unittest.TestCase):
    def make_client(self) -> ClashMiClient:
        return ClashMiClient(ClashMiSettings(base_url="http://127.0.0.1:9090"))

    def test_tun_enabled_means_active(self) -> None:
        client = self.make_client()
        client.get_configs = lambda: {"tun": {"enable": True}}
        self.assertTrue(client.is_active())

    def test_connections_mean_active(self) -> None:
        client = self.make_client()
        client.get_configs = lambda: {"tun": {"enable": False}}
        client.connection_count = lambda: 3
        self.assertTrue(client.is_active())

    def test_tun_off_and_no_connections_is_inactive(self) -> None:
        client = self.make_client()
        client.get_configs = lambda: {"tun": {"enable": False}}
        client.connection_count = lambda: 0
        self.assertFalse(client.is_active())

    def test_configs_failure_falls_back_to_connections(self) -> None:
        # Review fix (R4 note): a failed /configs query must not
        # short-circuit the verdict while connections are still live.
        client = self.make_client()

        def fail():
            raise ClashMiError("down")

        client.get_configs = fail
        client.connection_count = lambda: 5
        self.assertTrue(client.is_active())

    def test_count_failure_means_inactive(self) -> None:
        client = self.make_client()
        client.get_configs = lambda: {"mode": "Rule"}

        def fail():
            raise ClashMiError("down")

        client.connection_count = fail
        self.assertFalse(client.is_active())


# -- GUI: one-click add + guard flow -----------------------------------------

if QApplication is not None:
    import main_window as main_window_module
    from config import DEFAULT_CONFIG
    from main_window import (
        ACTION_KILL_PROCESS,
        EVENT_DISCONNECT,
        EVENT_SWITCH,
        MainWindow,
    )
    from wifi_monitor import WifiState


class _ConnectedMonitor:
    def __init__(self, ssid):
        self.current_state = WifiState(
            connected=bool(ssid),
            ssid=ssid,
            interface_name="WLAN",
            state_text="connected" if ssid else "disconnected",
        )


@unittest.skipIf(QApplication is None, "PySide6 not installed")
class QuickAddGuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        config_path = Path(self._tmp.name) / "config.json"
        config_path.write_text(
            json.dumps(copy.deepcopy(DEFAULT_CONFIG), ensure_ascii=False),
            encoding="utf-8",
        )
        self.config_manager = ConfigManager(config_path)
        self.config_manager.load()

        from PySide6.QtWidgets import QMessageBox

        self._dialogs = []
        self._orig_dialogs = {
            name: getattr(QMessageBox, name)
            for name in ("warning", "information", "critical")
        }
        for name in self._orig_dialogs:
            setattr(
                QMessageBox,
                name,
                staticmethod(lambda *a, _n=name, **k: self._dialogs.append((_n, a))),
            )

        self._orig_auto = MainWindow._auto_test_clashmi
        MainWindow._auto_test_clashmi = lambda self: None
        self.window = MainWindow(
            self.config_manager, _ConnectedMonitor("Xiaomi 14")
        )

    def tearDown(self) -> None:
        from PySide6.QtWidgets import QMessageBox

        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()
        MainWindow._auto_test_clashmi = self._orig_auto
        for name, fn in self._orig_dialogs.items():
            setattr(QMessageBox, name, fn)
        self._tmp.cleanup()

    def test_form_defaults_to_kill_process(self) -> None:
        # The single-action dropdown is gone; an empty form now falls
        # back to the same default (关闭 ClashMi / kill_process).
        self.assertEqual(
            self.window._read_actions(), [{"type": ACTION_KILL_PROCESS}]
        )

    def test_quick_add_creates_pair(self) -> None:
        self.window._quick_add_current_wifi()
        rules = self.config_manager.get_rules()
        self.assertEqual(len(rules), 2)
        by_event = {rule["when"]["event"]: rule for rule in rules}

        disconnect = by_event[EVENT_DISCONNECT]
        self.assertEqual(disconnect["when"]["previous_ssid"], "Xiaomi 14")
        self.assertNotIn("current_ssid", disconnect["when"])

        switch = by_event[EVENT_SWITCH]
        self.assertEqual(switch["when"]["previous_ssid"], "Xiaomi 14")
        self.assertEqual(switch["when"]["current_ssid"], "*")

        for rule in rules:
            self.assertTrue(rule["enabled"])
            self.assertEqual(
                [a["type"] for a in rule["actions"]], [ACTION_KILL_PROCESS]
            )
            self.assertEqual(rule["cooldown_seconds"], self.window.cooldown_spin.value())

    def test_quick_add_uses_form_cooldown(self) -> None:
        self.window.cooldown_spin.setValue(42)
        added, skipped = self.window.add_rules_for_current_ssid("Xiaomi 14")
        self.assertEqual((added, skipped), (2, 0))
        for rule in self.config_manager.get_rules():
            self.assertEqual(rule["cooldown_seconds"], 42)

    def test_quick_add_twice_skips_duplicates(self) -> None:
        self.window._quick_add_current_wifi()
        self.window._quick_add_current_wifi()
        self.assertEqual(len(self.config_manager.get_rules()), 2)
        infos = [a for name, a in self._dialogs if name == "information"]
        self.assertEqual(len(infos), 2)  # second click reported the skip

    def test_add_rules_reports_counts(self) -> None:
        self.assertEqual(
            self.window.add_rules_for_current_ssid("Xiaomi 14"), (2, 0)
        )
        self.assertEqual(
            self.window.add_rules_for_current_ssid("Xiaomi 14"), (0, 2)
        )
        self.assertEqual(
            self.window.add_rules_for_current_ssid("Other"), (2, 0)
        )

    def test_quick_add_disconnected_does_nothing(self) -> None:
        self.window.monitor = _ConnectedMonitor(None)
        self.window._quick_add_current_wifi()
        self.assertEqual(self.config_manager.get_rules(), [])


@unittest.skipIf(QApplication is None, "PySide6 not installed")
class StartupGuardGuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        config_path = Path(self._tmp.name) / "config.json"
        config_path.write_text(
            json.dumps(copy.deepcopy(DEFAULT_CONFIG), ensure_ascii=False),
            encoding="utf-8",
        )
        self.config_manager = ConfigManager(config_path)
        self.config_manager.load()

        from PySide6.QtWidgets import QMessageBox

        self.warnings = []
        self._orig_warning = QMessageBox.warning
        QMessageBox.warning = staticmethod(
            lambda *a, **k: self.warnings.append(a)
        )
        self._orig_dialogs = {
            name: getattr(QMessageBox, name) for name in ("information", "critical")
        }
        for name in self._orig_dialogs:
            setattr(QMessageBox, name, staticmethod(lambda *a, **k: None))

        self._orig_auto = MainWindow._auto_test_clashmi
        MainWindow._auto_test_clashmi = lambda self: None

    def tearDown(self) -> None:
        from PySide6.QtWidgets import QMessageBox

        QMessageBox.warning = self._orig_warning
        for name, fn in self._orig_dialogs.items():
            setattr(QMessageBox, name, fn)
        MainWindow._auto_test_clashmi = self._orig_auto
        self._tmp.cleanup()

    def _make_window(self, ssid, clashmi_active):
        window = MainWindow(self.config_manager, _ConnectedMonitor(ssid))
        fake_client = types.SimpleNamespace(is_active=lambda: clashmi_active)
        return window, fake_client

    def _run_guard(self, window, fake_client) -> None:
        # The patch must outlive the call: the probe runs on a worker
        # thread and only then emits the result signal.
        with mock.patch.object(
            main_window_module, "ClashMiClient", lambda: fake_client
        ):
            window._run_startup_guard_check()
            deadline = time.time() + 5.0
            while time.time() < deadline:
                self.app.processEvents()
                if self.warnings:
                    break
                time.sleep(0.02)
            self.app.processEvents()

    def test_guard_warns_on_uncovered_network(self) -> None:
        window, fake = self._make_window("CafeWiFi", True)
        try:
            self._run_guard(window, fake)
            self.assertEqual(len(self.warnings), 1)
        finally:
            window.close()
            window.deleteLater()

    def test_guard_silent_when_covered(self) -> None:
        self.config_manager.set_rules([_rule("CafeWiFi")])
        window, fake = self._make_window("CafeWiFi", True)
        try:
            self._run_guard(window, fake)
            time.sleep(0.3)
            self.app.processEvents()
            self.assertEqual(self.warnings, [])
        finally:
            window.close()
            window.deleteLater()

    def test_guard_silent_when_clashmi_off(self) -> None:
        window, fake = self._make_window("CafeWiFi", False)
        try:
            self._run_guard(window, fake)
            time.sleep(0.3)
            self.app.processEvents()
            self.assertEqual(self.warnings, [])
        finally:
            window.close()
            window.deleteLater()

    def test_start_guard_runs_only_once(self) -> None:
        window, _fake = self._make_window("CafeWiFi", True)
        try:
            window.start_startup_guard(delay_ms=60000)
            self.assertTrue(window._startup_guard_started)
            window.start_startup_guard(delay_ms=0)  # ignored: already started
            self.assertTrue(window._startup_guard_started)
        finally:
            window.close()
            window.deleteLater()


if __name__ == "__main__":
    unittest.main()
