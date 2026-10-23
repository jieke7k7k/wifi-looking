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

    iscc = find_inno_setup()
    if not iscc:
        print("Inno Setup 6 not found; EXE was built, installer skipped.")
        print("Install Inno Setup 6, then run the generated WiFi-Looking.exe installer script:")
        print("  ISCC.exe python/installer.iss")
        return 0

    installer_command = [str(iscc), str(ROOT / "installer.iss")]
    print(" ".join(installer_command))
    return subprocess.run(installer_command, cwd=ROOT).returncode


if __name__ == "__main__":
    raise SystemExit(main())
