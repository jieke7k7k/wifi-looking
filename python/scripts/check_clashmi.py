"""Manual diagnostic: print discovered ClashMi settings and the live
connection count. Not a unit test — it needs a running ClashMi, so it
lives in scripts/ where unittest discovery never picks it up.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from clashmi import ClashMiClient


def main() -> int:
    client = ClashMiClient()
    print(client.settings)
    print("connections", client.connection_count())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
