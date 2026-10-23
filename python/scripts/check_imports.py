"""Manual smoke check: verify PySide6 is importable in this environment.

Not a unit test (no assertions, prints only), so it lives in scripts/
where unittest discovery never picks it up.
"""

import PySide6
from PySide6.QtWidgets import QApplication  # noqa: F401

print("ok")
