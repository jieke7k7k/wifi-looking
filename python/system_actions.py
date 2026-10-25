"""小马联动 v1.0.0 系统动作模块（新建，不改动旧模块）。

约定：
- 所有 Windows 专属导入一律惰性/受保护导入，本模块在 Linux 下必须能
  正常 import；非 Windows 下 Windows 专属动作一律报 unsupported。
- execute_system_action 预期失败一律返回 ok=False，绝不抛异常。
- 存疑动作（音频默认设备、勿扰）能力检测默认 unsupported，绝不伪造成功；
  其在真实 Windows 上的表现仍需真机验证，见模块末尾 UNVERIFIED_NOTES。
- 亮度、音量执行后必须读回验证，读回不符即按失败处理。

动作 type（定死）与参数：
  wifi.set            {enabled: bool}              网卡管理状态开关
  bluetooth.set       {enabled: bool}              WinRT 无线电总开关
  brightness.set      {value: int 0-100}
  dark_mode.set       {mode: "dark"|"light"}
  wallpaper.set       {path: str}                  必须是绝对路径
  volume.set          {value: int 0-100}
  volume.mute         {muted: bool}
  audio.device.set    {device: str}                无公开接口，恒 unsupported
  power.plan.set      {plan: str}                  名字或 GUID，GUID 须来自枚举
  screen.timeout.set  {minutes: int >=0, plugged: bool}
  dnd.set             {enabled: bool}              无公开接口，恒 unsupported
  lock_screen         {}
  sleep               {}
  shutdown            {}
  restart             {}
  program.launch      {path: str, args: list}
  program.close       {name|pid, graceful: bool}
  process.kill        {name|pid, path_contains: str}  按名杀必须有路径过滤
"""

from __future__ import annotations

import ctypes
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

IS_WINDOWS = sys.platform == "win32"

ACTION_TYPES = (
    "wifi.set",
    "bluetooth.set",
    "brightness.set",
    "dark_mode.set",
    "wallpaper.set",
    "volume.set",
    "volume.mute",
    "audio.device.set",
    "power.plan.set",
    "screen.timeout.set",
    "dnd.set",
    "lock_screen",
    "sleep",
    "shutdown",
    "restart",
    "program.launch",
    "program.close",
    "process.kill",
)

# 高危动作（睡眠/关机/重启）：执行前必须经 15 秒置顶无焦点倒计时横幅
# 确认（见 banner.py）。锁屏归为常规动作，无需二次确认。
HIGH_RISK_ACTION_TYPES = ("sleep", "shutdown", "restart")

# Windows 专属动作（非 Windows 下一律 unsupported）。
_WINDOWS_ONLY = frozenset(
    {
        "wifi.set",
        "bluetooth.set",
        "brightness.set",
        "dark_mode.set",
        "wallpaper.set",
        "volume.set",
        "volume.mute",
        "audio.device.set",
        "power.plan.set",
        "screen.timeout.set",
        "dnd.set",
        "lock_screen",
        "sleep",
        "shutdown",
        "restart",
    }
)

UNVERIFIED_NOTES = (
    "以下结论未经真机验证，不得写进规格当定论："
    "WinRT Radio 在 PyInstaller 打包的桌面 exe 上是否返回 Allowed；"
    "WMI 亮度 / 电源计划 / 屏幕超时在普通用户下的真实权限表现；"
    "音频默认设备切换、勿扰开关均无公开接口，本模块直接报 unsupported。"
)


def _reason_windows_only() -> str:
    return f"仅支持 Windows，当前平台为 {sys.platform}，无对应系统接口"


def _spec_available(module_name: str) -> bool:
    try:
        import importlib.util

        return importlib.util.find_spec(module_name) is not None
    except Exception:
        return False


# ---------------------------------------------------------------------------
# 通用小工具（便于测试注入）
# ---------------------------------------------------------------------------

def _run_cmd(argv: List[str], timeout: float = 10.0) -> subprocess.CompletedProcess:
    """跑一条系统命令；Windows 下不弹黑框。测试可 patch 本函数。"""
    kwargs: Dict[str, Any] = {}
    if IS_WINDOWS:
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    return subprocess.run(
        argv,
        capture_output=True,
        timeout=timeout,
        encoding="utf-8",
        errors="replace",
        **kwargs,
    )


def _result(
    action_type: str, ok: bool, error: Optional[str] = None, **details: Any
) -> Dict[str, Any]:
    out: Dict[str, Any] = {"ok": ok, "action": action_type, "error": error}
    out["details"] = details
    return out


def _as_bool(value: Any, default: Optional[bool] = None) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        if value.strip().lower() in ("true", "1", "yes", "on"):
            return True
        if value.strip().lower() in ("false", "0", "no", "off"):
            return False
    return default


def _as_int(value: Any) -> Optional[int]:
    try:
        if isinstance(value, bool):
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _close_enough(target: float, actual: float, tolerance: float) -> bool:
    return abs(target - actual) <= tolerance


# ---------------------------------------------------------------------------
# 能力检测
# ---------------------------------------------------------------------------

def detect_capabilities() -> Dict[str, Dict[str, Any]]:
    """返回 {动作类型: {"supported": bool, "reason": str}}。"""
    caps: Dict[str, Dict[str, Any]] = {}

    def put(name: str, supported: bool, reason: str) -> None:
        caps[name] = {"supported": supported, "reason": reason}

    if not IS_WINDOWS:
        for name in ACTION_TYPES:
            if name in _WINDOWS_ONLY:
                put(name, False, _reason_windows_only())
        put("program.launch", True, "subprocess 跨平台可用")
        put(
            "program.close",
            _spec_available("psutil"),
            "psutil 可用时支持优雅关闭" if _spec_available("psutil") else "缺少 psutil，无法按 pid/名定位进程",
        )
        put(
            "process.kill",
            _spec_available("psutil"),
            "psutil 可用时支持；按名杀仍须 path_contains 路径过滤"
            if _spec_available("psutil")
            else "缺少 psutil，无法按名/路径定位进程",
        )
        # 存疑动作在任何平台都一样：无公开接口（且本身就是 Windows 专属）。
        put("audio.device.set", False, "仅支持 Windows，且无公开官方接口（IPolicyConfig 未公开），绝不伪造成功")
        put("dnd.set", False, "仅支持 Windows，且无公开官方接口（注册表键在 Win11 上是否生效未经真机验证），绝不伪造成功")
        return caps

    # ---- Windows ----
    put(
        "wifi.set",
        shutil.which("netsh") is not None,
        "netsh 可用时支持网卡管理状态开关（启用/禁用网卡需管理员权限）"
        if shutil.which("netsh")
        else "未找到 netsh",
    )
    winrt_ok = _spec_available("winsdk") or _spec_available("winrt")
    put(
        "bluetooth.set",
        winrt_ok,
        "WinRT Windows.Devices.Radios 无线电总开关（需系统允许应用控制无线电；"
        "桌面 exe 是否返回 Allowed 未经真机验证）"
        if winrt_ok
        else "缺少 WinRT 绑定（winsdk 或 winrt 包），无法调用 Radio API",
    )
    bright_ok = _spec_available("wmi") or _spec_available("monitorcontrol")
    put(
        "brightness.set",
        bright_ok,
        "内置屏走 WMI，外接屏走 DDC/CI（monitorcontrol）；普通用户权限未经真机验证"
        if bright_ok
        else "缺少 wmi 与 monitorcontrol，无法读写亮度",
    )
    put("dark_mode.set", True, "HKCU Personalize 双键 + WM_SETTINGCHANGE 广播")
    put("wallpaper.set", True, "SystemParametersInfoW，须绝对路径")
    pycaw_ok = _spec_available("pycaw")
    put(
        "volume.set",
        pycaw_ok,
        "pycaw IAudioEndpointVolume，执行后读回验证"
        if pycaw_ok
        else "缺少 pycaw，无法读写主音量",
    )
    put(
        "volume.mute",
        pycaw_ok,
        "pycaw IAudioEndpointVolume，执行后读回验证"
        if pycaw_ok
        else "缺少 pycaw，无法读写静音状态",
    )
    put("audio.device.set", False, "无公开官方接口（IPolicyConfig 未公开），绝不伪造成功")
    powercfg_ok = shutil.which("powercfg") is not None
    put(
        "power.plan.set",
        powercfg_ok,
        "powercfg /setactive，GUID 必须从 powercfg /list 枚举取得；权限表现未经真机验证"
        if powercfg_ok
        else "未找到 powercfg",
    )
    put(
        "screen.timeout.set",
        powercfg_ok,
        "powercfg /change monitor-timeout-ac|dc；可能需要管理员权限，未经真机验证"
        if powercfg_ok
        else "未找到 powercfg",
    )
    put("dnd.set", False, "无公开官方接口（注册表键在 Win11 上是否生效未经真机验证），绝不伪造成功")
    put("lock_screen", True, "user32 LockWorkStation")
    put("sleep", True, "powrprof SetSuspendState（BOOLEAN 参数，禁用休眠语义）")
    put("shutdown", True, "ExitWindowsEx（执行前启用关机特权），失败时回落 shutdown.exe")
    put("restart", True, "ExitWindowsEx（执行前启用关机特权），失败时回落 shutdown.exe")
    put("program.launch", True, "subprocess 列表传参，空格/中文路径不拼接命令行")
    put("program.close", True, "Windows 下 GUI 程序先发 WM_CLOSE 再 terminate 再 kill")
    put("process.kill", True, "按名杀必须带 path_contains 路径过滤（保守模型）")
    return caps


def _capability(action_type: str) -> Tuple[bool, str]:
    caps = detect_capabilities()
    entry = caps.get(action_type)
    if entry is None:
        return False, f"未知动作类型：{action_type}"
    return bool(entry["supported"]), str(entry["reason"])


def capability_report() -> str:
    """生成系统动作能力自检报告（P0-2：打包后落盘/展示用）。

    只读、不执行动作、不触发网络；逐行给出 ``动作类型: 支持状态 (原因)``。
    打包后无论是否具备蓝牙/亮度/音量依赖，都能据此快速定位能力缺口。
    """
    caps = detect_capabilities()
    lines = []
    for name in ACTION_TYPES:
        entry = caps.get(name, {"supported": False, "reason": "未检测"})
        lines.append(
            f"{name}: {'supported' if entry['supported'] else 'unsupported'} "
            f"({entry.get('reason', '')})"
        )
    return "\n".join(lines)


def log_capability_report() -> str:
    """把能力自检结果写入日志并返回报告文本。"""
    report = capability_report()
    logger.info("系统动作能力自检：\n%s", report)
    return report


# ---------------------------------------------------------------------------
# Windows 底层后端（全部可被测试 patch；非 Windows 下不调用）
# ---------------------------------------------------------------------------

def _enable_shutdown_privilege() -> bool:
    """启用当前进程令牌的 SeShutdownPrivilege，成功返回 True。"""
    if not IS_WINDOWS:
        return False
    try:
        advapi32 = ctypes.windll.advapi32
        kernel32 = ctypes.windll.kernel32

        class LUID(ctypes.Structure):
            _fields_ = [("LowPart", ctypes.c_ulong), ("HighPart", ctypes.c_long)]

        class LUID_AND_ATTRIBUTES(ctypes.Structure):
            _fields_ = [("Luid", LUID), ("Attributes", ctypes.c_ulong)]

        class TOKEN_PRIVILEGES(ctypes.Structure):
            _fields_ = [
                ("PrivilegeCount", ctypes.c_ulong),
                ("Privileges", LUID_AND_ATTRIBUTES * 1),
            ]

        TOKEN_ADJUST_PRIVILEGES = 0x0020
        TOKEN_QUERY = 0x0008
        SE_PRIVILEGE_ENABLED = 0x00000002
        token = ctypes.c_void_p()
        proc = kernel32.GetCurrentProcess()
        if not advapi32.OpenProcessToken(
            proc, TOKEN_ADJUST_PRIVILEGES | TOKEN_QUERY, ctypes.byref(token)
        ):
            return False
        try:
            luid = LUID()
            if not advapi32.LookupPrivilegeValueW(
                None, "SeShutdownPrivilege", ctypes.byref(luid)
            ):
                return False
            privs = TOKEN_PRIVILEGES()
            privs.PrivilegeCount = 1
            privs.Privileges[0].Luid = luid
            privs.Privileges[0].Attributes = SE_PRIVILEGE_ENABLED
            if not advapi32.AdjustTokenPrivileges(
                token, False, ctypes.byref(privs), 0, None, None
            ):
                return False
            return kernel32.GetLastError() == 0
        finally:
            kernel32.CloseHandle(token)
    except Exception:
        logger.debug("启用关机特权失败", exc_info=True)
        return False


def _broadcast_setting_change() -> None:
    """广播 WM_SETTINGCHANGE（主题类变更后通知各应用）。"""
    if not IS_WINDOWS:
        return
    try:
        user32 = ctypes.windll.user32
        HWND_BROADCAST = 0xFFFF
        WM_SETTINGCHANGE = 0x001A
        SMTO_ABORTIFNOTHUNG = 0x0002
        lparam = ctypes.create_unicode_buffer("ImmersiveColorSet")
        result = ctypes.c_ulong(0)
        user32.SendMessageTimeoutW(
            HWND_BROADCAST,
            WM_SETTINGCHANGE,
            0,
            ctypes.cast(lparam, ctypes.c_void_p),
            SMTO_ABORTIFNOTHUNG,
            1000,
            ctypes.byref(result),
        )
    except Exception:
        logger.debug("WM_SETTINGCHANGE 广播失败", exc_info=True)


def _wmi_set_brightness(value: int) -> None:
    import wmi  # type: ignore

    conn = wmi.WMI(namespace=r"root\WMI")
    methods = conn.WmiMonitorBrightnessMethods()
    if not methods:
        raise RuntimeError("未找到 WmiMonitorBrightnessMethods（可能不是内置屏）")
    methods[0].WmiSetBrightness(1, value)


def _wmi_get_brightness() -> Optional[int]:
    try:
        import wmi  # type: ignore

        conn = wmi.WMI(namespace=r"root\WMI")
        rows = conn.WmiMonitorBrightness()
        if not rows:
            return None
        current = rows[0].CurrentBrightness
        return int(current) if current is not None else None
    except Exception:
        return None


def _ddc_set_brightness(value: int) -> None:
    from monitorcontrol import get_monitors  # type: ignore

    monitors = get_monitors()
    if not monitors:
        raise RuntimeError("未找到支持 DDC/CI 的外接显示器")
    for monitor in monitors:
        with monitor:
            monitor.set_luminance(value)


def _ddc_get_brightness() -> Optional[int]:
    try:
        from monitorcontrol import get_monitors  # type: ignore

        monitors = get_monitors()
        if not monitors:
            return None
        with monitors[0]:
            return int(monitors[0].get_luminance())
    except Exception:
        return None


def _volume_endpoint():
    """返回 pycaw IAudioEndpointVolume 接口；失败抛异常。"""
    from comtypes import CLSCTX_ALL, POINTER, cast  # type: ignore
    from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume  # type: ignore

    devices = AudioUtilities.GetSpeakers()
    interface = devices.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
    return cast(interface, POINTER(IAudioEndpointVolume))


def _volume_get_scalar() -> Optional[float]:
    try:
        return float(_volume_endpoint().GetMasterVolumeLevelScalar())
    except Exception:
        return None


def _volume_set_scalar(value: float) -> None:
    _volume_endpoint().SetMasterVolumeLevelScalar(value, None)


def _volume_get_mute() -> Optional[bool]:
    try:
        return bool(_volume_endpoint().GetMute())
    except Exception:
        return None


def _volume_set_mute(muted: bool) -> None:
    _volume_endpoint().SetMute(muted, None)


def _radio_set_bluetooth(enabled: bool) -> Tuple[bool, str]:
    """WinRT 无线电总开关。严禁逐个枚举断开蓝牙设备。

    返回 (ok, detail)。winsdk 不可用时返回失败，不伪造成功。
    """
    try:
        import asyncio

        from winsdk.windows.devices.radios import (  # type: ignore
            Radio,
            RadioAccessStatus,
            RadioKind,
            RadioState,
        )

        async def _run() -> Tuple[bool, str]:
            access = await Radio.request_access_async()
            if access != RadioAccessStatus.ALLOWED:
                return False, f"系统拒绝应用控制无线电（access={access}），请检查隐私设置"
            radios = await Radio.get_radios_async()
            target = None
            for radio in radios:
                if radio.kind == RadioKind.BLUETOOTH:
                    target = radio
                    break
            if target is None:
                return False, "未找到蓝牙无线电设备"
            await target.set_state_async(
                RadioState.ON if enabled else RadioState.OFF
            )
            state = target.state
            want = RadioState.ON if enabled else RadioState.OFF
            if state != want:
                return False, f"读回无线电状态不符：期望 {want}，实际 {state}"
            return True, f"蓝牙无线电已{'开启' if enabled else '关闭'}"

        return asyncio.run(_run())
    except ImportError as exc:
        return False, f"缺少 WinRT 绑定（winsdk）：{exc}"
    except Exception as exc:
        return False, f"蓝牙无线电开关失败：{exc}"


def _wifi_interface_name() -> Optional[str]:
    """从 netsh 输出中找出 Wi-Fi 网卡名；找不到返回 None。"""
    try:
        proc = _run_cmd(["netsh", "interface", "show", "interface"], timeout=10)
    except Exception:
        return None
    output = (proc.stdout or "") + (proc.stderr or "")
    for line in output.splitlines():
        lowered = line.lower()
        if "wi-fi" in lowered or "wlan" in lowered or "无线" in line:
            # 行尾即接口名（接口名本身可能含空格，故取类型列之后的部分）。
            for token in ("dedicated", "loopback", "internal", "专用", "环回"):
                idx = lowered.find(token)
                if idx >= 0:
                    name = line[idx + len(token):].strip()
                    if name:
                        return name
            parts = line.split()
            if parts:
                return parts[-1]
    return None


def _parse_powercfg_list(output: str) -> List[Tuple[str, str]]:
    """解析 powercfg /list 输出 -> [(guid, name)]。GUID 绝不硬编码。"""
    plans: List[Tuple[str, str]] = []
    guid_re = re.compile(
        r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})"
    )
    for line in output.splitlines():
        match = guid_re.search(line)
        if not match:
            continue
        guid = match.group(1).lower()
        name = ""
        paren = re.search(r"\(([^)]*)\)", line[match.end():])
        if paren:
            name = paren.group(1).strip()
        plans.append((guid, name))
    return plans


def _resolve_power_plan(want: str) -> Tuple[Optional[str], List[Tuple[str, str]], str]:
    """按名字或 GUID 在枚举结果中定位电源计划。返回 (guid, plans, error)。"""
    try:
        proc = _run_cmd(["powercfg", "/list"], timeout=10)
    except Exception as exc:
        return None, [], f"枚举电源计划失败：{exc}"
    if proc.returncode != 0:
        return None, [], f"powercfg /list 失败：{(proc.stderr or proc.stdout or '').strip()}"
    plans = _parse_powercfg_list(proc.stdout or "")
    if not plans:
        return None, [], "powercfg /list 未返回任何电源计划"
    want_norm = want.strip().lower()
    for guid, name in plans:
        if want_norm == guid:
            return guid, plans, ""
    for guid, name in plans:
        if want_norm and want_norm in name.lower():
            return guid, plans, ""
    aliases = {
        "balanced": ("平衡", "balanced"),
        "high performance": ("高性能", "high performance"),
        "power saver": ("节能", "power saver"),
        "节能": ("节能", "power saver"),
        "平衡": ("平衡", "balanced"),
        "高性能": ("高性能", "high performance"),
    }
    for alias in aliases.get(want_norm, ()):
        for guid, name in plans:
            if alias in name.lower():
                return guid, plans, ""
    return None, plans, f"未找到电源计划：{want}（枚举到 {[n for _, n in plans]}）"


def _iter_processes():
    try:
        import psutil  # type: ignore

        return list(psutil.process_iter(attrs=["pid", "name", "exe"]))
    except Exception:
        return []


def _close_windows_for_pid(pid: int) -> bool:
    """向该 pid 的全部顶层窗口发 WM_CLOSE；无窗口/无 win32gui 返回 False。"""
    if not IS_WINDOWS:
        return False
    try:
        import win32gui  # type: ignore
        import win32con  # type: ignore
    except Exception:
        return False
    closed_any = False
    try:
        def _enum(hwnd, _extra):
            nonlocal closed_any
            try:
                _, win_pid = win32gui.GetWindowThreadProcessId(hwnd)
                if win_pid == pid and win32gui.IsWindowVisible(hwnd):
                    win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
                    closed_any = True
            except Exception:
                pass
            return True

        win32gui.EnumWindows(_enum, None)
    except Exception:
        return False
    return closed_any


def _close_process(proc: Any, graceful: bool, timeout: float = 3.0) -> Tuple[bool, str]:
    """优雅关闭单个进程对象：graceful 时先 WM_CLOSE/terminate 再 kill。"""
    try:
        pid = int(proc.pid)
    except Exception:
        return False, "无法取得 pid"
    if graceful and IS_WINDOWS:
        _close_windows_for_pid(pid)
    if graceful:
        try:
            proc.terminate()
        except Exception as exc:
            return False, f"terminate 失败：{exc}"
        try:
            proc.wait(timeout=timeout)
            return True, "graceful"
        except Exception:
            pass
    try:
        proc.kill()
    except Exception as exc:
        return False, f"kill 失败：{exc}"
    try:
        proc.wait(timeout=timeout)
    except Exception:
        pass
    return True, "killed"


# ---------------------------------------------------------------------------
# 各动作实现（每个返回 _result；不抛异常到调用方）
# ---------------------------------------------------------------------------

def _do_wifi_set(action: Dict[str, Any]) -> Dict[str, Any]:
    enabled = _as_bool(action.get("enabled"))
    if enabled is None:
        return _result("wifi.set", False, "enabled 必须是布尔值")
    name = _wifi_interface_name() or "Wi-Fi"
    state = "enabled" if enabled else "disabled"
    try:
        proc = _run_cmd(
            ["netsh", "interface", "set", "interface", f"name={name}", f"admin={state}"],
            timeout=15,
        )
    except Exception as exc:
        return _result("wifi.set", False, f"netsh 执行失败：{exc}", interface=name)
    if proc.returncode != 0:
        return _result(
            "wifi.set", False,
            f"netsh 返回 {proc.returncode}：{(proc.stderr or proc.stdout or '').strip()}",
            interface=name,
        )
    return _result("wifi.set", True, None, interface=name, enabled=enabled)


def _do_bluetooth_set(action: Dict[str, Any]) -> Dict[str, Any]:
    enabled = _as_bool(action.get("enabled"))
    if enabled is None:
        return _result("bluetooth.set", False, "enabled 必须是布尔值")
    ok, detail = _radio_set_bluetooth(enabled)
    return _result("bluetooth.set", ok, None if ok else detail, enabled=enabled, detail=detail)


def _do_brightness_set(action: Dict[str, Any]) -> Dict[str, Any]:
    value = _as_int(action.get("value", action.get("brightness")))
    if value is None or not 0 <= value <= 100:
        return _result("brightness.set", False, "value 必须是 0-100 的整数")
    errors: List[str] = []
    try:
        _wmi_set_brightness(value)
        actual = _wmi_get_brightness()
        if actual is not None:
            if _close_enough(value, actual, 1):
                return _result("brightness.set", True, None, value=value, readback=actual, backend="wmi")
            return _result("brightness.set", False, f"亮度读回不符：期望 {value}，实际 {actual}", value=value, readback=actual, backend="wmi")
        return _result("brightness.set", True, None, value=value, readback=None, backend="wmi", note="WMI 写入成功但读回为空")
    except Exception as exc:
        errors.append(f"WMI：{exc}")
    try:
        _ddc_set_brightness(value)
        actual = _ddc_get_brightness()
        if actual is not None and not _close_enough(value, actual, 2):
            return _result("brightness.set", False, f"亮度读回不符：期望 {value}，实际 {actual}", value=value, readback=actual, backend="ddc")
        return _result("brightness.set", True, None, value=value, readback=actual, backend="ddc")
    except Exception as exc:
        errors.append(f"DDC/CI：{exc}")
    return _result("brightness.set", False, "亮度设置失败：" + "；".join(errors), value=value)


def _do_dark_mode_set(action: Dict[str, Any]) -> Dict[str, Any]:
    mode = str(action.get("mode", "")).strip().lower()
    if mode not in ("dark", "light"):
        return _result("dark_mode.set", False, "mode 必须是 dark 或 light")
    light_value = 0 if mode == "dark" else 1
    try:
        import winreg  # type: ignore

        key_path = r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, "AppsUseLightTheme", 0, winreg.REG_DWORD, light_value)
            winreg.SetValueEx(key, "SystemUsesLightTheme", 0, winreg.REG_DWORD, light_value)
    except Exception as exc:
        return _result("dark_mode.set", False, f"写注册表失败：{exc}", mode=mode)
    _broadcast_setting_change()
    return _result("dark_mode.set", True, None, mode=mode)


def _do_wallpaper_set(action: Dict[str, Any]) -> Dict[str, Any]:
    path = str(action.get("path", "")).strip()
    if not path:
        return _result("wallpaper.set", False, "path 不能为空")
    if not os.path.isabs(path):
        return _result("wallpaper.set", False, "壁纸路径必须是绝对路径")
    if not os.path.exists(path):
        return _result("wallpaper.set", False, f"壁纸文件不存在：{path}")
    try:
        SPI_SETDESKWALLPAPER = 20
        SPIF_UPDATEINIFILE = 0x01
        SPIF_SENDWININICHANGE = 0x02
        ok = ctypes.windll.user32.SystemParametersInfoW(
            SPI_SETDESKWALLPAPER, 0, path, SPIF_UPDATEINIFILE | SPIF_SENDWININICHANGE
        )
    except Exception as exc:
        return _result("wallpaper.set", False, f"SystemParametersInfoW 调用失败：{exc}", path=path)
    if not ok:
        return _result("wallpaper.set", False, "SystemParametersInfoW 返回失败", path=path)
    return _result("wallpaper.set", True, None, path=path)


def _do_volume_set(action: Dict[str, Any]) -> Dict[str, Any]:
    value = _as_int(action.get("value", action.get("volume")))
    if value is None or not 0 <= value <= 100:
        return _result("volume.set", False, "value 必须是 0-100 的整数")
    try:
        _volume_set_scalar(value / 100.0)
    except Exception as exc:
        return _result("volume.set", False, f"音量设置失败：{exc}", value=value)
    actual = _volume_get_scalar()
    if actual is not None and not _close_enough(value, actual * 100.0, 2):
        return _result("volume.set", False, f"音量读回不符：期望 {value}，实际 {round(actual * 100)}", value=value, readback=round(actual * 100))
    return _result("volume.set", True, None, value=value, readback=(round(actual * 100) if actual is not None else None))


def _do_volume_mute(action: Dict[str, Any]) -> Dict[str, Any]:
    muted = _as_bool(action.get("muted"))
    if muted is None:
        return _result("volume.mute", False, "muted 必须是布尔值")
    try:
        _volume_set_mute(muted)
    except Exception as exc:
        return _result("volume.mute", False, f"静音设置失败：{exc}", muted=muted)
    actual = _volume_get_mute()
    if actual is not None and actual != muted:
        return _result("volume.mute", False, f"静音读回不符：期望 {muted}，实际 {actual}", muted=muted, readback=actual)
    return _result("volume.mute", True, None, muted=muted, readback=actual)


def _do_power_plan_set(action: Dict[str, Any]) -> Dict[str, Any]:
    want = str(action.get("plan", action.get("name", action.get("guid", "")))).strip()
    if not want:
        return _result("power.plan.set", False, "plan 不能为空（名字或 GUID）")
    guid, _plans, error = _resolve_power_plan(want)
    if guid is None:
        return _result("power.plan.set", False, error, plan=want)
    try:
        proc = _run_cmd(["powercfg", "/setactive", guid], timeout=15)
    except Exception as exc:
        return _result("power.plan.set", False, f"powercfg /setactive 失败：{exc}", plan=want, guid=guid)
    if proc.returncode != 0:
        return _result("power.plan.set", False, f"powercfg /setactive 返回 {proc.returncode}：{(proc.stderr or proc.stdout or '').strip()}", plan=want, guid=guid)
    try:
        active = _run_cmd(["powercfg", "/getactivescheme"], timeout=10)
        if guid not in (active.stdout or "").lower():
            return _result("power.plan.set", False, "电源计划读回不符：当前活动计划不是目标 GUID", plan=want, guid=guid)
    except Exception:
        pass
    return _result("power.plan.set", True, None, plan=want, guid=guid)


def _do_screen_timeout_set(action: Dict[str, Any]) -> Dict[str, Any]:
    minutes = _as_int(action.get("minutes", action.get("timeout_minutes")))
    if minutes is None or minutes < 0:
        return _result("screen.timeout.set", False, "minutes 必须是不小于 0 的整数（0 表示从不）")
    plugged = _as_bool(action.get("plugged"), True)
    assert plugged is not None
    flag = "monitor-timeout-ac" if plugged else "monitor-timeout-dc"
    try:
        proc = _run_cmd(["powercfg", "/change", flag, str(minutes)], timeout=15)
    except Exception as exc:
        return _result("screen.timeout.set", False, f"powercfg 执行失败：{exc}", minutes=minutes, plugged=plugged)
    if proc.returncode != 0:
        return _result("screen.timeout.set", False, f"powercfg 返回 {proc.returncode}：{(proc.stderr or proc.stdout or '').strip()}", minutes=minutes, plugged=plugged)
    return _result("screen.timeout.set", True, None, minutes=minutes, plugged=plugged)


def _do_lock_screen(_action: Dict[str, Any]) -> Dict[str, Any]:
    try:
        ok = ctypes.windll.user32.LockWorkStation()
    except Exception as exc:
        return _result("lock_screen", False, f"LockWorkStation 调用失败：{exc}")
    if not ok:
        return _result("lock_screen", False, "LockWorkStation 返回失败")
    return _result("lock_screen", True, None)


def _do_sleep(_action: Dict[str, Any]) -> Dict[str, Any]:
    priv = _enable_shutdown_privilege()
    try:
        powrprof = ctypes.windll.powrprof
        powrprof.SetSuspendState.argtypes = [ctypes.c_ubyte, ctypes.c_ubyte, ctypes.c_ubyte]
        powrprof.SetSuspendState.restype = ctypes.c_ubyte
        ok = powrprof.SetSuspendState(0, 0, 0)
    except Exception as exc:
        return _result("sleep", False, f"SetSuspendState 调用失败：{exc}", privilege_enabled=priv)
    if not ok:
        return _result("sleep", False, "SetSuspendState 返回失败", privilege_enabled=priv)
    return _result("sleep", True, None, privilege_enabled=priv)


def _do_shutdown_or_restart(action_type: str, _action: Dict[str, Any]) -> Dict[str, Any]:
    """关机/重启：必须先启用关机特权；禁止默认强制关闭应用。"""
    if not _enable_shutdown_privilege():
        return _result(action_type, False, "启用关机特权失败（SeShutdownPrivilege），请以管理员身份运行后重试")
    flag = 0x00000001 if action_type == "shutdown" else 0x00000002
    try:
        ok = ctypes.windll.user32.ExitWindowsEx(flag, 0)
    except Exception:
        ok = 0
    if ok:
        return _result(action_type, True, None, method="ExitWindowsEx")
    # 回落：shutdown.exe（本地关机一般普通用户可用）。
    argv = ["shutdown.exe", "/s" if action_type == "shutdown" else "/r", "/t", "0"]
    try:
        proc = _run_cmd(argv, timeout=10)
    except Exception as exc:
        return _result(action_type, False, f"ExitWindowsEx 与 shutdown.exe 均失败：{exc}")
    if proc.returncode != 0:
        return _result(action_type, False, f"shutdown.exe 返回 {proc.returncode}：{(proc.stderr or proc.stdout or '').strip()}")
    return _result(action_type, True, None, method="shutdown.exe")


def _do_program_launch(action: Dict[str, Any]) -> Dict[str, Any]:
    path = str(action.get("path", action.get("command", ""))).strip()
    if not path:
        return _result("program.launch", False, "path 不能为空")
    args = action.get("args", [])
    if isinstance(args, str):
        args = [args]
    if not isinstance(args, list):
        return _result("program.launch", False, "args 必须是列表")
    argv = [path, *[str(a) for a in args]]
    try:
        proc = subprocess.Popen(argv)
    except Exception as exc:
        return _result("program.launch", False, f"启动失败：{exc}", path=path)
    return _result("program.launch", True, None, path=path, pid=proc.pid)


def _find_process(name: Optional[str], pid: Optional[int]) -> List[Any]:
    found: List[Any] = []
    for proc in _iter_processes():
        try:
            info = proc.info or {}
        except Exception:
            continue
        if pid is not None and info.get("pid") == pid:
            found.append(proc)
        elif name is not None and str(info.get("name", "")).lower() == name.lower():
            found.append(proc)
    return found


def _do_program_close(action: Dict[str, Any]) -> Dict[str, Any]:
    name = action.get("name", action.get("process_name"))
    pid = _as_int(action.get("pid"))
    if not name and pid is None:
        return _result("program.close", False, "name 或 pid 必须提供其一")
    graceful = _as_bool(action.get("graceful"), True)
    assert graceful is not None
    targets = _find_process(str(name) if name else None, pid)
    if not targets:
        return _result("program.close", True, None, closed=[], note="目标进程已不在运行")
    closed: List[int] = []
    failed: List[Dict[str, Any]] = []
    for proc in targets:
        ok, detail = _close_process(proc, graceful)
        try:
            closed_pid = int(proc.pid)
        except Exception:
            closed_pid = -1
        if ok:
            closed.append(closed_pid)
        else:
            failed.append({"pid": closed_pid, "error": detail})
    if failed:
        return _result("program.close", False, "部分进程关闭失败", closed=closed, failed=failed)
    return _result("program.close", True, None, closed=closed)


def _do_process_kill(action: Dict[str, Any]) -> Dict[str, Any]:
    """保守模型：按名杀必须带 path_contains；按 pid 杀可附带路径校验。"""
    name = action.get("name", action.get("process_name"))
    pid = _as_int(action.get("pid"))
    path_contains = action.get("path_contains")
    if not name and pid is None:
        return _result("process.kill", False, "name 或 pid 必须提供其一")
    if name and not path_contains:
        return _result(
            "process.kill", False,
            "按名结束进程必须提供 path_contains 路径过滤，拒绝无差别按名强杀",
            name=str(name),
        )
    needle = str(path_contains).lower().replace("/", "\\") if path_contains else None
    targets: List[Any] = []
    for proc in _iter_processes():
        try:
            info = proc.info or {}
        except Exception:
            continue
        if pid is not None:
            if info.get("pid") != pid:
                continue
        elif name is not None:
            if str(info.get("name", "")).lower() != str(name).lower():
                continue
        if needle:
            exe = str(info.get("exe") or "").lower().replace("/", "\\")
            if needle not in exe:
                continue
        targets.append(proc)
    if not targets:
        return _result("process.kill", True, None, killed=[], note="无匹配进程（已不在运行或路径过滤未命中）")
    killed: List[int] = []
    failed: List[Dict[str, Any]] = []
    for proc in targets:
        try:
            proc.kill()
            try:
                proc.wait(timeout=3)
            except Exception:
                pass
            killed.append(int(proc.pid))
        except Exception as exc:
            failed.append({"pid": getattr(proc, "pid", None), "error": str(exc)})
    if failed:
        return _result("process.kill", False, "部分进程强杀失败", killed=killed, failed=failed)
    return _result("process.kill", True, None, killed=killed)


_HANDLERS: Dict[str, Callable[[Dict[str, Any]], Dict[str, Any]]] = {
    "wifi.set": _do_wifi_set,
    "bluetooth.set": _do_bluetooth_set,
    "brightness.set": _do_brightness_set,
    "dark_mode.set": _do_dark_mode_set,
    "wallpaper.set": _do_wallpaper_set,
    "volume.set": _do_volume_set,
    "volume.mute": _do_volume_mute,
    "power.plan.set": _do_power_plan_set,
    "screen.timeout.set": _do_screen_timeout_set,
    "lock_screen": _do_lock_screen,
    "sleep": _do_sleep,
    "shutdown": lambda a: _do_shutdown_or_restart("shutdown", a),
    "restart": lambda a: _do_shutdown_or_restart("restart", a),
    "program.launch": _do_program_launch,
    "program.close": _do_program_close,
    "process.kill": _do_process_kill,
}

_UNSUPPORTED_REASONS = {
    "audio.device.set": "无公开官方接口（IPolicyConfig 未公开），绝不伪造成功",
    "dnd.set": "无公开官方接口（注册表键在 Win11 上是否生效未经真机验证），绝不伪造成功",
}


def _radio_get_bluetooth() -> Optional[bool]:
    """读回蓝牙无线电总开关状态；不可用返回 None。"""
    try:
        import asyncio

        from winsdk.windows.devices.radios import (  # type: ignore
            Radio,
            RadioKind,
            RadioState,
        )

        async def _run() -> Optional[bool]:
            radios = await Radio.get_radios_async()
            for radio in radios:
                if radio.kind == RadioKind.BLUETOOTH:
                    return radio.state == RadioState.ON
            return None

        return asyncio.run(_run())
    except Exception:
        return None


def _registry_read_value(key_path: str, value_name: str) -> Optional[Any]:
    try:
        import winreg  # type: ignore

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
            value, _ = winreg.QueryValueEx(key, value_name)
            return value
    except Exception:
        return None


def _power_plan_active_guid() -> Optional[str]:
    try:
        out = _run_cmd(["powercfg", "/getactivescheme"]).stdout or ""
        match = re.search(r"([0-9a-fA-F]{36})", out)
        return match.group(1) if match else None
    except Exception:
        return None


def _screen_timeout_minutes(plugged: bool) -> Optional[int]:
    """读当前电源计划的屏幕超时（分钟）；解析失败返回 None。"""
    try:
        out = _run_cmd(
            ["powercfg", "/query", "SCHEME_CURRENT", "SUB_VIDEO", "VIDEOIDLE"]
        ).stdout or ""
        if plugged:
            pattern = r"(?:Current AC Power Setting Index|当前交流电源设置索引)\s*:\s*0x([0-9a-fA-F]+)"
        else:
            pattern = r"(?:Current DC Power Setting Index|当前直流电源设置索引)\s*:\s*0x([0-9a-fA-F]+)"
        match = re.search(pattern, out)
        if not match:
            return None
        return int(match.group(1), 16) // 60
    except Exception:
        return None


# 恢复栈可记录的属性名（与 restore_stack 的属性名一一对应）。
RESTORE_PROPERTIES = (
    "bluetooth_enabled",
    "brightness",
    "volume",
    "muted",
    "dark_mode",
    "wallpaper_path",
    "power_plan",
    "screen_timeout_minutes",
)


def read_property_value(property_name: str, action: Any = None) -> Any:
    """读取某系统属性的当前值，供恢复开关在动作执行前快照。

    读不到返回 None（调用方跳过该属性的记录，绝不伪造一个值）。
    screen_timeout_minutes 的值是 {"minutes": int, "plugged": bool}，
    plugged 取自触发动作的参数，默认 True。
    """
    if property_name == "bluetooth_enabled":
        return _radio_get_bluetooth()
    if property_name == "brightness":
        value = _wmi_get_brightness()
        return value if value is not None else _ddc_get_brightness()
    if property_name == "volume":
        scalar = _volume_get_scalar()
        return int(round(scalar * 100)) if scalar is not None else None
    if property_name == "muted":
        return _volume_get_mute()
    if property_name == "dark_mode":
        raw = _registry_read_value(
            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
            "AppsUseLightTheme",
        )
        if raw == 0:
            return "dark"
        if raw == 1:
            return "light"
        return None
    if property_name == "wallpaper_path":
        raw = _registry_read_value(r"Control Panel\Desktop", "Wallpaper")
        return str(raw) if raw else None
    if property_name == "power_plan":
        return _power_plan_active_guid()
    if property_name == "screen_timeout_minutes":
        plugged = True
        if isinstance(action, dict):
            plugged = _as_bool(action.get("plugged"), True)
            if plugged is None:
                plugged = True
        minutes = _screen_timeout_minutes(plugged)
        if minutes is None:
            return None
        return {"minutes": minutes, "plugged": plugged}
    return None


def execute_system_action(
    action: Any,
    dry_run: bool = False,
    confirmation: Optional[Callable[[Dict[str, Any]], bool]] = None,
) -> Dict[str, Any]:
    """执行一个系统动作。预期失败返回 ok=False，绝不抛异常。

    action 为 dict：{"type": "<动作类型>", ...参数}；dry_run=True 时只做
    参数与能力校验，不产生任何系统副作用（关机/重启/睡眠等高危动作
    在 dry_run 下一律不执行）。

    confirmation：高危动作（睡眠/关机/重启）执行前的确认回调，返回
    True 才继续执行；未提供时高危动作一律拒绝执行（绝不未经 15 秒
    倒计时横幅确认就触发），返回 requires_confirmation。回调由 UI 层
    注入（横幅倒计时归零 -> True，用户点击取消 -> False）。
    """
    try:
        if isinstance(action, str):
            action = {"type": action}
        if not isinstance(action, dict):
            return _result("", False, "action 必须是 dict")
        action_type = str(action.get("type", "")).strip()
        if not action_type:
            return _result("", False, "action.type 不能为空")
        if action_type not in ACTION_TYPES:
            return _result(action_type, False, f"未知动作类型：{action_type}")
        supported, reason = _capability(action_type)
        if not supported:
            return _result(action_type, False, reason)
        if dry_run:
            return _result(action_type, True, None, dry_run=True, capability=reason)
        if action_type in HIGH_RISK_ACTION_TYPES:
            if confirmation is None:
                return _result(
                    action_type,
                    False,
                    "高危动作需经 15 秒置顶无焦点倒计时横幅确认后方可执行",
                    requires_confirmation=True,
                )
            try:
                approved = bool(confirmation(action))
            except Exception as exc:
                return _result(action_type, False, f"确认回调异常：{exc}")
            if not approved:
                return _result(action_type, False, "用户取消了高危动作", cancelled=True)
        handler = _HANDLERS.get(action_type)
        if handler is None:
            # audio.device.set / dnd.set：能力检测已拦截，走到这里仍拒绝。
            return _result(action_type, False, _UNSUPPORTED_REASONS.get(action_type, "该动作暂不支持"))
        try:
            return handler(action)
        except Exception as exc:  # 兜底：单个动作失败绝不抛到调用方
            logger.warning("系统动作 %s 执行异常", action_type, exc_info=True)
            return _result(action_type, False, f"执行异常：{exc}")
    except Exception as exc:  # 连参数解析都失败时仍返回结构化结果
        return _result("", False, f"执行异常：{exc}")


__all__ = [
    "ACTION_TYPES",
    "HIGH_RISK_ACTION_TYPES",
    "IS_WINDOWS",
    "RESTORE_PROPERTIES",
    "UNVERIFIED_NOTES",
    "detect_capabilities",
    "execute_system_action",
    "read_property_value",
]
