from __future__ import annotations

import copy
import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any, Dict

from ssid_utils import normalize_ssid


logger = logging.getLogger(__name__)

APP_VERSION = "1.1.0"


DEFAULT_CONFIG: Dict[str, Any] = {
    "monitor": {
        "interval_seconds": 1.0,
        "debounce_count": 2,
        "use_native_notifications": True,
        # v1.0.0: Wi-Fi 变化防抖确认延迟（秒）。状态变化后须稳定满
        # 此时长才真正触发规则；0 表示立即触发（仅单元测试用）。
        "confirm_delay_seconds": 3.0,
    },
    "logging": {
        "level": "INFO",
        "file": "logs/app.log",
    },
    # No preset rules: SSIDs are the user's own networks, so shipping
    # sample rules (v0.2.0's "Xms" pair) only ever misfired. Users add
    # rules for their current Wi-Fi with one click in the main window.
    "rules": [],
}


# The two rules v0.2.0 shipped as factory defaults. Kept verbatim so
# load() can recognize an untouched 0.2.0 config and migrate it (R3).
LEGACY_DEFAULT_RULES: list[Dict[str, Any]] = [
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
]


def _ssid_fingerprint(value: Any) -> tuple:
    """Normalize an SSID field (str | list | missing) for comparison.

    Uses the shared SSID normalization (ssid_utils), so the migration
    fingerprint agrees with the rule engine and the startup guard
    about SSID identity (review bug 6 / optimization 2).
    """
    if isinstance(value, list):
        items = [normalize_ssid(item) for item in value]
    elif value is None:
        items = []
    else:
        items = [normalize_ssid(value)]
    return tuple(sorted(items))


def _strict_bool(value: Any) -> Any:
    """Strict boolean for fingerprints: True/False/1/0 keep their value;
    anything else (e.g. the string "false") maps to a sentinel that can
    never equal a shipped default, so a hand-edited ``enabled`` is
    always treated as "user changed it" (review bug 18)."""
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    return ("<invalid-enabled>", repr(value))


def _rule_fingerprint(rule: Any) -> tuple:
    """Semantic identity of a rule for the legacy-default migration.

    Compared fields: enabled, when.event, when.previous_ssid (from),
    when.current_ssid (to), actions (full dicts, in order) and
    cooldown_seconds. Ignored: id, description and any extra metadata —
    and, at the rule-set level, the order of rules in the list. If the
    user edited any compared field of any shipped rule, the set no
    longer matches and nothing is migrated.
    """
    if not isinstance(rule, dict):
        return ("<not-a-rule>",)
    when = rule.get("when", {})
    if not isinstance(when, dict):
        when = {}
    actions = []
    for action in rule.get("actions", []):
        if isinstance(action, dict):
            actions.append(json.dumps(action, sort_keys=True, ensure_ascii=False))
        else:
            actions.append(str(action))
    return (
        _strict_bool(rule.get("enabled", True)),
        when.get("event"),
        _ssid_fingerprint(when.get("previous_ssid")),
        _ssid_fingerprint(when.get("current_ssid")),
        tuple(actions),
        rule.get("cooldown_seconds", 0),
    )


def _is_legacy_default_rules(rules: Any) -> bool:
    """True only if ``rules`` is exactly the v0.2.0 factory rule set."""
    if not isinstance(rules, list) or len(rules) != len(LEGACY_DEFAULT_RULES):
        return False
    return sorted(_rule_fingerprint(rule) for rule in rules) == sorted(
        _rule_fingerprint(rule) for rule in LEGACY_DEFAULT_RULES
    )



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
        # XiaoMaLink v1.0.0 (spec 4.1): every successful save is
        # mirrored to this .bak file; restore_from_backup() can bring
        # it back when config.json itself is damaged.
        self.backup_path = self.config_path.with_name(
            self.config_path.name + ".bak"
        )
        self._config: Dict[str, Any] = copy.deepcopy(DEFAULT_CONFIG)
        # Human-readable reasons for rules dropped by tolerant
        # validation on the last load() (review bug 11).
        self.last_validation_warnings: list[str] = []
        # Reason the last load() failed, if any (None after a
        # successful load). The integration layer uses this for the
        # tray alert on config-load failure; load() still raises
        # exactly as before, so the .corrupt recovery flow in main.py
        # is untouched.
        self.last_load_error: str | None = None

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
        """Load and validate config.json.

        Behaviour is unchanged (same raises, same validation, same
        migration); the only addition is that a failure is also
        recorded in ``last_load_error`` before it is re-raised, and a
        success clears it.
        """
        try:
            self.ensure_config_exists()
            raw = self.config_path.read_text(encoding="utf-8")
            try:
                config = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ConfigError(
                    f"Invalid JSON in {self.config_path}: {exc}"
                ) from exc

            self._validate(config)
            self._config = config
            self._migrate_legacy_default_rules()
        except (ConfigError, OSError) as exc:
            self.last_load_error = str(exc)
            raise
        self.last_load_error = None
        return self._config

    def _migrate_legacy_default_rules(self) -> None:
        """Clear the untouched v0.2.0 factory rules (R3 migration).

        The 0.2.0 sample rules target "Xms" networks the user does not
        have; if the loaded rule set is still exactly that factory set
        (see _rule_fingerprint for the comparison), drop it and persist
        the empty list. Any user edit to any compared field keeps the
        whole set untouched.
        """
        rules = self._config.get("rules", [])
        if not isinstance(rules, list) or not rules:
            return
        # Whole-set only, deliberately (review bug 19, not adopted):
        # the review itself calls the whole-set comparison "reasonable
        # and conservatively biased (better to under-clear than to
        # delete by mistake)". Clearing untouched rules one by one
        # would break that contract — e.g. editing one shipped rule
        # would silently delete its untouched sibling. The leftover
        # boundary is documented instead: a user who deleted one
        # shipped rule keeps the other (it targets Xms networks the
        # user does not own, so it never fires for them, and it can
        # be deleted in the UI).
        if not _is_legacy_default_rules(rules):
            return
        logger.info(
            "Migrating config: clearing the v0.2.0 factory-default Xms rules"
        )
        self._config["rules"] = []
        try:
            self.save()
        except OSError:
            # The in-memory rules are already migrated; a failed write
            # must not turn an otherwise valid config into a "corrupt"
            # one at startup. The stale file is migrated again on the
            # next successful load/save cycle.
            logger.warning("Could not persist the migrated config", exc_info=True)

    def reload(self) -> Dict[str, Any]:
        """Reload config.json from disk."""
        return self.load()

    def get_monitor_config(self) -> Dict[str, Any]:
        return self._config.get("monitor", {})

    def get_logging_config(self) -> Dict[str, Any]:
        return self._config.get("logging", {})

    def get_rules(self) -> list[Dict[str, Any]]:
        # Copy of the list (pre-check item 6): callers may append or
        # filter without affecting an in-flight evaluation on another
        # thread; the rule dicts themselves are treated as immutable
        # once stored (edits replace the dict, see main_window).
        return list(self._config.get("rules", []))

    def set_rules(self, rules: list[Dict[str, Any]]) -> None:
        self._config["rules"] = rules

    def save(self) -> None:
        """Save current config back to config.json (atomically).

        On success the identical content is mirrored to
        ``config.json.bak`` (spec 4.1). The mirror is best-effort: a
        backup failure is only logged as a warning and can never make
        save() itself fail.
        """
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps(self._config, ensure_ascii=False, indent=2) + "\n"
        _write_text_atomic(self.config_path, text)
        try:
            _write_text_atomic(self.backup_path, text)
        except OSError:
            logger.warning(
                "Could not write config backup to %s", self.backup_path,
                exc_info=True,
            )

    def backup_exists(self) -> bool:
        """True if a usable ``config.json.bak`` file is present."""
        return self.backup_path.is_file()

    def restore_from_backup(self) -> bool:
        """Restore config.json from ``config.json.bak`` (one-click path).

        Returns True and atomically overwrites config.json (then
        reloads ``_config``) only when the backup exists and parses
        as a valid config object. Returns False and leaves the
        original config.json byte-for-byte untouched when there is no
        backup, the backup is not valid JSON, is not an object, or
        fails validation; a failed atomic rewrite also returns False.
        """
        if not self.backup_exists():
            return False
        try:
            raw = self.backup_path.read_text(encoding="utf-8")
        except OSError:
            logger.warning(
                "Could not read config backup %s", self.backup_path,
                exc_info=True,
            )
            return False
        try:
            restored = json.loads(raw)
        except json.JSONDecodeError:
            return False
        if not isinstance(restored, dict):
            return False
        candidate = copy.deepcopy(restored)
        try:
            self._validate(candidate)
        except ConfigError:
            return False
        try:
            # Raw backup bytes, not a re-dump: the restored file must
            # match the backup exactly. _config takes the validated
            # copy, mirroring what load() would hold in memory.
            _write_text_atomic(self.config_path, raw)
        except OSError:
            logger.warning(
                "Could not restore config from backup %s", self.backup_path,
                exc_info=True,
            )
            return False
        self._config = candidate
        self.last_load_error = None
        return True

    def _validate(self, config: Dict[str, Any]) -> None:
        if not isinstance(config, dict):
            raise ConfigError("config root must be an object")

        # Fill in defaults for missing sections/keys, so an older
        # config keeps working when a new version adds settings
        # (review bug 27).
        for section, defaults in (
            ("monitor", DEFAULT_CONFIG["monitor"]),
            ("logging", DEFAULT_CONFIG["logging"]),
        ):
            if section not in config:
                config[section] = copy.deepcopy(defaults)
            elif isinstance(config[section], dict):
                for key, value in defaults.items():
                    config[section].setdefault(key, copy.deepcopy(value))
        if "rules" not in config:
            config["rules"] = []

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
        confirm_delay = monitor.get("confirm_delay_seconds", 3.0)
        if not isinstance(confirm_delay, (int, float)) or confirm_delay < 0:
            raise ConfigError("monitor.confirm_delay_seconds must be >= 0")

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

        # Tolerant per-rule validation (review bug 11): one mistyped
        # rule must not nuke the whole config. Invalid rules are
        # dropped with a warning; valid rules survive. Structural
        # problems above (monitor/logging types, unparseable JSON)
        # still raise ConfigError.
        seen_ids: set = set()
        valid_rules: list = []
        dropped: list[str] = []
        for index, rule in enumerate(rules):
            trial_ids = set(seen_ids)
            try:
                self._validate_rule(rule, index, trial_ids)
            except ConfigError as exc:
                dropped.append(str(exc))
                logger.warning("Dropping invalid rule at index %s: %s", index, exc)
                continue
            seen_ids = trial_ids
            valid_rules.append(rule)
        if dropped:
            config["rules"] = valid_rules
            self.last_validation_warnings = dropped

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

        if "enabled" in rule and not (
            isinstance(rule["enabled"], bool)
            or (isinstance(rule["enabled"], int) and rule["enabled"] in (0, 1))
        ):
            raise ConfigError(f"rule {rule_id}.enabled must be a boolean")
        if rule_id in seen_ids:
            raise ConfigError(f"duplicate rule id: {rule_id}")
        seen_ids.add(rule_id)

        when = rule.get("when", {})
        if not isinstance(when, dict):
            raise ConfigError(f"rule {rule_id}.when must be an object")

        event = when.get("event")
        valid_events = {"on_disconnect", "on_connect", "on_switch", "on_program_started"}
        if event not in valid_events:
            raise ConfigError(
                f"rule {rule_id}.when.event must be one of {sorted(valid_events)}"
            )

        # v1.0.0 新增触发条件字段：程序触发的程序名、电源附加条件、
        # 退出恢复开关。非法值直接判整条规则无效（容忍式剔除）。
        if "program" in when and (
            not isinstance(when["program"], str) or not when["program"].strip()
        ):
            raise ConfigError(f"rule {rule_id}.when.program must be a non-empty string")
        if "power" in when and when["power"] not in {"any", "ac", "battery"}:
            raise ConfigError(
                f"rule {rule_id}.when.power must be one of any/ac/battery"
            )
        if "restore_on_exit" in rule and not isinstance(rule["restore_on_exit"], bool):
            raise ConfigError(f"rule {rule_id}.restore_on_exit must be a boolean")

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
            "clashmi.kill_process",
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
        # v1.0.0 系统动作类型（与 system_actions.ACTION_TYPES 同源；
        # 导入失败时退化为旧集合，含新动作的规则会被剔除并记 warning）。
        try:
            from system_actions import ACTION_TYPES as _SYSTEM_ACTION_TYPES
        except Exception:  # pragma: no cover - defensive
            _SYSTEM_ACTION_TYPES = ()
        valid_types |= set(_SYSTEM_ACTION_TYPES)
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
