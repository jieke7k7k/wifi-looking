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

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import QApplication


from action_executor import ActionExecutor
from config import APP_VERSION, ConfigError, ConfigManager
from restore_stack import RestoreError, RestoreStack
from rule_engine import RuleEngine
from wifi_monitor import WifiMonitor


logger = logging.getLogger(__name__)


# 系统动作 -> 可恢复属性名（与 system_actions.RESTORE_PROPERTIES 对齐）。
# 退出恢复开关开启时，规则执行这些动作的前一刻会快照对应属性的原值。
ACTION_TO_RESTORE_PROPERTY = {
    "bluetooth.set": "bluetooth_enabled",
    "brightness.set": "brightness",
    "volume.set": "volume",
    "volume.mute": "muted",
    "dark_mode.set": "dark_mode",
    "wallpaper.set": "wallpaper_path",
    "power.plan.set": "power_plan",
    "screen.timeout.set": "screen_timeout_minutes",
}

_HIGH_RISK_ACTION_LABELS = {
    "sleep": "睡眠",
    "shutdown": "关机",
    "restart": "重启",
}


class BannerConfirmation(QObject):
    """高危动作的 15 秒置顶无焦点倒计时横幅确认桥（P0-6）。

    规则动作在后台线程执行，`confirm()` 也可能被后台线程调用；本桥只
    在后台线程 `emit` 信号，由 Qt 队列连接切回 GUI 线程创建并显示
    `CountdownBanner`，倒计时归零 -> True、用户点击取消 -> False，再通过
    ``threading.Event`` 同步回调用方。
    """

    request_banner = Signal(object)

    def __init__(self, countdown_seconds: int = 15, parent=None) -> None:
        super().__init__(parent)
        self.countdown_seconds = countdown_seconds
        self._active_banner = None
        self.request_banner.connect(
            self._show_banner, Qt.ConnectionType.QueuedConnection
        )

    def confirm(self, action: dict) -> bool:
        holder: dict = {"result": False, "done": threading.Event()}
        rule_name = str(action.get("_rule_name") or "规则")
        action_name = _HIGH_RISK_ACTION_LABELS.get(
            action.get("type"), str(action.get("type"))
        )
        self.request_banner.emit(
            {"rule_name": rule_name, "action_name": action_name, "holder": holder}
        )
        holder["done"].wait(timeout=self.countdown_seconds + 10)
        return bool(holder.get("result"))

    def _show_banner(self, payload: dict) -> None:
        holder = payload.get("holder")
        if not isinstance(holder, dict):
            return
        try:
            from banner import CountdownBanner
        except Exception as exc:
            logger.warning("无法创建高危动作横幅：%s", exc)
            holder["result"] = False
            holder["done"].set()
            return
        banner = CountdownBanner(
            payload.get("rule_name", "规则"),
            payload.get("action_name", "动作"),
            seconds=self.countdown_seconds,
        )
        banner.cancelled.connect(lambda: self._finish(holder, False))
        banner.expired.connect(lambda: self._finish(holder, True))
        self._active_banner = banner
        banner.start()

    def _finish(self, holder: dict, result: bool) -> None:
        holder["result"] = result
        holder["done"].set()
        banner = self._active_banner
        self._active_banner = None
        if banner is not None:
            try:
                banner.stop()
                banner.close()
                banner.deleteLater()
            except Exception:
                logger.debug("关闭高危动作横幅失败", exc_info=True)


def _restore_action(property_name: str, value: object) -> dict:
    """把待恢复的 {属性: 原值} 翻译回一个系统动作 dict。"""
    if property_name == "bluetooth_enabled":
        return {"type": "bluetooth.set", "enabled": bool(value)}
    if property_name == "brightness":
        return {"type": "brightness.set", "value": int(value)}
    if property_name == "volume":
        return {"type": "volume.set", "value": int(value)}
    if property_name == "muted":
        return {"type": "volume.mute", "muted": bool(value)}
    if property_name == "dark_mode":
        return {"type": "dark_mode.set", "mode": str(value)}
    if property_name == "wallpaper_path":
        return {"type": "wallpaper.set", "path": str(value)}
    if property_name == "power_plan":
        return {"type": "power.plan.set", "plan": str(value)}
    if property_name == "screen_timeout_minutes":
        if isinstance(value, dict) and "minutes" in value:
            return {
                "type": "screen.timeout.set",
                "minutes": int(value["minutes"]),
                "plugged": bool(value.get("plugged", True)),
            }
        return {"type": "screen.timeout.set", "minutes": int(value), "plugged": True}
    return {}


def collect_watched_programs(rules) -> list:
    """收集所有「程序启动」触发规则监听的进程名（去重、保留原始写法）。"""
    names: list = []
    for rule in rules:
        when = rule.get("when", {}) if isinstance(rule, dict) else {}
        if when.get("event") != "on_program_started":
            continue
        program = when.get("program")
        if not (isinstance(program, str) and program.strip()):
            continue
        if not any(n.lower() == program.strip().lower() for n in names):
            names.append(program.strip())
    return names


def ensure_process_watcher_running(process_watcher, watched_names: list) -> None:
    """同步监听名单并保证监听线程在有监听对象时处于运行状态。

    这是 P0-3 修复点：进程监听器此前只在「启动时已有程序规则」才会
    启动，运行中新增第一条程序规则后名单虽被更新，线程却始终不启动，
    导致程序启动触发器静默失效。这里统一为：名单非空就启动（
    ``ProcessWatcher.start()`` 本身幂等，空名单不启动也不报错）。
    """
    process_watcher.set_watch_names(watched_names)
    if watched_names:
        process_watcher.start()


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
    was copied into Program Files — fall back to %APPDATA%\\XiaoMaLink
    instead of crashing on startup or being silently redirected by UAC
    virtualization.
    """
    base = app_dir()
    if _is_writable_dir(base):
        return base

    appdata = os.environ.get("APPDATA")
    if appdata:
        fallback = Path(appdata) / "XiaoMaLink"
        try:
            fallback.mkdir(parents=True, exist_ok=True)
            return fallback
        except OSError:
            pass
    return base


def load_config_manager(config_path: Path) -> ConfigManager:
    """Load config, recovering from a corrupt file instead of crashing.

    需求 4.1：加载失败时先尝试从 ``config.json.bak`` 一键恢复；没有
    可用备份或备份也无效时，才把损坏文件移到 ``config.json.corrupt``
    并从默认配置启动（绝不因一次坏写而无法再启动）。
    """
    manager = ConfigManager(config_path)
    try:
        manager.load()
        # Per-rule problems no longer reach here (ConfigManager drops
        # bad rules with warnings, review bug 11); only whole-file
        # failures (unparseable JSON, structural errors) fall through
        # to the backup / .corrupt recovery below.
        for warning in manager.last_validation_warnings:
            logger.warning("Config rule dropped during load: %s", warning)
        return manager
    except (ConfigError, OSError) as exc:
        logger.warning("Config %s unusable (%s); trying backup", config_path, exc)

    # 需求 4.1：先从 .bak 恢复；成功则标记供托盘报警。
    try:
        if manager.restore_from_backup():
            logger.warning("配置已从 %s 恢复", manager.backup_path)
            manager._recovered_from_backup = True
            return manager
    except Exception:
        logger.debug("Backup restore failed", exc_info=True)

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
    parser = argparse.ArgumentParser(description="小马联动（XiaoMaLink）后台自动化程序")
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
    restore_stack: "RestoreStack | None" = None,
    on_restore_failure=None,
    pause_flag: "dict | None" = None,
    confirmation=None,
) -> Callable[[dict], None]:
    """Build the monitor's event handler (shared by GUI and console mode).

    ``on_action_failure`` (GUI mode only) is called with a failed
    action result so the app can surface it — e.g. an access-denied
    kill must not stay a log-only whisper (review bug 16).

    ``restore_stack`` 非空时，开启「退出恢复」且触发器为程序启动的
    规则，在修改系统状态前会快照对应属性的原值（需求 4.3）。

    ``pause_flag`` 非空时，其 ``["paused"]`` 为真则规则被整体暂停
    （需求 5.3 托盘「暂停所有规则」）。
    """

    def handle_event(event: dict) -> None:
        if pause_flag is not None and pause_flag.get("paused"):
            logger.info("规则已暂停，忽略事件：%s", event.get("type"))
            return
        logger.info("Wi-Fi event detected: %s", event)
        rules = config_manager.get_rules()
        matched_rules = rule_engine.evaluate(event, rules)

        for rule in matched_rules:
            logger.info("Rule matched: %s", rule.get("id"))
            when = rule.get("when", {}) if isinstance(rule, dict) else {}
            restore_on_exit = bool(rule.get("restore_on_exit")) and when.get(
                "event"
            ) == "on_program_started"
            all_ok = True
            for action in rule.get("actions", []):
                # 携带规则名，供高危动作横幅展示「规则 [规则名称] 已触发」。
                exec_action = dict(action)
                exec_action["_rule_name"] = str(rule.get("name") or rule.get("id") or "规则")
                # 执行前快照原值（仅程序启动 + 退出恢复开关开启的规则）。
                if restore_stack is not None and restore_on_exit and not dry_run:
                    property_name = ACTION_TO_RESTORE_PROPERTY.get(exec_action.get("type"))
                    if property_name:
                        try:
                            from system_actions import read_property_value

                            value = read_property_value(property_name, exec_action)
                        except Exception:
                            value = None
                        if value is not None:
                            try:
                                restore_stack.record(rule["id"], property_name, value)
                            except (ValueError, OSError) as exc:
                                logger.warning("记录恢复快照失败：%s", exc)
                result = action_executor.execute_action(
                    exec_action,
                    event,
                    dry_run=dry_run,
                    confirmation=confirmation,
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


def make_program_exit_handler(
    config_manager: ConfigManager,
    action_executor: ActionExecutor,
    restore_stack: "RestoreStack",
    on_restore_failure=None,
) -> Callable[[str], None]:
    """Build the process-watcher's ``on_exited`` handler.

    程序彻底退出（含 3 秒防抖）后，对开启「退出恢复」的程序启动规则
    执行引用计数恢复：release 返回引用归零的属性原值，再翻译回系统
    动作还原。恢复失败通过 ``on_restore_failure`` 上报，绝不静默。
    """

    def on_program_exited(name: str) -> None:
        logger.info("Watched program exited: %s", name)
        for rule in config_manager.get_rules():
            when = rule.get("when", {}) if isinstance(rule, dict) else {}
            if when.get("event") != "on_program_started":
                continue
            program = when.get("program")
            if not (isinstance(program, str) and program.strip().lower() == name.lower()):
                continue
            if not rule.get("restore_on_exit"):
                continue
            try:
                pending = restore_stack.pending_for_release(rule["id"])
            except Exception as exc:
                logger.warning("恢复栈读取待恢复值失败：%s", exc)
                continue
            committed: list = []
            for property_name, value in pending.items():
                action = _restore_action(property_name, value)
                if not action:
                    continue
                result = action_executor.execute_action(action, {})
                if result.get("ok"):
                    committed.append(property_name)
                else:
                    logger.warning(
                        "恢复属性 %s 失败：%s", property_name, result.get("error")
                    )
                    if on_restore_failure is not None:
                        try:
                            on_restore_failure(property_name, result)
                        except Exception:
                            logger.debug("恢复失败回调异常", exc_info=True)
            # P0-4：仅恢复成功的属性才从栈里移除，失败保留以便下次重试。
            if committed:
                try:
                    restore_stack.commit_release(rule["id"], committed)
                except Exception as exc:
                    logger.warning("恢复栈提交失败：%s", exc)

    return on_program_exited


def create_monitor(config_manager: ConfigManager, args: argparse.Namespace, handle_event) -> WifiMonitor:
    monitor_config = config_manager.get_monitor_config()
    # v1.0.0：Wi-Fi 变化 3 秒防抖确认延迟（需求 2.1），来自配置。
    try:
        confirm_delay = float(monitor_config.get("confirm_delay_seconds", 3.0))
    except (TypeError, ValueError):
        confirm_delay = 3.0
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
                confirm_delay_seconds=confirm_delay,
            )
        except Exception:
            logger.exception("Failed to create native WLAN monitor; using polling")

    return WifiMonitor(
        interval_seconds=monitor_config.get("interval_seconds", 1.0),
        debounce_count=monitor_config.get("debounce_count", 2),
        on_event=handle_event,
        confirm_delay_seconds=confirm_delay,
    )


def run_gui(config_manager: ConfigManager, args: argparse.Namespace) -> int:
    # HiDPI (pre-check item 10): let Qt use high-resolution pixmaps
    # for tray/window icons instead of upscaling a 16px frame.
    from PySide6.QtCore import QCoreApplication, Qt

    QCoreApplication.setAttribute(Qt.ApplicationAttribute.AA_UseHighDpiPixmaps, True)
    app = QApplication(sys.argv)
    app.setApplicationName("小马联动")
    app.setQuitOnLastWindowClosed(False)

    # 系统动作能力自检（P0-2）：打包后蓝牙/亮度/音量依赖缺失能直接定位。
    try:
        from system_actions import log_capability_report

        log_capability_report()
    except Exception:
        logger.debug("系统动作能力自检失败", exc_info=True)

    # Single-instance guard (U1): acquire BEFORE any monitor/tray is
    # created. A second launch only wakes the running instance and
    # exits — it must never start a second monitor thread or tray icon.
    # 需求 1：命名 Mutex 判定唯一实例；启动时清理旧版本僵尸进程。
    from mutex_instance import NamedMutex
    from single_instance import SingleInstance

    mutex = NamedMutex()
    if not mutex.is_first:
        waker = SingleInstance(config_manager.config_path.parent / "XiaoMaLink.lock")
        if waker.notify_existing():
            logger.info("已有实例在运行，已请求其显示主窗口")
        else:
            logger.warning("已有实例在运行但未能唤醒")
        return 0
    app._single_instance_mutex = mutex

    try:
        from old_instance import takeover_old_instances

        old_report = takeover_old_instances()
        if old_report.get("found"):
            logger.info("已清理旧版本残留实例：%s", old_report)
    except Exception:
        logger.warning("清理旧版本实例失败", exc_info=True)

    guard = SingleInstance(config_manager.config_path.parent / "XiaoMaLink.lock")
    if not guard.try_acquire():
        logger.warning("单实例唤醒通道建立失败，仍继续运行")
    app._single_instance_guard = guard  # keep the guard alive for the app lifetime
    app.aboutToQuit.connect(guard.release)
    app.aboutToQuit.connect(mutex.release)

    stop_event = threading.Event()
    action_executor = ActionExecutor()
    rule_engine = RuleEngine()
    # 高危动作确认桥（P0-6）：必须在 GUI 线程拥有其生命周期。
    confirmation = BannerConfirmation()
    app._banner_confirmation = confirmation

    # 恢复栈（需求 4.3）：退出恢复 + 崩溃持久化 restore_state.json。
    restore_path = config_manager.config_path.parent / "restore_state.json"
    try:
        restore_stack = RestoreStack.load_pending(restore_path)
    except RestoreError as exc:
        logger.warning("恢复快照无法读取，从空栈开始：%s", exc)
        restore_stack = RestoreStack(restore_path)

    # 启动恢复（需求 4.3）：必须在创建事件/退出处理器之前完成，且全程
    # 使用同一个 restore_stack 实例（P0-4 修复）。恢复成功一项就清一项，
    # 失败则保留在栈里，待托盘出现后提示用户重试。
    startup_restore_failures: list = []
    pending_on_startup = restore_stack.pending
    if pending_on_startup:
        logger.info("检测到未完成的恢复快照，开始恢复：%s", pending_on_startup)
        for property_name, value in pending_on_startup.items():
            action = _restore_action(property_name, value)
            if not action:
                continue
            result = action_executor.execute_action(action, {})
            if result.get("ok"):
                restore_stack.clear_pending_property(property_name)
            else:
                startup_restore_failures.append((property_name, result))
                logger.warning(
                    "启动恢复 %s 失败：%s", property_name, result.get("error")
                )

    # Late-bound failure notifier: the tray does not exist yet when
    # the handler is built, so the callback reads the holder at call
    # time (review bug 16: access-denied kills get a tray prompt).
    failure_notifier: dict = {}
    pause_flag: dict = {"paused": False}
    shutdown_state: dict = {"shutting_down": False}

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
                "请以管理员身份运行小马联动后再试。"
            )

    def _on_restore_failure(property_name, result) -> None:
        notify = failure_notifier.get("notify")
        if notify is not None:
            notify(f"恢复 {property_name} 失败：{result.get('error', '未知错误')}")

    handle_event = make_event_handler(
        config_manager,
        rule_engine,
        action_executor,
        dry_run=args.dry_run,
        on_action_failure=_on_action_failure,
        restore_stack=restore_stack,
        on_restore_failure=_on_restore_failure,
        pause_flag=pause_flag,
        confirmation=confirmation.confirm,
    )

    monitor = create_monitor(config_manager, args, handle_event)
    monitor.start()

    # 程序启动/退出触发器（需求 2.2 + 4.3）。
    from process_watcher import create_process_watcher

    on_program_exited = make_program_exit_handler(
        config_manager,
        action_executor,
        restore_stack,
        on_restore_failure=_on_restore_failure,
    )
    process_watcher = create_process_watcher(
        collect_watched_programs(config_manager.get_rules()),
        on_started=lambda name: handle_event(
            {"type": "on_program_started", "program": name}
        ),
        on_exited=on_program_exited,
    )

    def refresh_process_watcher() -> None:
        ensure_process_watcher_running(
            process_watcher, collect_watched_programs(config_manager.get_rules())
        )

    ensure_process_watcher_running(
        process_watcher, collect_watched_programs(config_manager.get_rules())
    )

    def _shutdown() -> None:
        """退出清理（P0-7）：先停投递再停线程，等待线程退出后才销毁托盘。"""
        shutdown_state["shutting_down"] = True
        # 停止接收新事件/新通知，避免后台线程在退出期间触碰 Qt 对象。
        try:
            monitor.on_event = None
        except Exception:
            pass
        try:
            process_watcher.on_started = None
            process_watcher.on_exited = None
        except Exception:
            pass
        try:
            monitor.stop()
        except Exception:
            logger.debug("停止 Wi-Fi 监控失败", exc_info=True)
        try:
            process_watcher.stop()
        except Exception:
            logger.debug("停止进程监听失败", exc_info=True)

    from main_window import MainWindow
    from tray_app import TrayApp

    main_window = MainWindow(config_manager, monitor, on_rules_changed=refresh_process_watcher)
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
        on_reload=refresh_process_watcher,
        pause_flag=pause_flag,
        on_quit=_shutdown,
    )
    tray.show()
    failure_notifier["notify"] = lambda message: (
        None
        if shutdown_state["shutting_down"]
        else tray.notify("小马联动 提醒", message)
    )

    # 启动恢复失败在托盘出现后补一次“必达”提醒（P0-4：不静默丢失）。
    for property_name, result in startup_restore_failures:
        failure_notifier["notify"](
            f"启动恢复 {property_name} 失败：{result.get('error', '未知错误')}，"
            "已保留待恢复状态，可在托盘重新加载后重试。"
        )

    # 需求 4.1：启动时配置损坏、已从 .bak 恢复，托盘报警告知用户。
    if getattr(config_manager, "_recovered_from_backup", False):
        tray.show_message(
            "小马联动 提醒",
            "配置文件已损坏，已自动从备份恢复。请检查规则是否完整。",
        )

    # A second launch wakes this instance (U1): bring the window
    # forward instead of starting a second monitor/tray.
    guard.show_requested.connect(main_window.show_from_tray)

    # One-shot startup guard (R4): if the baseline network is not
    # covered by any rule while ClashMi is on, remind — never auto-close.
    main_window.start_startup_guard()

    app.aboutToQuit.connect(_shutdown)

    def handle_signal(signum: int, frame: object) -> None:
        logger.info("Received signal %s; stopping", signum)
        stop_event.set()
        _shutdown()
        app.quit()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    logger.info("小马联动 GUI 已启动")
    return app.exec()


def run_console(config_manager: ConfigManager, args: argparse.Namespace) -> int:
    logger.info("以控制台模式运行")

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

    logger.info("小马联动已停止")
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
    logger.info("小马联动 v%s 启动", APP_VERSION)

    if args.once:
        return run_once(config_manager)

    if args.no_tray:
        return run_console(config_manager, args)

    return run_gui(config_manager, args)


if __name__ == "__main__":
    raise SystemExit(main())
