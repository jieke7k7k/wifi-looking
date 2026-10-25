"""Tests for app_detection: fixed targets, 3-level priority, None."""

from __future__ import annotations

import os
import struct
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app_detection import TARGETS, detect_app_path, parse_lnk_target


def build_lnk(target_path: str) -> bytes:
    """Build a minimal valid .lnk pointing at ``target_path`` (ANSI)."""
    header = bytearray(76)
    header[0:4] = b"\x01\x14\x02\x00"
    struct.pack_into("<I", header, 20, 0x02)  # HasLinkInfo only
    local = target_path.encode("utf-8") + b"\x00"
    volume_size = 20
    local_offset = 28 + volume_size
    link_info_size = local_offset + len(local)
    link_info = bytearray()
    link_info += struct.pack(
        "<IIIIIII",
        link_info_size,
        28,
        0x01,  # VolumeIDAndLocalBasePath
        28,  # VolumeIDOffset
        local_offset,
        0,  # CommonNetworkRelativeLinkOffset
        0,  # CommonPathSuffixOffset
    )
    volume = struct.pack("<IIII", volume_size, 3, 0, 16) + b"C\x00\x00\x00"
    link_info += volume
    link_info += local
    assert len(link_info) == link_info_size
    return bytes(header) + bytes(link_info)


class AppDetectionTests(unittest.TestCase):
    def test_targets_are_fixed(self) -> None:
        self.assertEqual(TARGETS["wegame"]["process_name"], "wegame.exe")
        self.assertEqual(TARGETS["cloudmusic"]["process_name"], "cloudmusic.exe")
        # Auxiliary processes must not be treated as the main process.
        self.assertNotIn("tgp_daemon.exe", str(TARGETS["wegame"]))

    def test_unknown_app_returns_none(self) -> None:
        self.assertIsNone(detect_app_path("not-a-real-app"))
        self.assertIsNone(detect_app_path(""))

    def test_registry_beats_default_and_shortcut(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            reg_dir = Path(tmp) / "reg"
            reg_dir.mkdir()
            reg_exe = reg_dir / "wegame.exe"
            reg_exe.write_bytes(b"fake")
            extra = Path(tmp) / "extra"
            extra.mkdir()
            extra_exe = extra / "wegame.exe"
            extra_exe.write_bytes(b"fake")
            lnk = extra / "WeGame.lnk"
            lnk.write_bytes(build_lnk(str(extra_exe)))
            found = detect_app_path(
                "wegame",
                registry_fn=lambda _key: {"InstallLocation": str(reg_dir)},
                extra_search_dirs=[str(extra)],
            )
            self.assertEqual(found, str(reg_exe))

    def test_registry_display_icon_with_index_suffix(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            exe = Path(tmp) / "wegame.exe"
            exe.write_bytes(b"fake")
            found = detect_app_path(
                "wegame",
                registry_fn=lambda _key: {"DisplayIcon": f'"{exe},0"'},
            )
            self.assertEqual(found, str(exe))

    def test_registry_failure_falls_through_to_none(self) -> None:
        def boom(_key):
            raise RuntimeError("registry unreadable")

        self.assertIsNone(
            detect_app_path("wegame", registry_fn=boom, extra_search_dirs=[])
        )

    def test_default_path_beats_shortcut(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            default_dir = Path(tmp) / "NetEase" / "CloudMusic"
            default_dir.mkdir(parents=True)
            default_exe = default_dir / "cloudmusic.exe"
            default_exe.write_bytes(b"fake")
            extra = Path(tmp) / "extra"
            extra.mkdir()
            shortcut_target = Path(tmp) / "elsewhere" / "cloudmusic.exe"
            shortcut_target.parent.mkdir()
            shortcut_target.write_bytes(b"fake")
            (extra / "CloudMusic.lnk").write_bytes(
                build_lnk(str(shortcut_target))
            )
            old_env = dict(os.environ)
            os.environ["LocalAppData"] = tmp
            try:
                found = detect_app_path(
                    "cloudmusic",
                    registry_fn=lambda _key: None,
                    extra_search_dirs=[str(extra)],
                )
            finally:
                os.environ.clear()
                os.environ.update(old_env)
            self.assertEqual(found, str(default_exe))

    def test_shortcut_level_resolves_lnk_local_base_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "cloudmusic.exe"
            target.write_bytes(b"fake")
            extra = Path(tmp) / "shortcuts"
            extra.mkdir()
            (extra / "CloudMusic.lnk").write_bytes(build_lnk(str(target)))
            found = detect_app_path(
                "cloudmusic",
                registry_fn=lambda _key: None,
                extra_search_dirs=[str(extra)],
            )
            self.assertEqual(found, str(target))

    def test_shortcut_pointing_at_other_exe_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            extra = Path(tmp) / "shortcuts"
            extra.mkdir()
            (extra / "Other.lnk").write_bytes(
                build_lnk(str(Path(tmp) / "something_else.exe"))
            )
            self.assertIsNone(
                detect_app_path(
                    "cloudmusic",
                    registry_fn=lambda _key: None,
                    extra_search_dirs=[str(extra)],
                )
            )

    def test_unparseable_shortcut_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            extra = Path(tmp) / "shortcuts"
            extra.mkdir()
            bad = extra / "Bad.lnk"
            bad.write_bytes(b"this is not a shell link at all")
            self.assertIsNone(parse_lnk_target(bad))
            self.assertIsNone(
                detect_app_path(
                    "cloudmusic",
                    registry_fn=lambda _key: None,
                    extra_search_dirs=[str(extra)],
                )
            )

    def test_nothing_found_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(
                detect_app_path(
                    "wegame",
                    registry_fn=lambda _key: None,
                    extra_search_dirs=[tmp],
                )
            )


if __name__ == "__main__":
    unittest.main()
