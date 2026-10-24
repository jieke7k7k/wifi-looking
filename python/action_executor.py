from __future__ import annotations

import ctypes
import json
import logging
import subprocess
import sys
import time
import webbrowser
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

from clashmi import ClashMiClient, ClashMiError


logger = logging.getLogger(__name__)


# Candidate image names for the clashmi.kill_process action.
#
# CONSERVATIVE DEFAULT (review bugs 1/3): only "ClashMi.exe" — the GUI
# process whose name is specific to ClashMi — is a primary target.
# Generic names such as "mihomo.exe" and "clash.exe" are shared with
# other clients (Clash Verge, FlClash, hand-rolled mihomo), so killing
# them by image name alone could terminate unrelated software; they
# are demoted to secondary targets and are only killed when their
# executable path looks like a ClashMi install (see
# _path_looks_like_clashmi). "ClashMiCore.exe" has no verified source
# and was removed entirely.
#
# *** The exact on-device image names still need real-machine
# verification (tasklist / install dir) — see the v0.3.0 review-fix
# change list. *** Users can extend/override the lists without a new
# release via config.json:
#   "clashmi": {"primary_process_names": [...],
#               "secondary_process_names": [...]}
CLASHMI_PRIMARY_PROCESS_NAMES: tuple[str, ...] = ("ClashMi.exe",)
CLASHMI_SECONDARY_PROCESS_NAMES: tuple[str, ...] = ("mihomo.exe",)
# Backwards-compatible union of both lists.
CLASHMI_PROCESS_NAMES: tuple[str, ...] = (
    CLASHMI_PRIMARY_PROCESS_NAMES + CLASHMI_SECONDARY_PROCESS_NAMES
)


def _load_configured_process_names() -> "tuple[List[str], List[str]]":
    """(primary, secondary) image names, honoring config.json overrides."""
    primary = list(CLASHMI_PRIMARY_PROCESS_NAMES)
    secondary = list(CLASHMI_SECONDARY_PROCESS_NAMES)
    try:
        config_path = Path(__file__).resolve().parent / "config.json"
        payload = json.loads(config_path.read_text(encoding="utf-8"))
        section = payload.get("clashmi", {}) if isinstance(payload, dict) else {}
        if isinstance(section, dict):
            for key, target in (
                ("primary_process_names", primary),
                ("secondary_process_names", secondary),
            ):
                values = section.get(key)
                if isinstance(values, list):
                    for value in values:
                        name = str(value).strip()
                        if name and name.lower() not in [n.lower() for n in target]:
                            target.append(name)
    except Exception:
        logger.debug("Could not read clashmi process-name overrides", exc_info=True)
    return primary, secondary


def _path_looks_like_clashmi(exe_path: Optional[str]) -> bool:
    """Heuristic ownership check for generic-named core processes."""
    if not exe_path:
        return False
    lowered = exe_path.lower().replace("/", "\\")
    return "clashmi" in lowered


class ActionExecutor:
    """Executes configured rule actions."""

    def execute_action(
        self,
        action: Dict[str, Any],
        event: Dict[str, object],
        dry_run: bool = False,
    ) -> Dict[str, Any]:
        action_type = action.get("type")

        if action_type == "clashmi.close_connections":
            return self.method_clashmi_close_connections(dry_run=dry_run)
        if action_type == "clashmi.disable_tun":
            return self.method_clashmi_disable_tun(dry_run=dry_run)
        if action_type == "clashmi.test_connection":
            return self.method_clashmi_test_connection(dry_run=dry_run)
        if action_type == "clashmi.kill_process":
            return self.method_clashmi_kill_process(dry_run=dry_run)
        if action_type == "clash_api":
            return self.method_clash_api(action, dry_run=dry_run)
        if action_type == "system_proxy":
            return self.method_system_proxy(dry_run=dry_run)
        if action_type == "kill_process":
            return self.method_kill_process(action, dry_run=dry_run)
        if action_type == "run_command":
            return self.method_run_command(action, dry_run=dry_run)
        if action_type == "start_process":
            return self.method_start_process(action, dry_run=dry_run)
        if action_type == "http_request":
            return self.method_http_request(action, dry_run=dry_run)
        if action_type == "open_url":
            return self.method_open_url(action, dry_run=dry_run)
        if action_type == "delay":
            return self.method_delay(action, dry_run=dry_run)
        if action_type == "log":
            return {
                "ok": True,
                "action": "log",
                "message": action.get("message", "Rule matched"),
            }

        return {"ok": False, "error": f"Unsupported action type: {action_type}"}

    def method_clashmi_close_connections(self, dry_run: bool = False) -> Dict[str, Any]:
        if dry_run:
            return {"ok": True, "action": "clashmi.close_connections", "dry_run": True}
        try:
            client = ClashMiClient()
            closed = client.close_connections()
            return {
                "ok": True,
                "action": "clashmi.close_connections",
                "closed_connections": closed,
                "source": client.settings.source,
            }
        except ClashMiError as exc:
            logger.warning("ClashMi close connections failed: %s", exc)
            return {"ok": False, "action": "clashmi.close_connections", "error": str(exc)}

    def method_clashmi_disable_tun(self, dry_run: bool = False) -> Dict[str, Any]:
        if dry_run:
            return {"ok": True, "action": "clashmi.disable_tun", "dry_run": True}
        try:
            client = ClashMiClient()
            client.disable_tun()
            return {
                "ok": True,
                "action": "clashmi.disable_tun",
                "source": client.settings.source,
            }
        except ClashMiError as exc:
            logger.warning("ClashMi disable TUN failed: %s", exc)
            return {"ok": False, "action": "clashmi.disable_tun", "error": str(exc)}

    def method_clashmi_test_connection(self, dry_run: bool = False) -> Dict[str, Any]:
        if dry_run:
            return {"ok": True, "action": "clashmi.test_connection", "dry_run": True}
        try:
            client = ClashMiClient()
            count = client.connection_count()
            return {
                "ok": True,
                "action": "clashmi.test_connection",
                "connection_count": count,
                "base_url": client.settings.base_url,
                "source": client.settings.source,
            }
        except ClashMiError as exc:
            logger.warning("ClashMi test failed: %s", exc)
            return {"ok": False, "action": "clashmi.test_connection", "error": str(exc)}

    def method_clashmi_kill_process(self, dry_run: bool = False) -> Dict[str, Any]:
        """Turn ClashMi off by terminating its processes (taskkill /F /IM).

        Conservative ownership model (review bugs 1/3/4):
        - Primary target: ClashMi.exe (name is ClashMi-specific).
        - Secondary targets (generic names like mihomo.exe) are only
          killed when a process snapshot shows their executable path
          belongs to a ClashMi install; otherwise they are skipped and
          logged, never killed blind.
        - Each name is killed individually without /T (never a whole
          process tree). Exit 128 / not-in-snapshot counts as already
          off. Failures are reported, never raised.
        - Success is decided by the primary target plus a post-kill
          verification snapshot: if a primary process is verifiably
          still running, ok is False even if taskkill claimed success.
          Failures of secondary targets are reported but do not flip
          ok (and do not block cooldown) when the primary target is
          off — see review bug 4.
        """
        primary_names, secondary_names = _load_configured_process_names()
        if dry_run:
            return {
                "ok": True,
                "action": "clashmi.kill_process",
                "dry_run": True,
                "targets": primary_names + secondary_names,
            }

        killed: list[str] = []
        already_off: list[str] = []
        skipped: list[Dict[str, Any]] = []
        failed: list[Dict[str, Any]] = []

        snapshot = self._process_snapshot()
        snapshot_by_name: Dict[str, List[Dict[str, Any]]] = {}
        for proc in snapshot or []:
            snapshot_by_name.setdefault(proc["name"].lower(), []).append(proc)
        if snapshot is not None:
            for proc in snapshot:
                if proc["name"].lower() in {
                    n.lower() for n in primary_names + secondary_names
                }:
                    logger.info(
                        "clashmi.kill_process candidate: %s pid=%s exe=%s",
                        proc["name"], proc.get("pid"), proc.get("exe"),
                    )

        def kill_one(name: str) -> None:
            status, detail = self._taskkill_image(name)
            if status == "killed":
                killed.append(name)
            elif status == "absent":
                already_off.append(name)
            else:
                failed.append({"process": name, "error": detail})
                logger.warning("taskkill %s failed: %s", name, detail)
                if "access" in detail.lower() or "拒绝访问" in detail or "denied" in detail.lower():
                    logger.warning(
                        "%s could not be terminated (access denied). If "
                        "ClashMi runs as administrator, WiFi-Looking must "
                        "also run as administrator to close it.",
                        name,
                    )

        for name in primary_names:
            kill_one(name)

        for name in secondary_names:
            procs = snapshot_by_name.get(name.lower())
            if snapshot is not None and not procs:
                already_off.append(name)
                continue
            if procs and not any(
                _path_looks_like_clashmi(proc.get("exe")) for proc in procs
            ):
                skipped.append(
                    {"process": name, "reason": "path-not-clashmi", "paths": [pr.get("exe") for pr in procs]}
                )
                logger.info(
                    "Skipping %s: running instance(s) do not look like ClashMi (paths: %s)",
                    name, [pr.get("exe") for pr in procs],
                )
                continue
            kill_one(name)

        # Post-kill verification (review bug 3): re-snapshot and make
        # sure no primary process survived. API activity is logged as
        # supporting evidence only — a just-killed GUI can leave the
        # API briefly answering, so the process snapshot is decisive.
        verified_off: Optional[bool] = None
        after = self._process_snapshot()
        if after is not None:
            still = [
                proc["name"]
                for proc in after
                if proc["name"].lower() in {n.lower() for n in primary_names}
            ]
            verified_off = not still
            if still:
                logger.warning("ClashMi still running after kill: %s", still)
        try:
            if ClashMiClient().is_active():
                logger.info("ClashMi API still reports active right after kill (may be shutting down)")
        except Exception:
            logger.debug("Post-kill ClashMi API probe failed", exc_info=True)

        primary_failed = [f for f in failed if f["process"] in primary_names]
        ok = not primary_failed and verified_off is not False

        result: Dict[str, Any] = {
            "ok": ok,
            "action": "clashmi.kill_process",
            "killed": killed,
            "already_off": already_off,
            "verified_off": verified_off,
        }
        if skipped:
            result["skipped"] = skipped
        if failed:
            result["failed"] = failed
        return result

    @staticmethod
    def _process_snapshot() -> Optional[List[Dict[str, Any]]]:
        """Snapshot of running processes via psutil; None if unavailable.

        psutil (already a dependency) is used instead of tasklist so
        the check carries executable paths for ownership decisions.
        """
        try:
            import psutil
        except Exception:
            return None
        snapshot: List[Dict[str, Any]] = []
        try:
            for proc in psutil.process_iter(attrs=["name", "exe", "pid"]):
                info = proc.info or {}
                if info.get("name"):
                    snapshot.append(
                        {"name": str(info["name"]), "exe": info.get("exe"), "pid": info.get("pid")}
                    )
        except Exception:
            logger.debug("Process snapshot failed", exc_info=True)
            return None
        return snapshot

    def _taskkill_image(self, process_name: str) -> tuple[str, str]:
        """taskkill one image name; returns (status, detail).

        status is "killed" (exit 0), "absent" (exit 128 or the output
        says no such process — the exit code alone is not a documented
        contract, review bug 23) or "failed" (anything else, including
        taskkill hanging: subprocess timeout of 3s, review bug 5).
        Output is decoded explicitly with errors="replace" so a
        localized taskkill can never raise inside communicate().
        """
        try:
            kwargs: Dict[str, object] = {}
            if sys.platform == "win32":
                kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

            process = subprocess.run(
                ["taskkill", "/F", "/IM", process_name],
                capture_output=True,
                timeout=3,
                encoding="utf-8",
                errors="replace",
                **kwargs,
            )
        except subprocess.TimeoutExpired:
            return "failed", "taskkill timed out after 3s"
        except Exception as exc:
            return "failed", str(exc)

        output = ((process.stdout or "") + (process.stderr or "")).strip()
        lowered = output.lower()
        if process.returncode == 0:
            return "killed", output
        if (
            process.returncode == 128
            or "no running instance" in lowered
            or "not found" in lowered
            or "没有运行的任务" in output
            or "找不到" in output
        ):
            return "absent", output
        return "failed", f"taskkill exited {process.returncode}: {output}"

    def method_clash_api(
        self,
        action: Dict[str, Any],
        dry_run: bool = False,
    ) -> Dict[str, Any]:
        base_url = str(action.get("base_url", "")).rstrip("/")
        subaction = action.get("subaction")
        secret = action.get("secret", "")
        timeout_seconds = float(action.get("timeout_seconds", 3.0))

        headers: Dict[str, str] = {}
        if secret:
            headers["Authorization"] = f"Bearer {secret}"

        if subaction == "close_connections":
            url = f"{base_url}/connections"
            method = "DELETE"
            payload: Dict[str, Any] | None = None
        elif subaction == "set_mode":
            url = f"{base_url}/configs"
            method = "PATCH"
            payload = {"mode": action.get("mode", "Direct")}
        else:
            return {"ok": False, "error": f"Unsupported clash subaction: {subaction}"}

        if dry_run:
            return {
                "ok": True,
                "action": "clash_api",
                "subaction": subaction,
                "dry_run": True,
                "method": method,
                "url": url,
                "payload": payload,
            }

        try:
            response = requests.request(
                method=method,
                url=url,
                headers=headers,
                json=payload,
                timeout=timeout_seconds,
            )
            return {
                "ok": response.ok,
                "action": "clash_api",
                "subaction": subaction,
                "status": response.status_code,
                "body": response.text[:1000],
            }
        except requests.RequestException as exc:
            logger.warning("Clash API request failed", exc_info=exc)
            return {"ok": False, "action": "clash_api", "error": str(exc)}

    def method_system_proxy(self, dry_run: bool = False) -> Dict[str, Any]:
        if dry_run:
            return {"ok": True, "action": "system_proxy", "dry_run": True}

        if sys.platform != "win32":
            return {"ok": False, "action": "system_proxy", "error": "Windows only"}

        try:
            import winreg

            key_path = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                key_path,
                0,
                winreg.KEY_SET_VALUE,
            ) as key:
                winreg.SetValueEx(key, "ProxyEnable", 0, winreg.REG_DWORD, 0)

            self._refresh_windows_settings()
            return {"ok": True, "action": "system_proxy"}
        except Exception as exc:
            logger.warning("Failed to disable system proxy", exc_info=exc)
            return {"ok": False, "action": "system_proxy", "error": str(exc)}

    def _refresh_windows_settings(self) -> None:
        if sys.platform != "win32":
            return

        internet_set_option = ctypes.windll.wininet.InternetSetOptionW
        internet_set_option(0, 39, 0, 0)
        internet_set_option(0, 37, 0, 0)

    def method_kill_process(
        self,
        action: Dict[str, Any],
        dry_run: bool = False,
    ) -> Dict[str, Any]:
        process_name = str(action.get("process_name", "")).strip()
        if not process_name:
            return {"ok": False, "action": "kill_process", "error": "process_name is required"}

        if dry_run:
            return {
                "ok": True,
                "action": "kill_process",
                "process_name": process_name,
                "dry_run": True,
            }

        try:
            import psutil

            killed = []
            for process in psutil.process_iter(attrs=["name"]):
                name = process.info.get("name", "")
                if name and name.lower() == process_name.lower():
                    process.kill()
                    killed.append(process.pid)
            return {
                "ok": True,
                "action": "kill_process",
                "process_name": process_name,
                "killed_pids": killed,
            }
        except ImportError:
            return self._kill_with_taskkill(process_name)
        except Exception as exc:
            logger.warning("Failed to kill process", exc_info=exc)
            return {
                "ok": False,
                "action": "kill_process",
                "process_name": process_name,
                "error": str(exc),
            }

    def _kill_with_taskkill(self, process_name: str) -> Dict[str, Any]:
        try:
            kwargs: Dict[str, object] = {}
            if sys.platform == "win32":
                kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

            process = subprocess.run(
                ["taskkill", "/F", "/IM", process_name],
                capture_output=True,
                text=True,
                timeout=3,
                **kwargs,
            )
            return {
                "ok": process.returncode == 0,
                "action": "kill_process",
                "process_name": process_name,
                "returncode": process.returncode,
                "stdout": process.stdout,
                "stderr": process.stderr,
            }
        except Exception as exc:
            logger.warning("taskkill failed", exc_info=exc)
            return {
                "ok": False,
                "action": "kill_process",
                "process_name": process_name,
                "error": str(exc),
            }

    def method_run_command(
        self,
        action: Dict[str, Any],
        dry_run: bool = False,
    ) -> Dict[str, Any]:
        command = action.get("command")
        args = action.get("args", [])
        timeout_seconds = float(action.get("timeout_seconds", 10.0))

        if not isinstance(command, str) or not command.strip():
            return {"ok": False, "action": "run_command", "error": "command is required"}
        if not isinstance(args, list):
            return {"ok": False, "action": "run_command", "error": "args must be a list"}

        if dry_run:
            return {
                "ok": True,
                "action": "run_command",
                "command": command,
                "args": args,
                "dry_run": True,
            }

        try:
            kwargs: Dict[str, object] = {}
            if sys.platform == "win32":
                kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

            process = subprocess.run(
                [command, *args],
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                **kwargs,
            )
            return {
                "ok": process.returncode == 0,
                "action": "run_command",
                "returncode": process.returncode,
                "stdout": process.stdout[:1000],
                "stderr": process.stderr[:1000],
            }
        except Exception as exc:
            logger.warning("run_command failed", exc_info=exc)
            return {"ok": False, "action": "run_command", "error": str(exc)}

    def method_start_process(
        self,
        action: Dict[str, Any],
        dry_run: bool = False,
    ) -> Dict[str, Any]:
        command = action.get("command")
        args = action.get("args", [])

        if not isinstance(command, str) or not command.strip():
            return {"ok": False, "action": "start_process", "error": "command is required"}
        if not isinstance(args, list):
            return {"ok": False, "action": "start_process", "error": "args must be a list"}

        if dry_run:
            return {
                "ok": True,
                "action": "start_process",
                "command": command,
                "args": args,
                "dry_run": True,
            }

        try:
            kwargs: Dict[str, object] = {}
            if sys.platform == "win32":
                kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

            process = subprocess.Popen([command, *args], **kwargs)
            return {
                "ok": True,
                "action": "start_process",
                "pid": process.pid,
            }
        except Exception as exc:
            logger.warning("start_process failed", exc_info=exc)
            return {"ok": False, "action": "start_process", "error": str(exc)}

    def method_http_request(
        self,
        action: Dict[str, Any],
        dry_run: bool = False,
    ) -> Dict[str, Any]:
        url = action.get("url")
        method = str(action.get("method", "GET")).upper()
        headers = action.get("headers", {})
        body = action.get("body")
        json_body = action.get("json")
        timeout_seconds = float(action.get("timeout_seconds", 5.0))

        if not isinstance(url, str) or not url.strip():
            return {"ok": False, "action": "http_request", "error": "url is required"}
        if not isinstance(headers, dict):
            return {"ok": False, "action": "http_request", "error": "headers must be an object"}

        if dry_run:
            return {
                "ok": True,
                "action": "http_request",
                "method": method,
                "url": url,
                "dry_run": True,
            }

        try:
            response = requests.request(
                method=method,
                url=url,
                headers=headers,
                data=body,
                json=json_body,
                timeout=timeout_seconds,
            )
            return {
                "ok": response.ok,
                "action": "http_request",
                "status": response.status_code,
                "body": response.text[:1000],
            }
        except requests.RequestException as exc:
            logger.warning("http_request failed", exc_info=exc)
            return {"ok": False, "action": "http_request", "error": str(exc)}

    def method_open_url(
        self,
        action: Dict[str, Any],
        dry_run: bool = False,
    ) -> Dict[str, Any]:
        url = action.get("url")
        if not isinstance(url, str) or not url.strip():
            return {"ok": False, "action": "open_url", "error": "url is required"}

        if dry_run:
            return {
                "ok": True,
                "action": "open_url",
                "url": url,
                "dry_run": True,
            }

        try:
            webbrowser.open(url)
            return {"ok": True, "action": "open_url", "url": url}
        except Exception as exc:
            logger.warning("open_url failed", exc_info=exc)
            return {"ok": False, "action": "open_url", "error": str(exc)}

    def method_delay(
        self,
        action: Dict[str, Any],
        dry_run: bool = False,
    ) -> Dict[str, Any]:
        seconds = float(action.get("seconds", 1.0))
        if seconds <= 0:
            return {"ok": False, "action": "delay", "error": "seconds must be positive"}

        if dry_run:
            return {
                "ok": True,
                "action": "delay",
                "seconds": seconds,
                "dry_run": True,
            }

        time.sleep(seconds)
        return {"ok": True, "action": "delay", "seconds": seconds}
