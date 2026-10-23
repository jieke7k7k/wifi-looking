from __future__ import annotations

import json
import os
import sys
import threading
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


# Discovery touches the disk and the registry, so its result is cached
# at module level and shared by every client. A 401 response invalidates
# the cache and forces one rediscovery + retry.
_settings_cache: Optional[ClashMiSettings] = None
_settings_cache_lock = threading.Lock()


def invalidate_settings_cache() -> None:
    global _settings_cache
    with _settings_cache_lock:
        _settings_cache = None


class ClashMiClient:
    def __init__(self, settings: ClashMiSettings | None = None) -> None:
        self.settings = settings or self.discover_settings()

    @staticmethod
    def discover_settings(use_cache: bool = True) -> ClashMiSettings:
        """Read ClashMi settings from disk, then registry, then fallback."""
        global _settings_cache
        if use_cache:
            with _settings_cache_lock:
                if _settings_cache is not None:
                    return _settings_cache

        settings = _settings_from_file()
        if settings is None:
            settings = _settings_from_registry()
        if settings is None:
            settings = ClashMiSettings(source="fallback")

        with _settings_cache_lock:
            _settings_cache = settings
        return settings

    def _rediscover(self) -> None:
        invalidate_settings_cache()
        self.settings = self.discover_settings(use_cache=False)

    def _headers(self) -> Dict[str, str]:
        if not self.settings.secret:
            return {}
        return {"Authorization": f"Bearer {self.settings.secret}"}

    def _url(self, path: str) -> str:
        return f"{self.settings.base_url.rstrip('/')}{path}"

    def _request(
        self,
        method: str,
        path: str,
        timeout_seconds: float = 3.0,
        **kwargs: Any,
    ) -> requests.Response:
        """Perform one API request; on 401 rediscover settings and retry once."""
        for attempt in (0, 1):
            try:
                response = requests.request(
                    method,
                    self._url(path),
                    headers=self._headers(),
                    timeout=timeout_seconds,
                    **kwargs,
                )
            except requests.RequestException as exc:
                raise ClashMiError(str(exc)) from exc

            if response.status_code == 401:
                if attempt == 0:
                    # The secret may have rotated since discovery (e.g.
                    # ClashMi restarted). Drop the cached settings and
                    # try once more with freshly discovered ones.
                    self._rediscover()
                    continue
                raise ClashMiError("ClashMi secret 无效或已变更")

            return response

        raise ClashMiError("ClashMi 请求失败")  # pragma: no cover

    def get_connections(self, timeout_seconds: float = 3.0) -> Dict[str, Any]:
        response = self._request("GET", "/connections", timeout_seconds)

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
        # The DELETE is the only request that matters. The "before"
        # count is informational: if the GET fails for any reason, the
        # DELETE must still be sent (returning -1 for "count unknown").
        try:
            before = self.connection_count()
        except ClashMiError:
            before = -1

        response = self._request("DELETE", "/connections", timeout_seconds)

        if not response.ok:
            raise ClashMiError(f"关闭 ClashMi 连接失败：HTTP {response.status_code}")

        return before

    def disable_tun(self, timeout_seconds: float = 3.0) -> None:
        response = self._request(
            "PATCH",
            "/configs",
            timeout_seconds,
            json={"tun": {"enable": False}},
        )

        if not response.ok:
            raise ClashMiError(f"关闭 ClashMi TUN 失败：HTTP {response.status_code}")


def _clean_str(value: Any) -> str:
    """Normalize a settings value to a stripped string.

    JSON ``null`` must become "" (missing), never the string "None";
    registry REG_BINARY values arrive as bytes and are decoded.
    """
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="ignore").strip()
    return str(value).strip()


def _settings_from_file(path: Optional[Path] = None) -> Optional[ClashMiSettings]:
    path = APPDATA_SETTINGS_PATH if path is None else path
    if not path.is_absolute() or not path.exists():
        return None

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None

    if not isinstance(payload, dict):
        return None

    external_controller = _clean_str(payload.get("external-controller"))
    secret = _clean_str(payload.get("secret"))
    if not external_controller:
        return None

    base_url = _normalize_base_url(external_controller)
    return ClashMiSettings(base_url=base_url, secret=secret, source=str(path))


def _read_registry_did() -> Any:
    """Read the raw ``did`` value from the registry (None if unavailable)."""
    if sys.platform != "win32":
        return None

    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REGISTRY_DID_PATH) as key:
            return winreg.QueryValueEx(key, REGISTRY_DID_VALUE)[0]
    except Exception:
        return None


def _derive_secret(did: Any) -> Optional[str]:
    """Derive the controller secret from a ClashMi device id."""
    did_text = _clean_str(did)
    if len(did_text) < 24:
        return None

    # Derived from ClashMi's getSecretFromDid(): did[8:24].
    return did_text[8:24]


def _settings_from_registry() -> Optional[ClashMiSettings]:
    secret = _derive_secret(_read_registry_did())
    if secret is None:
        return None

    return ClashMiSettings(
        base_url="http://127.0.0.1:9090",
        secret=secret,
        source="registry",
    )


def _normalize_base_url(value: str) -> str:
    if value.startswith(("http://", "https://")):
        return value.rstrip("/")
    return f"http://{value}".rstrip("/")
