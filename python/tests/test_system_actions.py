"""系统动作模块测试。Windows API 一律 mock；Linux 下真实跑跨平台动作。"""

from __future__ import annotations

import subprocess
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import system_actions
from system_actions import detect_capabilities, execute_system_action


class TestLinuxCapabilities(unittest.TestCase):
    def test_windows_only_unsupported_on_linux(self):
        if sys.platform == "win32":
            self.skipTest("仅非 Windows 平台")
        caps = detect_capabilities()
        for name in (
            "wifi.set", "bluetooth.set", "brightness.set", "dark_mode.set",
            "wallpaper.set", "volume.set", "volume.mute", "power.plan.set",
            "screen.timeout.set", "dnd.set", "lock_screen", "sleep",
            "shutdown", "restart",
        ):
            self.assertFalse(caps[name]["supported"], name)
            self.assertIn("Windows", caps[name]["reason"])
        self.assertTrue(caps["program.launch"]["supported"])

    def test_doubtful_actions_always_unsupported(self):
        caps = detect_capabilities()
        self.assertFalse(caps["audio.device.set"]["supported"])
        self.assertFalse(caps["dnd.set"]["supported"])
        # 绝不伪造成功：即使在“Windows”模拟下也一样。
        with mock.patch.object(system_actions, "IS_WINDOWS", True):
            caps = detect_capabilities()
            self.assertFalse(caps["audio.device.set"]["supported"])
            self.assertFalse(caps["dnd.set"]["supported"])
            self.assertFalse(execute_system_action({"type": "audio.device.set", "device": "x"})["ok"])
            self.assertFalse(execute_system_action({"type": "dnd.set", "enabled": True})["ok"])

    def test_execute_on_linux_windows_action_fails_without_raising(self):
        result = execute_system_action({"type": "brightness.set", "value": 50})
        self.assertFalse(result["ok"])
        self.assertIsNotNone(result["error"])


class TestValidationNeverRaises(unittest.TestCase):
    def test_bad_inputs(self):
        cases = [
            None,
            "not-a-dict",
            {},
            {"type": ""},
            {"type": "nope.unknown"},
            {"type": "brightness.set", "value": 101},
            {"type": "brightness.set", "value": -1},
            {"type": "brightness.set"},
            {"type": "dark_mode.set", "mode": "blue"},
            {"type": "wallpaper.set", "path": "relative\\wall.jpg"},
            {"type": "volume.set", "value": 200},
            {"type": "volume.mute"},
            {"type": "screen.timeout.set", "minutes": -5},
            {"type": "program.launch"},
            {"type": "program.close"},
            {"type": "process.kill"},
        ]
        for action in cases:
            with self.subTest(action=action):
                result = execute_system_action(action)
                self.assertFalse(result["ok"])
                self.assertIn("ok", result)
                self.assertIn("error", result)

    def test_process_kill_by_name_requires_path_filter(self):
        result = execute_system_action({"type": "process.kill", "name": "sleep"})
        self.assertFalse(result["ok"])
        self.assertIn("path_contains", result["error"])

    def test_dry_run_does_not_execute(self):
        result = execute_system_action(
            {"type": "program.launch", "path": sys.executable, "args": ["-c", "pass"]},
            dry_run=True,
        )
        self.assertTrue(result["ok"])
        self.assertTrue(result["details"]["dry_run"])
        self.assertNotIn("pid", result["details"])


class TestReadbackVerification(unittest.TestCase):
    """亮度/音量读回验证逻辑：模拟 Windows 后端，Linux 下也可测。"""

    def _windows_env(self):
        return (
            mock.patch.object(system_actions, "IS_WINDOWS", True),
            mock.patch.object(
                system_actions, "_spec_available",
                lambda name: name in ("wmi", "monitorcontrol", "pycaw"),
            ),
        )

    def test_brightness_readback_match(self):
        patches = self._windows_env()
        with patches[0], patches[1], \
                mock.patch.object(system_actions, "_wmi_set_brightness") as set_fn, \
                mock.patch.object(system_actions, "_wmi_get_brightness", return_value=80):
            result = execute_system_action({"type": "brightness.set", "value": 80})
            set_fn.assert_called_once_with(80)
            self.assertTrue(result["ok"])
            self.assertEqual(result["details"]["readback"], 80)

    def test_brightness_readback_mismatch_is_failure(self):
        patches = self._windows_env()
        with patches[0], patches[1], \
                mock.patch.object(system_actions, "_wmi_set_brightness"), \
                mock.patch.object(system_actions, "_wmi_get_brightness", return_value=30):
            result = execute_system_action({"type": "brightness.set", "value": 80})
            self.assertFalse(result["ok"])
            self.assertIn("读回不符", result["error"])

    def test_volume_readback_mismatch_is_failure(self):
        patches = self._windows_env()
        with patches[0], patches[1], \
                mock.patch.object(system_actions, "_volume_set_scalar"), \
                mock.patch.object(system_actions, "_volume_get_scalar", return_value=0.3):
            result = execute_system_action({"type": "volume.set", "value": 80})
            self.assertFalse(result["ok"])
            self.assertIn("读回不符", result["error"])

    def test_volume_mute_readback(self):
        patches = self._windows_env()
        with patches[0], patches[1], \
                mock.patch.object(system_actions, "_volume_set_mute") as set_fn, \
                mock.patch.object(system_actions, "_volume_get_mute", return_value=True):
            result = execute_system_action({"type": "volume.mute", "muted": True})
            set_fn.assert_called_once_with(True)
            self.assertTrue(result["ok"])


class TestPowerPlanEnumeration(unittest.TestCase):
    FAKE_GUID = "11111111-2222-3333-4444-555555555555"

    def _fake_run(self, argv, timeout=10):
        if argv[:2] == ["powercfg", "/list"]:
            return SimpleNamespace(
                returncode=0,
                stdout=f"现有电源使用方案 (* Active)\nGUID: {self.FAKE_GUID}  (测试计划)\n",
                stderr="",
            )
        if argv[:2] == ["powercfg", "/setactive"]:
            self.seen_setactive = argv[2]
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if argv[:2] == ["powercfg", "/getactivescheme"]:
            return SimpleNamespace(returncode=0, stdout=f"GUID: {self.FAKE_GUID} (测试计划)", stderr="")
        raise AssertionError(f"unexpected argv {argv}")

    def test_guid_comes_from_enumeration_not_hardcoded(self):
        self.seen_setactive = None
        with mock.patch.object(system_actions, "IS_WINDOWS", True), \
                mock.patch.object(system_actions.shutil, "which", return_value="powercfg"), \
                mock.patch.object(system_actions, "_run_cmd", side_effect=self._fake_run):
            result = execute_system_action({"type": "power.plan.set", "plan": "测试计划"})
            self.assertTrue(result["ok"], result)
            # setactive 用的 GUID 必须就是枚举输出里的那一个。
            self.assertEqual(self.seen_setactive, self.FAKE_GUID)
            self.assertEqual(result["details"]["guid"], self.FAKE_GUID)

    def test_unknown_plan_fails(self):
        with mock.patch.object(system_actions, "IS_WINDOWS", True), \
                mock.patch.object(system_actions.shutil, "which", return_value="powercfg"), \
                mock.patch.object(system_actions, "_run_cmd", side_effect=self._fake_run):
            result = execute_system_action({"type": "power.plan.set", "plan": "不存在的计划"})
            self.assertFalse(result["ok"])


class TestCrossPlatformProcessActions(unittest.TestCase):
    def test_program_launch_real(self):
        result = execute_system_action(
            {"type": "program.launch", "path": sys.executable, "args": ["-c", "pass"]}
        )
        self.assertTrue(result["ok"], result)
        self.assertIsInstance(result["details"]["pid"], int)

    def test_process_kill_by_pid_and_name_filter(self):
        proc = subprocess.Popen(["sleep", "60"])
        try:
            # 按名无路径过滤必须拒绝，且进程必须还活着。
            denied = execute_system_action({"type": "process.kill", "name": "sleep"})
            self.assertFalse(denied["ok"])
            self.assertIsNone(proc.poll())
            # 按 pid 杀成功。
            killed = execute_system_action({"type": "process.kill", "pid": proc.pid})
            self.assertTrue(killed["ok"], killed)
            proc.wait(timeout=5)
        finally:
            if proc.poll() is None:
                proc.kill()

    def test_program_close_graceful(self):
        proc = subprocess.Popen(["sleep", "60"])
        try:
            result = execute_system_action(
                {"type": "program.close", "pid": proc.pid, "graceful": True}
            )
            self.assertTrue(result["ok"], result)
            proc.wait(timeout=5)
        finally:
            if proc.poll() is None:
                proc.kill()


if __name__ == "__main__":
    unittest.main()
