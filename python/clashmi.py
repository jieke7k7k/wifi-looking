from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

import requests


SETTINGS_FILE_NAME = "service_core_setting.json"
APPDATA_SETTINGS_PATH = Path(
    os.environ.get("APPDATA", ""),
    "clashmi",
    "clashmi",
    SETTINGS_FILE_NAME,
)
REGISTRY_DID_PATH = r"Software\ClashMi"
REGISTRY_DID_VALUE = "did"


@dataclass(frozen=True)
class ClashMiSettings:
    base_url: str = "http://127.0.0.1:9090"
    secret: str = ""
    source: str = "default"


class ClashMiError(RuntimeError):
    pass


class ClashMiClient:
    def __init__(self, settings: ClashMiSettings | None = None) -> None:
        self.settings = settings or self.discover_settings()

    @staticmethod
    def discover_settings() -> ClashMiSettings:
        """Read ClashMi settings from disk, then from registry, then fallback."""
        settings = _settings_from_file()
        if settings:
            return settings

        settings = _settings_from_registry()
        if settings:
            return settings

        return ClashMiSettings(source="fallback")

    def _headers(self) -> Dict[str, str]:
        if not self.settings.secret:
            return {}
        return {"Authorization": f"Bearer {self.settings.secret}"}

    def _url(self, path: str) -> str:
        return f"{self.settings.base_url.rstrip('/')}{path}"

    def get_connections(self, timeout_seconds: float = 3.0) -> Dict[str, Any]:
        try:
            response = requests.get(
                self._url("/connections"),
                headers=self._headers(),
                timeout=timeout_seconds,
            )
        except requests.RequestException as exc:
            raise ClashMiError(str(exc)) from exc

        if response.status_code == 401:
            raise ClashMiError("ClashMi secret 无效或已变更")
        if not response.ok:
            raise ClashMiError(f"ClashMi API 返回 {response.status_code}: {response.text[:200]}")

        try:
            return response.json()
        except ValueError as exc:
            raise ClashMiError("ClashMi API 返回了无效 JSON") from exc

    def connection_count(self) -> int:
        payload = self.get_connections()
        connections = payload.get("connections")
        return len(connections) if isinstance(connections, list) else 0

    def close_connections(self, timeout_seconds: float = 3.0) -> int:
        before = self.connection_count()
        try:
            response = requests.delete(
                self._url("/connections"),
                headers=self._headers(),
                timeout=timeout_seconds,
            )
        except requests.RequestException as exc:
            raise ClashMiError(str(exc)) from exc

        if response.status_code == 401:
            raise ClashMiError("ClashMi secret 无效或已变更")
        if not response.ok:
            raise ClashMiError(f"关闭 ClashMi 连接失败：HTTP {response.status_code}")

        return before

    def disable_tun(self, timeout_seconds: float = 3.0) -> None:
        try:
            response = requests.patch(
                self._url("/configs"),
                headers=self._headers(),
                json={"tun": {"enable": False}},
                timeout=timeout_seconds,
            )
        except requests.RequestException as exc:
            raise ClashMiError(str(exc)) from exc

        if response.status_code == 401:
            raise ClashMiError("ClashMi secret 无效或已变更")
        if not response.ok:
            raise ClashMiError(f"关闭 ClashMi TUN 失败：HTTP {response.status_code}")


def _settings_from_file() -> Optional[ClashMiSettings]:
    path = APPDATA_SETTINGS_PATH
    if not path.is_absolute() or not path.exists():
        return None

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None

    if not isinstance(payload, dict):
        return None

    external_controller = str(payload.get("external-controller", "")).strip()
    secret = str(payload.get("secret", "")).strip()
    if not external_controller:
        return None

    base_url = _normalize_base_url(external_controller)
    return ClashMiSettings(base_url=base_url, secret=secret, source=str(path))


def _settings_from_registry() -> Optional[ClashMiSettings]:
    if sys.platform != "win32":
        return None

    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REGISTRY_DID_PATH) as key:
            did = str(winreg.QueryValueEx(key, REGISTRY_DID_VALUE)[0]).strip()
    except Exception:
        return None

    if len(did) < 24:
        return None

    # Derived from ClashMi's getSecretFromDid(): did[8:24].
    secret = did[8:24]
    return ClashMiSettings(
        base_url="http://127.0.0.1:9090",
        secret=secret,
        source="registry",
    )


def _normalize_base_url(value: str) -> str:
    if value.startswith(("http://", "https://")):
        return value.rstrip("/")
    return f"http://{value}".rstrip("/")
