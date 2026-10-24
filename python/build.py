from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def find_inno_setup() -> Path | None:
    candidates = [
        Path(os.environ.get("ProgramFiles(x86)", ""), "Inno Setup 6", "ISCC.exe"),
        Path(os.environ.get("ProgramFiles", ""), "Inno Setup 6", "ISCC.exe"),
        Path(os.environ.get("LOCALAPPDATA", ""), "Programs", "Inno Setup 6", "ISCC.exe"),
    ]
    for path in candidates:
        if path.exists():
            return path
    return shutil.which("ISCC")


def main() -> int:
    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onefile",
        "--noconsole",
        "--name",
        "WiFi-Looking",
        "--icon",
        str(ROOT / "assets" / "app.ico"),
        "--add-data",
        f"{ROOT / 'assets'};assets",
        str(ROOT / "main.py"),
    ]

    print(" ".join(command))
    result = subprocess.run(command, cwd=ROOT)
    if result.returncode != 0:
        return result.returncode

    # Generate the installer seed config from DEFAULT_CONFIG (review
    # bug 15): the shipped config.json must never be the developer's
    # live runtime config. The seed is asserted to carry no rules.
    import json as _json
    import sys as _sys

    _sys.path.insert(0, str(ROOT))
    from config import DEFAULT_CONFIG

    assert DEFAULT_CONFIG["rules"] == [], "DEFAULT_CONFIG must ship without rules"
    seed_path = ROOT / "dist" / "config.json"
    seed_path.parent.mkdir(parents=True, exist_ok=True)
    seed_path.write_text(
        _json.dumps(DEFAULT_CONFIG, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"Installer seed config written: {seed_path}")

    iscc = find_inno_setup()
    if not iscc:
        # A build without an installer is incomplete, not a success
        # (review optimization 10).
        print("Inno Setup 6 not found; EXE was built but the installer was NOT.")
        print("Install Inno Setup 6, then run:")
        print("  ISCC.exe python/installer.iss")
        return 1

    installer_command = [str(iscc), str(ROOT / "installer.iss")]
    print(" ".join(installer_command))
    return subprocess.run(installer_command, cwd=ROOT).returncode


if __name__ == "__main__":
    raise SystemExit(main())
