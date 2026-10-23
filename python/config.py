from __future__ import annotations

import copy
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Dict


DEFAULT_CONFIG: Dict[str, Any] = {
    "monitor": {
        "interval_seconds": 1.0,
        "debounce_count": 2,
        "use_native_notifications": True,
    },
    "logging": {
        "level": "INFO",
        "file": "logs/app.log",
    },
    "rules": [
        {
            "id": "xms-disconnect-close-clash",
            "enabled": True,
            "description": "从 Xms 断开时断开 ClashMi 连接",
            "when": {
                "event": "on_disconnect",
                "previous_ssid": ["Xms", "Xms 5G"],
            },
            "actions": [
                {
                    "type": "clashmi.close_connections",
                }
            ],
            "cooldown_seconds": 10,
        },
        {
            "id": "xms-to-other-close-clash",
            "enabled": True,
            "description": "从 Xms 切换到其他 Wi-Fi 时断开 ClashMi 连接",
            "when": {
                "event": "on_switch",
                "previous_ssid": ["Xms", "Xms 5G"],
                "current_ssid": "*",
            },
            "actions": [
                {
                    "type": "clashmi.close_connections",
                }
            ],
            "cooldown_seconds": 10,
        },
    ],
}


class ConfigError(Exception):
    """Raised when config.json is invalid."""


def _write_text_atomic(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` atomically (temp file + os.replace).

    A crash or power loss mid-write can then never leave a truncated
    config.json behind for the next startup to choke on.
    """
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=path.name + ".", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


class ConfigManager:
    """Reads, validates, and reloads config.json."""

    def __init__(self, config_path: Path | str = "config.json") -> None:
        self.config_path = Path(config_path)
        self._config: Dict[str, Any] = copy.deepcopy(DEFAULT_CONFIG)

    def ensure_config_exists(self) -> None:
        """Create config.json from defaults if it does not exist."""
        if not self.config_path.exists():
            self.config_path.parent.mkdir(parents=True, exist_ok=True)
            self._config = copy.deepcopy(DEFAULT_CONFIG)
            _write_text_atomic(
                self.config_path,
                json.dumps(self._config, ensure_ascii=False, indent=2) + "\n",
            )

    def load(self) -> Dict[str, Any]:
        """Load and validate config.json."""
        self.ensure_config_exists()
        raw = self.config_path.read_text(encoding="utf-8")
        try:
            config = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ConfigError(f"Invalid JSON in {self.config_path}: {exc}") from exc

        self._validate(config)
        self._config = config
        return self._config

    def reload(self) -> Dict[str, Any]:
        """Reload config.json from disk."""
        return self.load()

    def get_monitor_config(self) -> Dict[str, Any]:
        return self._config.get("monitor", {})

    def get_logging_config(self) -> Dict[str, Any]:
        return self._config.get("logging", {})

    def get_rules(self) -> list[Dict[str, Any]]:
        return self._config.get("rules", [])

    def set_rules(self, rules: list[Dict[str, Any]]) -> None:
        self._config["rules"] = rules

    def save(self) -> None:
        """Save current config back to config.json (atomically)."""
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        _write_text_atomic(
            self.config_path,
            json.dumps(self._config, ensure_ascii=False, indent=2) + "\n",
        )

    def _validate(self, config: Dict[str, Any]) -> None:
        if not isinstance(config, dict):
            raise ConfigError("config root must be an object")

        monitor = config.get("monitor", {})
        if not isinstance(monitor, dict):
            raise ConfigError("monitor must be an object")
        interval = monitor.get("interval_seconds", 1.0)
        debounce = monitor.get("debounce_count", 2)
        if not isinstance(interval, (int, float)) or interval <= 0:
            raise ConfigError("monitor.interval_seconds must be a positive number")
        if not isinstance(debounce, int) or debounce < 1:
            raise ConfigError("monitor.debounce_count must be an integer >= 1")
        if "use_native_notifications" in monitor and not isinstance(
            monitor["use_native_notifications"], bool
        ):
            raise ConfigError("monitor.use_native_notifications must be a boolean")

        logging_cfg = config.get("logging", {})
        if not isinstance(logging_cfg, dict):
            raise ConfigError("logging must be an object")
        if "level" in logging_cfg and not isinstance(logging_cfg["level"], str):
            raise ConfigError("logging.level must be a string")
        if "file" in logging_cfg and not isinstance(logging_cfg["file"], str):
            raise ConfigError("logging.file must be a string")

        rules = config.get("rules", [])
        if not isinstance(rules, list):
            raise ConfigError("rules must be an array")

        seen_ids = set()
        for index, rule in enumerate(rules):
            self._validate_rule(rule, index, seen_ids)

    def _validate_rule(
        self,
        rule: Dict[str, Any],
        index: int,
        seen_ids: set[str],
    ) -> None:
        if not isinstance(rule, dict):
            raise ConfigError(f"rules[{index}] must be an object")

        rule_id = rule.get("id")
        if not isinstance(rule_id, str) or not rule_id.strip():
            raise ConfigError(f"rules[{index}].id must be a non-empty string")
        if rule_id in seen_ids:
            raise ConfigError(f"duplicate rule id: {rule_id}")
        seen_ids.add(rule_id)

        when = rule.get("when", {})
        if not isinstance(when, dict):
            raise ConfigError(f"rule {rule_id}.when must be an object")

        event = when.get("event")
        valid_events = {"on_disconnect", "on_connect", "on_switch"}
        if event not in valid_events:
            raise ConfigError(
                f"rule {rule_id}.when.event must be one of {sorted(valid_events)}"
            )

        actions = rule.get("actions", [])
        if not isinstance(actions, list) or not actions:
            raise ConfigError(f"rule {rule_id}.actions must be a non-empty array")

        for action_index, action in enumerate(actions):
            self._validate_action(rule_id, action_index, action)

        cooldown = rule.get("cooldown_seconds", 0)
        if not isinstance(cooldown, (int, float)) or cooldown < 0:
            raise ConfigError(f"rule {rule_id}.cooldown_seconds must be >= 0")

    def _validate_action(
        self,
        rule_id: str,
        action_index: int,
        action: Dict[str, Any],
    ) -> None:
        if not isinstance(action, dict):
            raise ConfigError(f"rule {rule_id} action {action_index} must be an object")

        action_type = action.get("type")
        valid_types = {
            "clashmi.close_connections",
            "clashmi.disable_tun",
            "clashmi.test_connection",
            "clash_api",
            "system_proxy",
            "kill_process",
            "run_command",
            "start_process",
            "http_request",
            "open_url",
            "delay",
            "log",
        }
        if action_type not in valid_types:
            raise ConfigError(
                f"rule {rule_id} action {action_index}.type must be one of {sorted(valid_types)}"
            )

        if action_type == "clash_api":
            if not isinstance(action.get("base_url"), str):
                raise ConfigError(f"rule {rule_id} clash_api action requires base_url")
            if action.get("subaction") not in {"close_connections", "set_mode"}:
                raise ConfigError(
                    f"rule {rule_id} clash_api.subaction must be close_connections or set_mode"
                )

        if action_type == "kill_process" and not isinstance(action.get("process_name"), str):
            raise ConfigError(f"rule {rule_id} kill_process requires process_name")

        if action_type in {"run_command", "start_process"}:
            if not isinstance(action.get("command"), str):
                raise ConfigError(f"rule {rule_id} {action_type} requires command")

        if action_type == "http_request":
            if not isinstance(action.get("url"), str):
                raise ConfigError(f"rule {rule_id} http_request requires url")
            if not isinstance(action.get("method"), str):
                raise ConfigError(f"rule {rule_id} http_request requires method")

        if action_type == "open_url" and not isinstance(action.get("url"), str):
            raise ConfigError(f"rule {rule_id} open_url requires url")

        if action_type == "delay":
            seconds = action.get("seconds")
            if not isinstance(seconds, (int, float)) or seconds <= 0:
                raise ConfigError(f"rule {rule_id} delay requires positive seconds")
