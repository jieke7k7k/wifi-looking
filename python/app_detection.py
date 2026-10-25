"""Built-in app path detection for XiaoMaLink trigger rules.

Implements spec section 2.2: the watched main processes are fixed,
and the install path of each app is resolved in this priority order:

1. the registry ``Uninstall`` table's ``InstallLocation`` /
   ``DisplayIcon`` (Windows only; ``registry_fn`` injectable);
2. well-known default install paths (environment variables expanded);
3. Desktop / Start Menu ``.lnk`` shortcuts, resolved by parsing the
   shortcut's LinkInfo ``LocalBasePath``.

Returns ``None`` when nothing is found -- the integration layer is
responsible for highlighting the field red and offering a Browse
button. Never raises for an expected failure (missing registry,
unreadable shortcut, unknown app key); those all yield ``None``.
"""

from __future__ import annotations

import logging
import os
import re
import struct
import sys
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Union

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Fixed targets (spec section 2.2 -- do not change without a spec change)
# ---------------------------------------------------------------------------

TARGETS: Dict[str, Dict[str, object]] = {
    "wegame": {
        "display_name": "WeGame",
        "process_name": "wegame.exe",
        # Auxiliary processes such as tgp_daemon.exe are deliberately
        # NOT listed: only the main process counts as "the app".
        "registry_keywords": ("wegame",),
        "default_paths": (
            r"C:\Program Files (x86)\WeGame\wegame.exe",
            r"C:\Program Files\WeGame\wegame.exe",
        ),
    },
    "cloudmusic": {
        "display_name": "网易云音乐",
        "process_name": "cloudmusic.exe",
        "registry_keywords": ("cloudmusic", "netease", "网易云音乐"),
        "default_paths": (
            r"%LocalAppData%\NetEase\CloudMusic\cloudmusic.exe",
            r"%ProgramFiles%\NetEase\CloudMusic\cloudmusic.exe",
            r"%ProgramFiles(x86)%\NetEase\CloudMusic\cloudmusic.exe",
        ),
    },
}

# Friendly aliases accepted for app_key.
_ALIASES = {
    "netease": "cloudmusic",
    "netease_cloudmusic": "cloudmusic",
    "wangyiyun": "cloudmusic",
    "wangyi_cloudmusic": "cloudmusic",
}


def _resolve_key(app_key: str) -> Optional[str]:
    key = (app_key or "").strip().lower()
    if key in TARGETS:
        return key
    return _ALIASES.get(key)


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

_ENV_VAR_RE = re.compile(r"%([^%]+)%")


def _expand_path(raw: str) -> str:
    """Expand ``%VAR%`` (Windows style, any platform) and ``$VAR``."""
    expanded = os.path.expanduser(raw)

    def replace(match: "re.Match[str]") -> str:
        name = match.group(1)
        if name in os.environ:
            return os.environ[name]
        lowered = name.lower()
        for key, value in os.environ.items():
            if key.lower() == lowered:
                return value
        return match.group(0)

    expanded = _ENV_VAR_RE.sub(replace, expanded)
    expanded = os.path.expandvars(expanded)
    if os.sep != "\\":
        # Default paths are written Windows-style; on non-Windows
        # platforms (unit tests) normalise so existence checks work.
        expanded = expanded.replace("\\", os.sep)
    return expanded


def _strip_icon_suffix(value: str) -> str:
    """DisplayIcon may be ``"C:\\...\\app.exe,0"``; drop the index."""
    value = value.strip().strip('"').strip()
    if "," in value:
        head = value.rsplit(",", 1)[0].strip().strip('"')
        if head.lower().endswith(".exe"):
            return head
    return value


def _candidate_paths_from_registry_value(
    value: object, process_name: str
) -> List[str]:
    """Turn one registry value (str / dict / iterable) into candidates."""
    candidates: List[str] = []
    if value is None:
        return candidates
    if isinstance(value, str):
        candidates.append(_strip_icon_suffix(value))
        return candidates
    if isinstance(value, dict):
        location = value.get("InstallLocation")
        if location:
            location = str(location).strip().strip('"')
            if location.lower().endswith(".exe"):
                candidates.append(location)
            elif location:
                candidates.append(os.path.join(location, process_name))
        icon = value.get("DisplayIcon")
        if icon:
            candidates.append(_strip_icon_suffix(str(icon)))
        return candidates
    if isinstance(value, Iterable):
        for item in value:
            candidates.extend(
                _candidate_paths_from_registry_value(item, process_name)
            )
    return candidates


def _prefer_exact_name(candidates: List[str], process_name: str) -> List[str]:
    """Existing exact-name matches first, then the rest, order kept."""
    exact = [
        c for c in candidates if os.path.basename(c).lower() == process_name.lower()
    ]
    rest = [c for c in candidates if c not in exact]
    return exact + rest


def _first_existing(candidates: List[str]) -> Optional[str]:
    for candidate in candidates:
        try:
            if candidate and os.path.isfile(_expand_path(candidate)):
                return _expand_path(candidate)
        except Exception:
            continue
    return None


# ---------------------------------------------------------------------------
# Level 1: registry (Windows only)
# ---------------------------------------------------------------------------

def _default_registry_candidates(app_key: str) -> List[str]:
    """Scan HKLM/HKCU Uninstall tables; [] off Windows or on failure."""
    if sys.platform != "win32":
        return []
    try:
        import winreg
    except Exception:
        return []
    target = TARGETS[app_key]
    keywords = [k.lower() for k in target["registry_keywords"]]  # type: ignore[union-attr]
    process_name = str(target["process_name"])
    candidates: List[str] = []
    for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        for subkey_path in (
            r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
            r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall",
        ):
            try:
                key = winreg.OpenKey(root, subkey_path)
            except OSError:
                continue
            try:
                count = winreg.QueryInfoKey(key)[0]
                for index in range(count):
                    try:
                        sub = winreg.EnumKey(key, index)
                        entry = winreg.OpenKey(key, sub)
                    except OSError:
                        continue
                    try:
                        try:
                            display = winreg.QueryValueEx(entry, "DisplayName")[0]
                        except OSError:
                            display = ""
                        if not display or not any(
                            kw in str(display).lower() for kw in keywords
                        ):
                            continue
                        record: Dict[str, str] = {}
                        for value_name in ("InstallLocation", "DisplayIcon"):
                            try:
                                record[value_name] = str(
                                    winreg.QueryValueEx(entry, value_name)[0]
                                )
                            except OSError:
                                pass
                        candidates.extend(
                            _candidate_paths_from_registry_value(
                                record, process_name
                            )
                        )
                    finally:
                        winreg.CloseKey(entry)
            finally:
                winreg.CloseKey(key)
    return _prefer_exact_name(candidates, process_name)


def _registry_candidates(
    app_key: str,
    registry_fn: Optional[Callable[..., object]],
) -> List[str]:
    if registry_fn is None:
        return _default_registry_candidates(app_key)
    try:
        try:
            raw = registry_fn(app_key)
        except TypeError:
            raw = registry_fn()
    except Exception:
        logger.debug("registry_fn failed", exc_info=True)
        return []
    process_name = str(TARGETS[app_key]["process_name"])
    return _prefer_exact_name(
        _candidate_paths_from_registry_value(raw, process_name), process_name
    )


# ---------------------------------------------------------------------------
# Level 3: .lnk shortcut parsing
# ---------------------------------------------------------------------------

def parse_lnk_target(lnk_path: Union[str, os.PathLike]) -> Optional[str]:
    """Parse a .lnk file's LinkInfo LocalBasePath; None on any failure.

    Handles the standard Shell Link header, an optional IDList, and
    LinkInfo headers of both 28- and 36-byte sizes (the latter carries
    the Unicode LocalBasePath offset). Returns the LocalBasePath plus
    CommonPathSuffix when present.
    """
    try:
        data = Path(lnk_path).read_bytes()
    except Exception:
        return None
    try:
        return _parse_lnk_bytes(data)
    except Exception:
        logger.debug("failed to parse shortcut %s", lnk_path, exc_info=True)
        return None


def _read_c_string(data: bytes, offset: int, unicode_: bool) -> Optional[str]:
    if offset <= 0 or offset >= len(data):
        return None
    if unicode_:
        end = offset
        while end + 1 < len(data) and data[end : end + 2] != b"\x00\x00":
            end += 2
        try:
            return data[offset:end].decode("utf-16-le")
        except UnicodeDecodeError:
            return None
    end = data.find(b"\x00", offset)
    if end == -1:
        end = len(data)
    try:
        return data[offset:end].decode("mbcs", errors="replace")
    except Exception:
        return data[offset:end].decode("utf-8", errors="replace")


def _parse_lnk_bytes(data: bytes) -> Optional[str]:
    if len(data) < 76:
        return None
    if data[0:4] != b"\x01\x14\x02\x00":
        return None
    (link_flags,) = struct.unpack_from("<I", data, 20)
    has_id_list = bool(link_flags & 0x01)
    has_link_info = bool(link_flags & 0x02)
    is_unicode = bool(link_flags & 0x80)
    if not has_link_info:
        return None
    offset = 76
    if has_id_list:
        if offset + 2 > len(data):
            return None
        (id_list_size,) = struct.unpack_from("<H", data, offset)
        offset += 2 + id_list_size
    if offset + 28 > len(data):
        return None
    (link_info_size, header_size, link_info_flags) = struct.unpack_from(
        "<III", data, offset
    )
    if link_info_size < 28 or offset + link_info_size > len(data):
        return None
    if header_size < 28:
        return None
    fields = struct.unpack_from("<IIII", data, offset + 12)
    # fields: VolumeIDOffset, LocalBasePathOffset,
    #         CommonNetworkRelativeLinkOffset, CommonPathSuffixOffset
    local_base_path_offset = fields[1]
    common_path_suffix_offset = fields[3]
    local_base_path_offset_unicode = 0
    common_path_suffix_offset_unicode = 0
    if header_size >= 36:
        extra = struct.unpack_from("<II", data, offset + 28)
        local_base_path_offset_unicode = extra[0]
        common_path_suffix_offset_unicode = extra[1]
    base: Optional[str] = None
    if is_unicode and local_base_path_offset_unicode:
        base = _read_c_string(
            data, offset + local_base_path_offset_unicode, True
        )
    if not base and local_base_path_offset:
        # The plain LocalBasePath field is ANSI even in a Unicode link;
        # only the *Unicode offset field above is UTF-16.
        base = _read_c_string(data, offset + local_base_path_offset, False)
    if not base:
        return None
    suffix: Optional[str] = None
    if is_unicode and common_path_suffix_offset_unicode:
        suffix = _read_c_string(
            data, offset + common_path_suffix_offset_unicode, True
        )
    if not suffix and common_path_suffix_offset:
        suffix = _read_c_string(
            data, offset + common_path_suffix_offset, False
        )
    if suffix:
        if base.endswith(("\\", "/")):
            return base + suffix
        return base + "\\" + suffix
    return base


def _shortcut_dirs(extra_search_dirs: Optional[Iterable[str]]) -> List[str]:
    dirs: List[str] = []
    for raw in (
        r"%USERPROFILE%\Desktop",
        r"%PUBLIC%\Desktop",
        r"%AppData%\Microsoft\Windows\Start Menu",
        r"%ProgramData%\Microsoft\Windows\Start Menu",
    ):
        expanded = _expand_path(raw)
        if expanded not in dirs:
            dirs.append(expanded)
    for raw in extra_search_dirs or []:
        expanded = _expand_path(raw)
        if expanded and expanded not in dirs:
            dirs.append(expanded)
    return dirs


def _shortcut_target(
    process_name: str, extra_search_dirs: Optional[Iterable[str]]
) -> Optional[str]:
    for directory in _shortcut_dirs(extra_search_dirs):
        try:
            root = Path(directory)
            if not root.is_dir():
                continue
            lnk_files = sorted(root.rglob("*.lnk"))
        except Exception:
            continue
        for lnk in lnk_files:
            target = parse_lnk_target(lnk)
            if target and os.path.basename(target).lower() == process_name.lower():
                return target
    return None


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def detect_app_path(
    app_key: str,
    registry_fn: Optional[Callable[..., object]] = None,
    extra_search_dirs: Optional[Iterable[str]] = None,
) -> Optional[str]:
    """Resolve an installed app's exe path, or ``None`` if not found.

    Priority: 1) registry Uninstall table, 2) default install paths
    (plus ``extra_search_dirs``), 3) Desktop / Start Menu shortcuts.
    """
    key = _resolve_key(app_key)
    if key is None:
        return None
    target = TARGETS[key]
    process_name = str(target["process_name"])

    # Level 1: registry.
    found = _first_existing(_registry_candidates(key, registry_fn))
    if found:
        return found

    # Level 2: default paths, then caller-supplied search dirs.
    level2: List[str] = [
        _expand_path(p) for p in target["default_paths"]  # type: ignore[union-attr]
    ]
    for directory in extra_search_dirs or []:
        level2.append(os.path.join(_expand_path(directory), process_name))
    found = _first_existing(level2)
    if found:
        return found

    # Level 3: shortcuts (parse success + exe-name match is enough;
    # a shortcut pointing at the exe is the evidence, even if the
    # target drive is temporarily unavailable).
    return _shortcut_target(process_name, extra_search_dirs)
