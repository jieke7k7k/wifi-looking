"""Config robustness tests: atomic save, logging field validation and
startup recovery from a corrupt config.json.
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

try:
    import main as app_main
except ImportError:  # pragma: no cover
    app_main = None


class LoggingValidationTests(unittest.TestCase):
    def load_with_logging(self, logging_cfg) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            config = json.loads(json.dumps(DEFAULT_CONFIG))
            config["logging"] = logging_cfg
            path.write_text(json.dumps(config), encoding="utf-8")
            ConfigManager(path).load()

    def test_logging_file_must_be_string(self) -> None:
        with self.assertRaises(ConfigError):
            self.load_with_logging({"level": "INFO", "file": 123})

    def test_logging_level_must_be_string(self) -> None:
        with self.assertRaises(ConfigError):
            self.load_with_logging({"level": 3, "file": "logs/app.log"})

    def test_valid_logging_passes(self) -> None:
        self.load_with_logging({"level": "DEBUG", "file": "logs/app.log"})


class AtomicSaveTests(unittest.TestCase):
    def test_save_roundtrips_and_leaves_no_temp_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            manager = ConfigManager(path)
            manager.load()
            manager.set_rules([])
            manager.save()

            leftovers = [p.name for p in Path(tmp).iterdir() if p.suffix == ".tmp"]
            self.assertEqual(leftovers, [])
            reloaded = ConfigManager(path)
            reloaded.load()
            self.assertEqual(reloaded.get_rules(), [])


@unittest.skipIf(app_main is None, "PySide6 not installed")
class CorruptConfigRecoveryTests(unittest.TestCase):
    def test_corrupt_json_falls_back_to_defaults_and_backs_up(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text("{ this is not json", encoding="utf-8")

            manager = app_main.load_config_manager(path)

            self.assertEqual(manager.get_rules(), DEFAULT_CONFIG["rules"])
            backup = Path(tmp) / "config.json.corrupt"
            self.assertTrue(backup.exists())
            self.assertEqual(backup.read_text(encoding="utf-8"), "{ this is not json")
            # A fresh valid default config replaced the corrupt one.
            manager2 = ConfigManager(path)
            manager2.load()
            self.assertEqual(manager2.get_rules(), DEFAULT_CONFIG["rules"])

    def test_valid_config_loads_normally(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(json.dumps(DEFAULT_CONFIG), encoding="utf-8")
            manager = app_main.load_config_manager(path)
            self.assertEqual(len(manager.get_rules()), 2)
            self.assertFalse((Path(tmp) / "config.json.corrupt").exists())


if __name__ == "__main__":
    unittest.main()
