"""ClashMi 四步原子关闭（小马联动 v1.0.0 核心修复项）。

步骤（严格按此顺序，单步失败不中断后续步骤）：
  ① API 优雅卸载：client.disable_tun()、client.close_connections()、关闭系统代理
  ② 关闭主窗口：向 ClashMi.exe 主窗口发 WM_CLOSE（win32gui 不可用则跳过并记原因）
  ③ 等待 1.5 秒（sleep 可注入）后检查，仍在的主进程或子内核进程按进程树强杀
     （Windows 用 taskkill /F /T；其它平台用 psutil 子孙递归）
  ④ 注册表代理兜底：HKCU Internet Settings 下 ProxyEnable 为 1 且 ProxyServer
     指向本地 ClashMi 端口时，强制置 0 并刷新（仅 Windows，非 Windows 跳过）

返回 {"ok": bool, "steps": [...]}：
  ok 当且仅当主进程已消失且代理已干净（非 Windows 下代理步骤跳过不计失败）。

安全说明（与旧版保守模型一致，特此明示，非悄悄降级）：
  内核进程名 mihomo.exe / clash-meta.exe 是通用名（其它客户端也在用），
  故按名清扫时只杀满足以下任一条件的：exe 路径含 clashmi，或是 ClashMi
  主进程的子孙进程。不满足的记入 skipped，绝不无差别强杀。

全部外部依赖（sleep、进程快照、窗口关闭、注册表读写、进程树强杀）均可
通过参数注入，便于单元测试；默认实现走 Windows/系统路线。
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

IS_WINDOWS = sys.platform == "win32"

MAIN_PROCESS_NAMES = ("ClashMi.exe",)
KERNEL_PROCESS_NAMES = ("mihomo.exe", "clash-meta.exe")
WAIT_AFTER_WINDOW_CLOSE_SECONDS = 1.5

PROXY_KEY_PATH = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"


def _path_looks_like_clashmi(exe_path: Optional[str]) -> bool:
    if not exe_path:
        return False
    return "clashmi" in str(exe_path).lower().replace("/", "\\")


# ---------------------------------------------------------------------------
# 默认实现：进程快照 / 进程树强杀 / 窗口关闭 / 注册表代理
# ---------------------------------------------------------------------------

def default_process_snapshot() -> Optional[List[Dict[str, Any]]]:
    """返回 [{name, pid, ppid, exe}]；psutil 不可用时返回 None。"""
    try:
        import psutil  # type: ignore
    except Exception:
        return None
    snapshot: List[Dict[str, Any]] = []
    try:
        for proc in psutil.process_iter(attrs=["pid", "ppid", "name", "exe"]):
            info = proc.info or {}
            if info.get("name"):
                snapshot.append(
                    {
                        "name": str(info["name"]),
                        "pid": info.get("pid"),
                        "ppid": info.get("ppid"),
                        "exe": info.get("exe"),
                    }
                )
    except Exception:
        logger.debug("进程快照失败", exc_info=True)
        return None
    return snapshot


def default_tree_kill(pid: Optional[int], name: Optional[str] = None) -> Tuple[bool, str]:
    """按进程树强杀。Windows：taskkill /F /T；其它平台：psutil 子孙递归。"""
    if pid is None and not name:
        return False, "既无 pid 又无进程名"
    if IS_WINDOWS:
        argv = ["taskkill", "/F", "/T"]
        if pid is not None:
            argv += ["/PID", str(pid)]
        else:
            argv += ["/IM", str(name)]
        try:
            kwargs: Dict[str, Any] = {"creationflags": subprocess.CREATE_NO_WINDOW}
            proc = subprocess.run(
                argv, capture_output=True, timeout=10,
                encoding="utf-8", errors="replace", **kwargs,
            )
        except Exception as exc:
            return False, f"taskkill 执行失败：{exc}"
        output = ((proc.stdout or "") + (proc.stderr or "")).strip()
        if proc.returncode == 0:
            return True, output or "taskkill 成功"
        if proc.returncode == 128 or "no running instance" in output.lower() or "没有运行的任务" in output:
            return True, "进程已不在运行"
        return False, f"taskkill 返回 {proc.returncode}：{output}"
    # 非 Windows：psutil 子孙递归。
    try:
        import psutil  # type: ignore
    except Exception:
        return False, "缺少 psutil，无法按进程树强杀"
    try:
        if pid is not None:
            proc = psutil.Process(pid)
        else:
            matches = [
                p for p in psutil.process_iter(attrs=["name"])
                if (p.info or {}).get("name", "").lower() == str(name).lower()
            ]
            if not matches:
                return True, "进程已不在运行"
            proc = matches[0]
        children = proc.children(recursive=True)
        for child in children:
            try:
                child.kill()
            except Exception:
                pass
        proc.kill()
        try:
            proc.wait(timeout=3)
        except Exception:
            pass
        return True, "psutil 进程树强杀完成"
    except Exception as exc:
        return False, f"psutil 强杀失败：{exc}"


def default_close_main_window(main_pids: List[int]) -> Dict[str, Any]:
    """向主进程窗口发 WM_CLOSE。返回 {ok|skipped, detail}。"""
    if not IS_WINDOWS:
        return {"skipped": True, "detail": "非 Windows 平台，无 win32 窗口可关"}
    if not main_pids:
        return {"skipped": True, "detail": "未发现 ClashMi 主进程，无窗口可关"}
    try:
        import win32gui  # type: ignore
        import win32con  # type: ignore
    except Exception:
        return {"skipped": True, "detail": "win32gui 不可用，跳过窗口关闭（后续仍按进程树强杀）"}
    closed = 0
    try:
        def _enum(hwnd, _extra):
            nonlocal closed
            try:
                _, win_pid = win32gui.GetWindowThreadProcessId(hwnd)
                if win_pid in main_pids and win32gui.IsWindowVisible(hwnd):
                    win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
                    closed += 1
            except Exception:
                pass
            return True

        win32gui.EnumWindows(_enum, None)
    except Exception as exc:
        return {"ok": False, "detail": f"枚举窗口失败：{exc}"}
    if closed:
        return {"ok": True, "detail": f"已向 {closed} 个主窗口发送 WM_CLOSE"}
    return {"skipped": True, "detail": "主进程无可见窗口"}


def default_read_proxy() -> Optional[Dict[str, Any]]:
    """读 HKCU 代理设置；非 Windows 返回 None（表示跳过）。"""
    if not IS_WINDOWS:
        return None
    try:
        import winreg  # type: ignore

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, PROXY_KEY_PATH) as key:
            try:
                enabled, _ = winreg.QueryValueEx(key, "ProxyEnable")
            except Exception:
                enabled = 0
            try:
                server, _ = winreg.QueryValueEx(key, "ProxyServer")
            except Exception:
                server = ""
        return {"enabled": int(enabled), "server": str(server or "")}
    except Exception as exc:
        logger.debug("读取代理注册表失败", exc_info=True)
        return {"enabled": None, "server": None, "error": str(exc)}


def default_write_proxy_disable() -> Tuple[bool, str]:
    """将 ProxyEnable 置 0 并刷新系统网络状态；返回 (ok, detail)。"""
    if not IS_WINDOWS:
        return False, "非 Windows 平台，跳过注册表代理清理"
    try:
        import ctypes
        import winreg  # type: ignore

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, PROXY_KEY_PATH, 0, winreg.KEY_SET_VALUE
        ) as key:
            winreg.SetValueEx(key, "ProxyEnable", 0, winreg.REG_DWORD, 0)
        try:
            internet_set_option = ctypes.windll.wininet.InternetSetOptionW
            internet_set_option(0, 39, 0, 0)
            internet_set_option(0, 37, 0, 0)
        except Exception:
            pass
        return True, "ProxyEnable 已置 0 并刷新系统网络状态"
    except Exception as exc:
        return False, f"写代理注册表失败：{exc}"


# ---------------------------------------------------------------------------
# 辅助：端口与代理指向判断
# ---------------------------------------------------------------------------

def _clashmi_port(client: Any) -> Optional[int]:
    """从 client 的 ``/configs`` 代理字段取本地代理端口；取不到返回 None。

    注意（P0-5 修复）：External Controller 端口（settings.base_url，常见
    9090）不是系统代理端口，绝不能拿来匹配注册表 ``ProxyServer``。这里只
    读取 ``mixed-port`` / ``port`` / ``socks-port`` 等真正承载代理的字段。
    """
    try:
        configs = client.get_configs() if client is not None else {}
        if isinstance(configs, dict):
            for key in ("mixed-port", "port", "socks-port"):
                value = configs.get(key)
                if isinstance(value, int) and value > 0:
                    return value
    except Exception:
        pass
    return None


def _proxy_points_to_clashmi(server: str, port: Optional[int]) -> bool:
    """ProxyServer 是否指向本地 ClashMi 端口。"""
    if not server:
        return False
    text = server.strip().lower()
    host = text.split(":")[0].strip()
    if host not in ("127.0.0.1", "localhost", "::1"):
        return False
    if port is None:
        return True  # 端口未知时，凡本地代理都视为 ClashMi 代理兜底对象
    if ":" in text:
        tail = text.rsplit(":", 1)[1]
        return tail.isdigit() and int(tail) == port
    return False


def _descendants_of(snapshot: List[Dict[str, Any]], roots: List[int]) -> set:
    """快照中属于 roots 子孙的 pid 集合（按 ppid 链展开）。"""
    by_pid = {p.get("pid"): p for p in snapshot if p.get("pid") is not None}
    found: set = set()
    frontier = list(roots)
    while frontier:
        parent = frontier.pop()
        for proc in snapshot:
            if proc.get("ppid") == parent and proc.get("pid") not in found:
                found.add(proc.get("pid"))
                frontier.append(proc.get("pid"))
    return found


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

def close_clashmi_atomic(
    client: Any = None,
    *,
    sleep_fn: Callable[[float], None] = time.sleep,
    snapshot_fn: Optional[Callable[[], Optional[List[Dict[str, Any]]]]] = None,
    tree_kill_fn: Optional[Callable[..., Tuple[bool, str]]] = None,
    window_closer_fn: Optional[Callable[[List[int]], Dict[str, Any]]] = None,
    proxy_reader_fn: Optional[Callable[[], Optional[Dict[str, Any]]]] = None,
    proxy_writer_fn: Optional[Callable[[], Tuple[bool, str]]] = None,
    system_proxy_off_fn: Optional[Callable[[], Tuple[bool, str]]] = None,
) -> Dict[str, Any]:
    """执行 ClashMi 四步原子关闭。单步失败不中断后续步骤，绝不抛异常。

    client 为 None 时尝试新建 ClashMiClient；创建失败则第 ① 步的 API
    部分记失败，其余步骤照常执行。
    """
    snapshot_fn = snapshot_fn or default_process_snapshot
    tree_kill_fn = tree_kill_fn or default_tree_kill
    window_closer_fn = window_closer_fn or default_close_main_window
    proxy_reader_fn = proxy_reader_fn or default_read_proxy
    proxy_writer_fn = proxy_writer_fn or default_write_proxy_disable
    if system_proxy_off_fn is None:
        # 第 ① 步的“关闭系统代理”与第 ④ 步用同一注册表写入路径。
        system_proxy_off_fn = proxy_writer_fn

    steps: List[Dict[str, Any]] = []
    try:
        # ---- ① API 优雅卸载 ----
        api_detail: Dict[str, Any] = {}
        if client is None:
            try:
                from clashmi import ClashMiClient  # type: ignore

                client = ClashMiClient()
            except Exception as exc:
                client = None
                api_detail["client_error"] = f"无法创建 ClashMiClient：{exc}"
        if client is not None:
            for method_name in ("disable_tun", "close_connections"):
                try:
                    result = getattr(client, method_name)()
                    api_detail[method_name] = {"ok": True, "result": result}
                except Exception as exc:
                    api_detail[method_name] = {"ok": False, "error": str(exc)}
        else:
            for method_name in ("disable_tun", "close_connections"):
                api_detail[method_name] = {"ok": False, "error": "无可用 ClashMiClient"}
        if IS_WINDOWS:
            try:
                ok, detail = system_proxy_off_fn()
                api_detail["system_proxy"] = {"ok": ok, "detail": detail}
            except Exception as exc:
                api_detail["system_proxy"] = {"ok": False, "error": str(exc)}
        else:
            api_detail["system_proxy"] = {"skipped": True, "detail": "非 Windows 平台，跳过系统代理关闭"}
        api_ok = all(
            v.get("ok") for v in api_detail.values() if isinstance(v, dict) and "ok" in v
        )
        steps.append({"step": "api_graceful", "ok": api_ok, "detail": api_detail})

        # ---- ② 关闭主窗口 ----
        before = snapshot_fn()
        main_pids: List[int] = []
        if before:
            main_pids = [
                p["pid"] for p in before
                if str(p.get("name", "")).lower() in {n.lower() for n in MAIN_PROCESS_NAMES}
                and p.get("pid") is not None
            ]
        try:
            window_result = window_closer_fn(main_pids)
        except Exception as exc:
            window_result = {"ok": False, "detail": f"窗口关闭回调异常：{exc}"}
        steps.append({"step": "close_main_window", "ok": window_result.get("ok", False) or bool(window_result.get("skipped")), "detail": window_result, "main_pids": main_pids})

        # ---- ③ 等待 1.5 秒后检查并按进程树强杀 ----
        try:
            sleep_fn(WAIT_AFTER_WINDOW_CLOSE_SECONDS)
            slept = WAIT_AFTER_WINDOW_CLOSE_SECONDS
        except Exception as exc:
            slept = 0.0
            logger.debug("等待回调异常", exc_info=True)
        after = snapshot_fn()
        kill_detail: Dict[str, Any] = {"slept_seconds": slept}
        killed: List[Dict[str, Any]] = []
        skipped_kernel: List[Dict[str, Any]] = []
        if after is None:
            kill_detail["note"] = "进程快照不可用，无法核查残留进程"
        else:
            descendants = _descendants_of(after, main_pids) if main_pids else set()
            for proc in after:
                name_lower = str(proc.get("name", "")).lower()
                pid = proc.get("pid")
                if name_lower in {n.lower() for n in MAIN_PROCESS_NAMES}:
                    ok, detail = tree_kill_fn(pid, proc.get("name"))
                    killed.append({"name": proc.get("name"), "pid": pid, "ok": ok, "detail": detail})
                elif name_lower in {n.lower() for n in KERNEL_PROCESS_NAMES}:
                    if pid in descendants or _path_looks_like_clashmi(proc.get("exe")):
                        ok, detail = tree_kill_fn(pid, proc.get("name"))
                        killed.append({"name": proc.get("name"), "pid": pid, "ok": ok, "detail": detail})
                    else:
                        skipped_kernel.append(
                            {"name": proc.get("name"), "pid": pid, "exe": proc.get("exe"),
                             "reason": "路径不像 ClashMi 且非主进程子孙，拒绝无差别强杀"}
                        )
        kill_detail["killed"] = killed
        kill_detail["skipped_kernel"] = skipped_kernel
        kill_ok = all(k.get("ok") for k in killed)
        steps.append({"step": "kill_process_tree", "ok": kill_ok, "detail": kill_detail})

        # ---- ④ 注册表代理兜底 ----
        proxy_clean = True  # 非 Windows 下跳过即视为干净（不计失败）
        proxy_detail: Dict[str, Any] = {}
        if not IS_WINDOWS:
            proxy_detail = {"skipped": True, "detail": "非 Windows 平台，跳过注册表代理兜底"}
        else:
            try:
                current = proxy_reader_fn()
            except Exception as exc:
                current = {"enabled": None, "server": None, "error": str(exc)}
            proxy_detail["before"] = current
            if current is None:
                proxy_detail["skipped"] = True
                proxy_detail["detail"] = "代理读取返回空，跳过"
            elif current.get("error"):
                proxy_clean = False
                proxy_detail["error"] = current["error"]
            else:
                port = _clashmi_port(client)
                enabled = current.get("enabled")
                server = str(current.get("server") or "")
                if enabled == 1 and _proxy_points_to_clashmi(server, port):
                    try:
                        ok, detail = proxy_writer_fn()
                    except Exception as exc:
                        ok, detail = False, f"代理写入回调异常：{exc}"
                    proxy_detail["cleanup"] = {"ok": ok, "detail": detail}
                    if ok:
                        try:
                            recheck = proxy_reader_fn()
                        except Exception:
                            recheck = None
                        proxy_detail["after"] = recheck
                        proxy_clean = bool(recheck and recheck.get("enabled") == 0)
                    else:
                        proxy_clean = False
                else:
                    proxy_detail["skipped"] = True
                    proxy_detail["detail"] = (
                        "代理未指向本地 ClashMi 端口或已关闭，无需兜底"
                        if enabled != 1 else "代理已开启但不指向本地 ClashMi，不动他人代理"
                    )
                    proxy_clean = True
        steps.append({"step": "proxy_cleanup", "ok": proxy_clean, "detail": proxy_detail})

        # ---- 最终判定 ----
        final_snapshot = snapshot_fn()
        if final_snapshot is not None:
            remaining_main = [
                p for p in final_snapshot
                if str(p.get("name", "")).lower() in {n.lower() for n in MAIN_PROCESS_NAMES}
            ]
            main_gone = not remaining_main
            _desc = _descendants_of(final_snapshot, main_pids) if main_pids else set()
            remaining_kernel = [
                p for p in final_snapshot
                if str(p.get("name", "")).lower() in {n.lower() for n in KERNEL_PROCESS_NAMES}
                and (p.get("pid") in _desc or _path_looks_like_clashmi(p.get("exe")))
            ]
            kernel_gone = not remaining_kernel
        else:
            # 快照不可用 = 无法核验主进程/内核是否真正消失（P0-5 修复）：
            # 此时绝不能报成功，必须判失败并留待上层通知/重试。
            main_gone = False
            kernel_gone = False
            steps.append({
                "step": "final_verify",
                "ok": False,
                "detail": "进程快照不可用，无法核验主进程与内核进程已消失",
            })
        ok = bool(main_gone and kernel_gone and proxy_clean)
        return {
            "ok": ok,
            "steps": steps,
            "details": {
                "main_gone": main_gone,
                "kernel_gone": kernel_gone,
                "proxy_clean": proxy_clean,
            },
        }
    except Exception as exc:  # 兜底：绝不抛异常
        logger.warning("ClashMi 原子关闭异常", exc_info=True)
        steps.append({"step": "fatal", "ok": False, "detail": str(exc)})
        return {"ok": False, "steps": steps, "details": {"error": str(exc)}}


__all__ = [
    "IS_WINDOWS",
    "MAIN_PROCESS_NAMES",
    "KERNEL_PROCESS_NAMES",
    "WAIT_AFTER_WINDOW_CLOSE_SECONDS",
    "close_clashmi_atomic",
    "default_process_snapshot",
    "default_tree_kill",
    "default_close_main_window",
    "default_read_proxy",
    "default_write_proxy_disable",
]
