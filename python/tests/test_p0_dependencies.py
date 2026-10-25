"""P0-2：系统动作依赖与能力自检的可离线验证测试。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


class TestSystemActionDependenciesP0(unittest.TestCase):
    def test_windows_requirements_declare_core_backends(self):
        path = ROOT / "requirements-windows.txt"
        self.assertTrue(path.exists(), "缺少 requirements-windows.txt")
        text = path.read_text(encoding="utf-8")
        for package in ("winsdk", "WMI", "monitorcontrol", "pycaw", "comtypes"):
            self.assertIn(package, text)

    def test_core_requirements_reference_windows_file(self):
        text = (ROOT / "requirements.txt").read_text(encoding="utf-8")
        self.assertIn("requirements-windows.txt", text)

    def test_build_declares_hidden_imports_for_lazy_modules(self):
        text = (ROOT / "build.py").read_text(encoding="utf-8")
        for module in (
            "winsdk.windows.devices.radios",
            "wmi",
            "monitorcontrol",
            "pycaw.pycaw",
            "comtypes",
        ):
            self.assertIn(f'"{module}"', text)
        self.assertIn("--hidden-import", text)

    def test_capability_report_covers_all_actions_and_marks_unsupported(self):
        from system_actions import ACTION_TYPES, capability_report

        report = capability_report()
        for action_type in ACTION_TYPES:
            self.assertIn(action_type, report)
        # 无公开接口的两个动作必须明确 unsupported。
        self.assertIn("audio.device.set: unsupported", report)
        self.assertIn("dnd.set: unsupported", report)


if __name__ == "__main__":
    unittest.main()
