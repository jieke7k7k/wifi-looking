from __future__ import annotations

import ctypes
import logging
import subprocess
import sys
import time
import webbrowser
from typing import Any, Dict

import requests

from clashmi import ClashMiClient, ClashMiError


logger = logging.getLogger(__name__)


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
