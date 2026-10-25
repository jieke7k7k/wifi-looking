"""P0-8：打包改名与自启清理的可离线验证测试（不实际打包）。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


class TestPackagingNamesP0(unittest.TestCase):
    def test_build_exe_name_is_xiaomalink(self):
        build_py = (ROOT / "build.py").read_text(encoding="utf-8")
        self.assertIn('"--name",\n        "XiaoMaLink",', build_py)
        self.assertNotIn('"--name",\n        "WiFi-Looking",', build_py)

    def test_installer_uses_xiaomalink_exe_publisher_and_dir(self):
        iss = (ROOT / "installer.iss").read_text(encoding="utf-8-sig")
        self.assertIn('#define MyAppPublisher "XiaoMaLink"', iss)
        self.assertIn('#define MyAppExeName "XiaoMaLink.exe"', iss)
        self.assertIn(r"DefaultDirName={userpf}\XiaoMaLink", iss)

    def test_installer_no_startup_folder_autostart(self):
        iss = (ROOT / "installer.iss").read_text(encoding="utf-8-sig")
        self.assertNotIn("{userstartup}", iss)
        self.assertNotIn('Name: "autostart"', iss)

    def test_installer_uninstall_deletes_task_scheduler_task(self):
        iss = (ROOT / "installer.iss").read_text(encoding="utf-8-sig")
        self.assertIn("[UninstallRun]", iss)
        self.assertIn("/DELETE /TN XiaoMaLink /F", iss)

    def test_task_name_matches_installer_cleanup(self):
        from autostart import DEFAULT_TASK_NAME

        self.assertEqual(DEFAULT_TASK_NAME, "XiaoMaLink")
        iss = (ROOT / "installer.iss").read_text(encoding="utf-8-sig")
        self.assertIn(f"/DELETE /TN {DEFAULT_TASK_NAME} /F", iss)


if __name__ == "__main__":
    unittest.main()
