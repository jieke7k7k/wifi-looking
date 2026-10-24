from __future__ import annotations

import argparse
import json
import logging
from logging.handlers import RotatingFileHandler
import os
import signal
import sys
import threading
from pathlib import Path
from typing import Callable

from PySide6.QtWidgets import QApplication


from action_executor import ActionExecutor
from config import APP_VERSION, ConfigError, ConfigManager
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
        # Per-rule problems no longer reach here (ConfigManager drops
        # bad rules with warnings, review bug 11); only whole-file
        # failures (unparseable JSON, structural errors) fall through
        # to the .corrupt recovery below.
        for warning in manager.last_validation_warnings:
            logger.warning("Config rule dropped during load: %s", warning)
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
    parser.add_argument(
        "--minimized",
        action="store_true",
        help="Start hidden in the tray (used by the autostart entry)",
    )
    parser.add_argument(
        "--reset-config",
        action="store_true",
        help="Reset config.json to the shipped defaults and exit",
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

    # Rotating logs (pre-check item 4): the old unbounded FileHandler
    # let a failing monitor grow app.log without limit; 1 MB x 5
    # files bounds it at ~5 MB. Failure logging itself is throttled
    # separately in the monitor loop (review bug 24).
    handlers: list[logging.Handler] = [
        RotatingFileHandler(
            log_path, encoding="utf-8", maxBytes=1024 * 1024, backupCount=5
        )
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
    on_action_failure=None,
) -> Callable[[dict], None]:
    """Build the monitor's event handler (shared by GUI and console mode).

    ``on_action_failure`` (GUI mode only) is called with a failed
    action result so the app can surface it — e.g. an access-denied
    kill must not stay a log-only whisper (review bug 16).
    """

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
                    if on_action_failure is not None:
                        try:
                            on_action_failure(action, result)
                        except Exception:
                            logger.debug("Failure callback failed", exc_info=True)
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
    # HiDPI (pre-check item 10): let Qt use high-resolution pixmaps
    # for tray/window icons instead of upscaling a 16px frame.
    from PySide6.QtCore import QCoreApplication, Qt

    QCoreApplication.setAttribute(Qt.ApplicationAttribute.AA_UseHighDpiPixmaps, True)
    app = QApplication(sys.argv)
    app.setApplicationName("WiFi-Looking")
    app.setQuitOnLastWindowClosed(False)

    # Single-instance guard (U1): acquire BEFORE any monitor/tray is
    # created. A second launch only wakes the running instance and
    # exits — it must never start a second monitor thread or tray icon.
    from single_instance import SingleInstance

    guard = SingleInstance(config_manager.config_path.parent / "WiFi-Looking.lock")
    if not guard.try_acquire():
        if guard.notify_existing():
            logger.info("Another WiFi-Looking instance is running; asked it to show its window")
        else:
            logger.warning("Another instance is running but did not answer the wake-up message")
        return 0
    app._single_instance_guard = guard  # keep the guard alive for the app lifetime
    app.aboutToQuit.connect(guard.release)

    stop_event = threading.Event()
    action_executor = ActionExecutor()
    rule_engine = RuleEngine()
    # Late-bound failure notifier: the tray does not exist yet when
    # the handler is built, so the callback reads the holder at call
    # time (review bug 16: access-denied kills get a tray prompt).
    failure_notifier: dict = {}

    def _on_action_failure(action, result) -> None:
        if action.get("type") != "clashmi.kill_process":
            return
        errors = " ".join(str(f.get("error", "")) for f in result.get("failed", []))
        if not any(token in errors.lower() for token in ("access", "denied", "拒绝访问")):
            return
        notify = failure_notifier.get("notify")
        if notify is not None:
            notify(
                "ClashMi 无法关闭（拒绝访问）：若 ClashMi 以管理员身份运行，"
                "请以管理员身份运行 WiFi-Looking 后再试。"
            )

    handle_event = make_event_handler(
        config_manager,
        rule_engine,
        action_executor,
        dry_run=args.dry_run,
        on_action_failure=_on_action_failure,
    )

    monitor = create_monitor(config_manager, args, handle_event)
    monitor.start()

    from main_window import MainWindow
    from tray_app import TrayApp

    main_window = MainWindow(config_manager, monitor)
    if args.minimized:
        # Autostart launches straight to the tray; popping the main
        # window on every logon trains users to disable autostart
        # (review bug 26).
        main_window.hide()
    else:
        main_window.show()

    tray = TrayApp(
        app=app,
        config_manager=config_manager,
        monitor=monitor,
        main_window=main_window,
    )
    tray.show()
    failure_notifier["notify"] = lambda message: tray.show_message(
        "WiFi-Looking 提醒", message
    )

    # A second launch wakes this instance (U1): bring the window
    # forward instead of starting a second monitor/tray.
    guard.show_requested.connect(main_window.show_from_tray)

    # One-shot startup guard (R4): if the baseline network is not
    # covered by any rule while ClashMi is on, remind — never auto-close.
    main_window.start_startup_guard()

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

    if args.reset_config:
        # "Restore factory defaults" escape hatch (review bug 27):
        # overwrite config.json with the shipped defaults.
        from config import DEFAULT_CONFIG
        import copy as _copy
        import json as _json

        target = Path(args.config) if args.config else data_dir() / "config.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            _json.dumps(_copy.deepcopy(DEFAULT_CONFIG), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(f"Config reset to defaults: {target}")
        return 0

    # Migrate a config left behind in the %APPDATA% fallback before
    # loading from the primary data dir (review bug 28).
    primary_dir = data_dir()
    if not (primary_dir / "config.json").exists():
        appdata = os.environ.get("APPDATA")
        if appdata:
            legacy = Path(appdata) / "WiFi-Looking" / "config.json"
            if legacy.exists() and legacy.resolve() != (primary_dir / "config.json").resolve():
                try:
                    (primary_dir / "config.json").write_bytes(legacy.read_bytes())
                    logger.warning("Migrated config from fallback dir %s", legacy.parent)
                except OSError:
                    logger.warning("Could not migrate fallback config", exc_info=True)

    config_manager = load_config_manager(
        Path(args.config) if args.config else primary_dir / "config.json"
    )
    setup_logging(config_manager)
    logger.info("WiFi-Looking version %s starting", APP_VERSION)

    if args.once:
        return run_once(config_manager)

    if args.no_tray:
        return run_console(config_manager, args)

    return run_gui(config_manager, args)


if __name__ == "__main__":
    raise SystemExit(main())
