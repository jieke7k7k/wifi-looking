"""Tests for autostart: schtasks argument building + platform gate."""

from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from autostart import build_schtasks_args, is_registered, register, unregister


def _completed(returncode, stdout="", stderr=""):
    return types.SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


class BuildArgsTests(unittest.TestCase):
    def test_exact_schtasks_shape(self):
        args = build_schtasks_args(r"C:\Program Files\XiaoMaLink\XiaoMaLink.exe")
        self.assertEqual(
            args,
            [
                "schtasks",
                "/CREATE",
                "/TN",
                "XiaoMaLink",
                "/TR",
                r'"C:\Program Files\XiaoMaLink\XiaoMaLink.exe" --minimized',
                "/SC",
                "ONLOGON",
                "/RL",
                "HIGHEST",
                "/F",
            ],
        )

    def test_custom_task_name_and_plain_path_quoted(self):
        args = build_schtasks_args(r"C:\Apps\XiaoMaLink.exe", task_name="Custom")
        self.assertEqual(args[3], "Custom")
        self.assertEqual(args[5], r'"C:\Apps\XiaoMaLink.exe" --minimized')

    def test_pure_function_no_subprocess(self):
        # build_schtasks_args must not touch the system; calling it
        # twice yields equal, independent lists.
        first = build_schtasks_args("C:\\a.exe")
        second = build_schtasks_args("C:\\a.exe")
        self.assertEqual(first, second)
        self.assertIsNot(first, second)


class PlatformGateTests(unittest.TestCase):
    def test_register_non_windows_is_unsupported_not_success(self):
        result = register(r"C:\Apps\XiaoMaLink.exe")
        if sys.platform != "win32":
            self.assertFalse(result["ok"])
            self.assertEqual(result["status"], "unsupported")

    def test_unregister_non_windows_is_unsupported_not_success(self):
        result = unregister()
        if sys.platform != "win32":
            self.assertFalse(result["ok"])
            self.assertEqual(result["status"], "unsupported")

    def test_is_registered_non_windows_is_unknown_not_false(self):
        if sys.platform != "win32":
            self.assertIsNone(is_registered())


class RunnerSeamTests(unittest.TestCase):
    def test_register_uses_built_args_and_reports_success(self):
        seen = []

        def runner(args):
            seen.append(args)
            return _completed(0)

        result = register(r"C:\Apps\XiaoMaLink.exe", runner=runner)
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "registered")
        self.assertEqual(seen[0], build_schtasks_args(r"C:\Apps\XiaoMaLink.exe"))

    def test_register_failure_reports_returncode(self):
        result = register("C:\\a.exe", runner=lambda args: _completed(1, stderr="denied"))
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["returncode"], 1)

    def test_unregister_and_query_shapes(self):
        seen = []
        unregister(runner=lambda args: (seen.append(args), _completed(0))[1])
        self.assertEqual(seen[0], ["schtasks", "/DELETE", "/TN", "XiaoMaLink", "/F"])
        self.assertTrue(is_registered(runner=lambda args: _completed(0)))
        self.assertFalse(is_registered(runner=lambda args: _completed(1)))


if __name__ == "__main__":
    unittest.main()
