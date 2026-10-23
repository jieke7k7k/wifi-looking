"""Path anchoring tests: config/logs must follow the app directory,
not the process working directory. Skipped without PySide6 (main.py
imports it for the GUI entry point).
"""

from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    import main as app_main
    from config import DEFAULT_CONFIG, ConfigManager
except ImportError:
    app_main = None


@unittest.skipIf(app_main is None, "PySide6 not installed")
class MainPathTests(unittest.TestCase):
    def test_parse_args_config_defaults_to_none(self) -> None:
        args = app_main.parse_args([])
        self.assertIsNone(args.config)

    def test_app_dir_is_python_dir_when_not_frozen(self) -> None:
        self.assertEqual(
            app_main.app_dir(),
            Path(app_main.__file__).resolve().parent,
        )

    def test_relative_log_path_anchored_to_config_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"
            config = json.loads(json.dumps(DEFAULT_CONFIG))
            config["logging"] = {"level": "INFO", "file": "logs/app.log"}
            config_path.write_text(json.dumps(config), encoding="utf-8")
            manager = ConfigManager(config_path)
            manager.load()
            try:
                app_main.setup_logging(manager)
                self.assertTrue((Path(tmp) / "logs" / "app.log").exists())
            finally:
                logging.shutdown()


if __name__ == "__main__":
    unittest.main()
