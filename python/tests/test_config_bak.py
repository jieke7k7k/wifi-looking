"""Tests for the config.json.bak mirror (XiaoMaLink v1.0.0, spec 4.1).

Covers: save() writes an identical .bak, restoring a damaged main
config from the backup, no-backup / illegal-backup returning False
with the original file untouched, backup failure never failing
save(), and last_load_error tracking on load failure.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import DEFAULT_CONFIG, ConfigError, ConfigManager

VALID_RULE = {
    "id": "test-rule",
    "enabled": True,
    "description": "test",
    "when": {"event": "on_disconnect", "previous_ssid": ["Xiaomi 14"]},
    "actions": [{"type": "clashmi.close_connections"}],
    "cooldown_seconds": 10,
}


def _write_config(path: Path, rules) -> None:
    config = json.loads(json.dumps(DEFAULT_CONFIG))
    config["rules"] = rules
    path.write_text(json.dumps(config), encoding="utf-8")


class SaveWritesBakTests(unittest.TestCase):
    def test_save_writes_identical_bak(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            manager = ConfigManager(path)
            manager.load()
            manager.set_rules([VALID_RULE])
            manager.save()

            self.assertEqual(manager.backup_path, Path(tmp) / "config.json.bak")
            self.assertTrue(manager.backup_exists())
            self.assertEqual(
                manager.backup_path.read_text(encoding="utf-8"),
                path.read_text(encoding="utf-8"),
            )
            # The backup itself parses and carries the rule.
            backup = json.loads(manager.backup_path.read_text(encoding="utf-8"))
            self.assertEqual(backup["rules"], [VALID_RULE])

    def test_save_leaves_no_temp_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            manager = ConfigManager(path)
            manager.load()
            manager.save()
            leftovers = [p.name for p in Path(tmp).iterdir() if p.suffix == ".tmp"]
            self.assertEqual(leftovers, [])


class RestoreFromBackupTests(unittest.TestCase):
    def test_damaged_main_config_restored_from_backup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            manager = ConfigManager(path)
            manager.load()
            manager.set_rules([VALID_RULE])
            manager.save()
            backup_text = manager.backup_path.read_text(encoding="utf-8")

            # Main config gets damaged after the backup was made.
            path.write_text("{ this is not json", encoding="utf-8")
            self.assertTrue(manager.restore_from_backup())

            self.assertEqual(path.read_text(encoding="utf-8"), backup_text)
            self.assertEqual(manager.get_rules(), [VALID_RULE])
            # And it loads cleanly afterwards.
            self.assertEqual(ConfigManager(path).load()["rules"], [VALID_RULE])

    def test_no_backup_returns_false_and_leaves_file_untouched(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            _write_config(path, [VALID_RULE])
            before = path.read_text(encoding="utf-8")
            manager = ConfigManager(path)
            self.assertFalse(manager.backup_exists())
            self.assertFalse(manager.restore_from_backup())
            self.assertEqual(path.read_text(encoding="utf-8"), before)

    def test_illegal_backup_returns_false_and_leaves_file_untouched(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            _write_config(path, [VALID_RULE])
            before = path.read_text(encoding="utf-8")
            manager = ConfigManager(path)
            manager.backup_path.write_text("{ not json", encoding="utf-8")
            self.assertFalse(manager.restore_from_backup())
            self.assertEqual(path.read_text(encoding="utf-8"), before)

    def test_non_object_backup_returns_false_and_leaves_file_untouched(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            _write_config(path, [VALID_RULE])
            before = path.read_text(encoding="utf-8")
            manager = ConfigManager(path)
            manager.backup_path.write_text("[1, 2, 3]", encoding="utf-8")
            self.assertFalse(manager.restore_from_backup())
            self.assertEqual(path.read_text(encoding="utf-8"), before)


class BackupFailureTests(unittest.TestCase):
    def test_backup_failure_does_not_fail_save(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            manager = ConfigManager(path)
            manager.load()
            # Block the backup path with a directory: the atomic
            # replace onto it fails, which must only warn.
            manager.backup_path.mkdir(exist_ok=True)
            manager.set_rules([VALID_RULE])
            manager.save()  # must not raise
            self.assertEqual(
                ConfigManager(path).load()["rules"], [VALID_RULE]
            )


class LastLoadErrorTests(unittest.TestCase):
    def test_failed_load_records_error_and_success_clears_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text("{ this is not json", encoding="utf-8")
            manager = ConfigManager(path)
            with self.assertRaises(ConfigError):
                manager.load()
            self.assertIsNotNone(manager.last_load_error)
            self.assertIn("Invalid JSON", manager.last_load_error)

            # Repair the file; a successful load clears the error.
            _write_config(path, [])
            manager.load()
            self.assertIsNone(manager.last_load_error)

    def test_fresh_manager_has_no_load_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manager = ConfigManager(Path(tmp) / "config.json")
            self.assertIsNone(manager.last_load_error)


if __name__ == "__main__":
    unittest.main()
