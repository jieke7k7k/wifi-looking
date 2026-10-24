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
    # Never print the settings object itself: it carries the
    # controller secret (pre-check item 3). Only the endpoint and
    # source are diagnostic; the secret is shown as present/absent.
    print("base_url", client.settings.base_url)
    print("source", client.settings.source)
    print("secret_present", bool(client.settings.secret))
    print("connections", client.connection_count())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
