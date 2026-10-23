from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import threading
from pathlib import Path
from typing import Callable

from PySide6.QtWidgets import QApplication


from action_executor import ActionExecutor
from config import ConfigError, ConfigManager
from rule_engine import RuleEngine
from wifi_monitor import WifiMonitor


logger = logging.getLogger(__name__)


def app_dir() -> Path:
    """Directory the app lives in (exe dir when frozen, script dir else).

    Config and logs are anchored here instead of the current working
    directory, so launching the exe from a shortcut or another folder
    never silently creates a fresh default config somewhere else.
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def _is_writable_dir(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".wifi-looking-write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False


def data_dir() -> Path:
    """Directory holding config.json and logs.

    Normally the app directory itself (the installer targets the user's
    own folder, which is writable). If it is not writable — e.g. the exe
    was copied into Program Files — fall back to %APPDATA%\\WiFi-Looking
    instead of crashing on startup or being silently redirected by UAC
    virtualization.
    """
    base = app_dir()
    if _is_writable_dir(base):
        return base

    appdata = os.environ.get("APPDATA")
    if appdata:
        fallback = Path(appdata) / "WiFi-Looking"
        try:
            fallback.mkdir(parents=True, exist_ok=True)
            return fallback
        except OSError:
            pass
    return base


def load_config_manager(config_path: Path) -> ConfigManager:
    """Load config, recovering from a corrupt file instead of crashing.

    A damaged config.json is moved aside to ``config.json.corrupt`` and
    the app starts from defaults, so one bad write can never brick every
    future launch (the packaged build has no console to show a traceback).
    """
    manager = ConfigManager(config_path)
    try:
        manager.load()
        return manager
    except (ConfigError, OSError) as exc:
        logger.warning("Config %s unusable (%s); starting from defaults", config_path, exc)

    try:
        config_path.replace(config_path.with_name(config_path.name + ".corrupt"))
    except OSError:
        pass

    manager = ConfigManager(config_path)
    try:
        manager.save()
    except OSError:
        logger.warning("Could not write a fresh default config to %s", config_path)
    return manager


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="WiFi-Looking background daemon")
    parser.add_argument(
        "--config",
        default=None,
        help="Path to config.json (default: config.json next to the app)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Match rules but do not execute actions",
    )
    parser.add_argument(
        "--no-tray",
        action="store_true",
        help="Run in console mode instead of GUI + tray mode",
    )
    parser.add_argument(
        "--no-native",
        action="store_true",
        help="Force polling mode even if native notifications are enabled",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Read one Wi-Fi state and exit",
    )
    return parser.parse_args(argv)


def setup_logging(config_manager: ConfigManager) -> None:
    logging_config = config_manager.get_logging_config()
    level_name = str(logging_config.get("level", "INFO")).upper()
    level = getattr(logging, level_name, logging.INFO)
    log_file = logging_config.get("file", "logs/app.log")

    log_path = Path(log_file)
    if not log_path.is_absolute():
        log_path = config_manager.config_path.resolve().parent / log_path
    log_path.parent.mkdir(parents=True, exist_ok=True)

    handlers: list[logging.Handler] = [
        logging.FileHandler(log_path, encoding="utf-8")
    ]
    # In the --noconsole packaged build sys.stdout is None; attaching a
    # StreamHandler then makes every log call raise inside the handler.
    if sys.stdout is not None:
        handlers.append(logging.StreamHandler(sys.stdout))

    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=handlers,
        force=True,
    )


def run_once(config_manager: ConfigManager) -> int:
    monitor = WifiMonitor(
        interval_seconds=config_manager.get_monitor_config().get("interval_seconds", 1.0),
        debounce_count=1,
    )
    try:
        state = monitor.read_state()
    except Exception as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        return 1
    print(
        json.dumps(
            {
                "connected": state.connected,
                "ssid": state.ssid,
                "interface_name": state.interface_name,
                "state_text": state.state_text,
            },
            ensure_ascii=False,
        )
    )
    return 0


def make_event_handler(
    config_manager: ConfigManager,
    rule_engine: RuleEngine,
    action_executor: ActionExecutor,
    dry_run: bool = False,
) -> Callable[[dict], None]:
    """Build the monitor's event handler (shared by GUI and console mode)."""

    def handle_event(event: dict) -> None:
        logger.info("Wi-Fi event detected: %s", event)
        rules = config_manager.get_rules()
        matched_rules = rule_engine.evaluate(event, rules)

        for rule in matched_rules:
            logger.info("Rule matched: %s", rule.get("id"))
            all_ok = True
            for action in rule.get("actions", []):
                result = action_executor.execute_action(
                    action,
                    event,
                    dry_run=dry_run,
                )
                logger.info("Action result: %s", result)
                if not result.get("ok"):
                    all_ok = False
            # Cooldown starts only after a fully successful run, so a
            # transient failure (ClashMi down, 401) is retried on the
            # next event instead of being swallowed by the cooldown.
            if all_ok:
                rule_engine.mark_run(rule["id"])

    return handle_event


def create_monitor(config_manager: ConfigManager, args: argparse.Namespace, handle_event) -> WifiMonitor:
    monitor_config = config_manager.get_monitor_config()
    use_native = bool(
        monitor_config.get("use_native_notifications", True)
        and not args.no_native
        and sys.platform == "win32"
    )

    if use_native:
        try:
            from native_wifi_monitor import NativeWifiMonitor

            return NativeWifiMonitor(
                interval_seconds=monitor_config.get("interval_seconds", 1.0),
                debounce_count=monitor_config.get("debounce_count", 2),
                on_event=handle_event,
            )
        except Exception:
            logger.exception("Failed to create native WLAN monitor; using polling")

    return WifiMonitor(
        interval_seconds=monitor_config.get("interval_seconds", 1.0),
        debounce_count=monitor_config.get("debounce_count", 2),
        on_event=handle_event,
    )


def run_gui(config_manager: ConfigManager, args: argparse.Namespace) -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("WiFi-Looking")
    app.setQuitOnLastWindowClosed(False)

    stop_event = threading.Event()
    action_executor = ActionExecutor()
    rule_engine = RuleEngine()
    handle_event = make_event_handler(
        config_manager, rule_engine, action_executor, dry_run=args.dry_run
    )

    monitor = create_monitor(config_manager, args, handle_event)
    monitor.start()

    from main_window import MainWindow
    from tray_app import TrayApp

    main_window = MainWindow(config_manager, monitor)
    main_window.show()

    tray = TrayApp(
        app=app,
        config_manager=config_manager,
        monitor=monitor,
        main_window=main_window,
    )
    tray.show()

    app.aboutToQuit.connect(monitor.stop)

    def handle_signal(signum: int, frame: object) -> None:
        logger.info("Received signal %s; stopping", signum)
        stop_event.set()
        monitor.stop()
        app.quit()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    logger.info("WiFi-Looking GUI started")
    return app.exec()


def run_console(config_manager: ConfigManager, args: argparse.Namespace) -> int:
    logger.info("Running in console mode")

    stop_event = threading.Event()
    action_executor = ActionExecutor()
    rule_engine = RuleEngine()
    handle_event = make_event_handler(
        config_manager, rule_engine, action_executor, dry_run=args.dry_run
    )

    monitor = create_monitor(config_manager, args, handle_event)
    monitor.start()

    def handle_signal(signum: int, frame: object) -> None:
        logger.info("Received signal %s; stopping", signum)
        stop_event.set()
        monitor.stop()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    while not stop_event.is_set():
        stop_event.wait(timeout=1.0)

    logger.info("WiFi-Looking stopped")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    config_manager = load_config_manager(
        Path(args.config) if args.config else data_dir() / "config.json"
    )
    setup_logging(config_manager)

    if args.once:
        return run_once(config_manager)

    if args.no_tray:
        return run_console(config_manager, args)

    return run_gui(config_manager, args)


if __name__ == "__main__":
    raise SystemExit(main())
