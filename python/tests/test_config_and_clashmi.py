"""Config validation and ClashMi settings helpers."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from clashmi import _normalize_base_url
from config import DEFAULT_CONFIG, ConfigManager


class ConfigTests(unittest.TestCase):
    def test_default_config_loads_and_validates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(json.dumps(DEFAULT_CONFIG), encoding="utf-8")
            manager = ConfigManager(path)
            loaded = manager.load()
            # v0.3.0: the default config ships no preset rules.
            self.assertEqual(len(loaded["rules"]), 0)

    def test_missing_config_is_created_from_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            manager = ConfigManager(path)
            manager.load()
            self.assertTrue(path.exists())


class ClashMiUrlTests(unittest.TestCase):
    def test_bare_host_port_gets_http_prefix(self) -> None:
        self.assertEqual(_normalize_base_url("127.0.0.1:9090"), "http://127.0.0.1:9090")

    def test_full_url_kept_and_trimmed(self) -> None:
        self.assertEqual(
            _normalize_base_url("http://127.0.0.1:9090/"), "http://127.0.0.1:9090"
        )


if __name__ == "__main__":
    unittest.main()
