"""Tests for the global reference-counted restore stack (spec 4.3).

Covers: first record wins for a shared property, restore only when
the last holder exits, no restore on a middle exit, discard drops
without restoring, crash recovery from restore_state.json, atomic
snapshot writes, and property/value validation.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from restore_stack import ALLOWED_PROPERTIES, RestoreError, RestoreStack


class FirstRecordWinsTests(unittest.TestCase):
    def test_two_rules_same_property_first_value_kept(self) -> None:
        stack = RestoreStack()
        stack.record("rule-a", "brightness", 50)
        stack.record("rule-b", "brightness", 90)
        self.assertEqual(stack.pending, {"brightness": 50})

    def test_same_rule_re_record_does_not_overwrite(self) -> None:
        stack = RestoreStack()
        stack.record("rule-a", "brightness", 50)
        stack.record("rule-a", "brightness", 10)
        self.assertEqual(stack.pending, {"brightness": 50})

    def test_add_ref_does_not_change_initial(self) -> None:
        stack = RestoreStack()
        stack.record("rule-a", "brightness", 50)
        stack.add_ref("rule-b", "brightness")
        self.assertEqual(stack.pending, {"brightness": 50})
        self.assertEqual(stack.ref_counts, {"brightness": 2})


class LastExitRestoresTests(unittest.TestCase):
    def test_middle_exit_restores_nothing(self) -> None:
        stack = RestoreStack()
        stack.record("rule-a", "brightness", 50)
        stack.record("rule-b", "brightness", 90)
        self.assertEqual(stack.release("rule-a"), {})
        self.assertEqual(stack.pending, {"brightness": 50})

    def test_last_exit_restores_initial(self) -> None:
        stack = RestoreStack()
        stack.record("rule-a", "brightness", 50)
        stack.record("rule-b", "brightness", 90)
        stack.release("rule-a")
        self.assertEqual(stack.release("rule-b"), {"brightness": 50})
        self.assertEqual(stack.pending, {})

    def test_release_returns_only_freed_properties(self) -> None:
        stack = RestoreStack()
        stack.record("rule-a", "brightness", 50)
        stack.record("rule-a", "volume", 70)
        stack.record("rule-b", "brightness", 90)
        # rule-a exits: volume is freed, brightness is still held by b.
        self.assertEqual(stack.release("rule-a"), {"volume": 70})
        self.assertEqual(stack.pending, {"brightness": 50})

    def test_release_unknown_rule_is_noop(self) -> None:
        stack = RestoreStack()
        stack.record("rule-a", "brightness", 50)
        self.assertEqual(stack.release("rule-nope"), {})
        self.assertEqual(stack.pending, {"brightness": 50})

    def test_add_ref_holder_blocks_restore(self) -> None:
        stack = RestoreStack()
        stack.record("rule-a", "brightness", 50)
        stack.add_ref("rule-b", "brightness")
        self.assertEqual(stack.release("rule-a"), {})
        self.assertEqual(stack.release("rule-b"), {"brightness": 50})


class DiscardTests(unittest.TestCase):
    def test_discard_shared_property_restores_nothing_keeps_initial(self) -> None:
        stack = RestoreStack()
        stack.record("rule-a", "brightness", 50)
        stack.record("rule-b", "brightness", 90)
        stack.discard("rule-a")
        self.assertEqual(stack.pending, {"brightness": 50})
        # The remaining holder still restores on exit.
        self.assertEqual(stack.release("rule-b"), {"brightness": 50})

    def test_discard_sole_holder_drops_without_restore(self) -> None:
        stack = RestoreStack()
        stack.record("rule-a", "brightness", 50)
        stack.discard("rule-a")
        self.assertEqual(stack.pending, {})
        # Nothing left to restore afterwards, either.
        self.assertEqual(stack.release("rule-a"), {})

    def test_discard_unknown_rule_is_noop(self) -> None:
        stack = RestoreStack()
        stack.record("rule-a", "brightness", 50)
        stack.discard("rule-nope")
        self.assertEqual(stack.pending, {"brightness": 50})


class ValidationTests(unittest.TestCase):
    def test_unknown_property_rejected(self) -> None:
        stack = RestoreStack()
        with self.assertRaises(ValueError):
            stack.record("rule-a", "touchpad_enabled", True)
        with self.assertRaises(ValueError):
            stack.add_ref("rule-a", "touchpad_enabled")
        self.assertEqual(stack.pending, {})

    def test_allowed_properties_cover_spec_list(self) -> None:
        self.assertEqual(
            ALLOWED_PROPERTIES,
            {
                "bluetooth_enabled",
                "brightness",
                "volume",
                "muted",
                "dark_mode",
                "wallpaper_path",
                "power_plan",
                "screen_timeout_minutes",
            },
        )

    def test_non_json_value_rejected(self) -> None:
        stack = RestoreStack()
        with self.assertRaises(ValueError):
            stack.record("rule-a", "brightness", {1, 2})
        with self.assertRaises(ValueError):
            stack.record("rule-a", "wallpaper_path", b"bytes")
        with self.assertRaises(ValueError):
            stack.record("rule-a", "brightness", float("inf"))
        self.assertEqual(stack.pending, {})

    def test_all_json_value_kinds_roundtrip(self) -> None:
        stack = RestoreStack()
        stack.record("rule-a", "bluetooth_enabled", True)
        stack.record("rule-a", "muted", False)
        stack.record("rule-a", "wallpaper_path", "C:\\wall.jpg")
        stack.record("rule-a", "power_plan", None)
        self.assertEqual(
            stack.pending,
            {
                "bluetooth_enabled": True,
                "muted": False,
                "wallpaper_path": "C:\\wall.jpg",
                "power_plan": None,
            },
        )

    def test_pending_returns_a_copy(self) -> None:
        stack = RestoreStack()
        stack.record("rule-a", "brightness", 50)
        pending = stack.pending
        pending["brightness"] = 999
        self.assertEqual(stack.pending, {"brightness": 50})


class CrashRecoveryTests(unittest.TestCase):
    def test_rebuild_from_restore_state_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "restore_state.json"
            stack = RestoreStack(path)
            stack.record("rule-a", "brightness", 40)
            stack.record("rule-b", "volume", 70)
            stack.record("rule-b", "brightness", 90)
            self.assertTrue(path.exists())

            # Simulate a crash: drop the stack, rebuild from the file.
            rebuilt = RestoreStack.load_pending(path)
            self.assertEqual(
                rebuilt.pending, {"brightness": 40, "volume": 70}
            )
            self.assertEqual(rebuilt.ref_counts, {"brightness": 2, "volume": 1})
            # rule-a exits: brightness is still held by rule-b, so
            # nothing restores yet.
            self.assertEqual(rebuilt.release("rule-a"), {})
            self.assertEqual(
                rebuilt.pending, {"brightness": 40, "volume": 70}
            )
            # Last holder exits: both properties restore.
            self.assertEqual(
                rebuilt.release("rule-b"),
                {"brightness": 40, "volume": 70},
            )
            self.assertEqual(rebuilt.pending, {})

    def test_snapshot_is_valid_json_and_atomic(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "restore_state.json"
            stack = RestoreStack(path)
            stack.record("rule-a", "brightness", 40)
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data["initial"], {"brightness": 40})
            leftovers = [p.name for p in Path(tmp).iterdir() if p.suffix == ".tmp"]
            self.assertEqual(leftovers, [])
            # After the last exit the file reflects the empty snapshot.
            stack.release("rule-a")
            self.assertEqual(
                json.loads(path.read_text(encoding="utf-8"))["initial"], {}
            )

    def test_load_pending_missing_file_is_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            stack = RestoreStack.load_pending(Path(tmp) / "restore_state.json")
            self.assertEqual(stack.pending, {})

    def test_load_pending_corrupt_file_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "restore_state.json"
            path.write_text("{ not json", encoding="utf-8")
            with self.assertRaises(RestoreError):
                RestoreStack.load_pending(path)


class PendingAndCommitReleaseTests(unittest.TestCase):
    """P0-4：恢复动作成功后才提交移除，失败保留可重试。"""

    def test_pending_for_release_does_not_mutate(self) -> None:
        stack = RestoreStack()
        stack.record("rule-a", "brightness", 50)
        self.assertEqual(stack.pending_for_release("rule-a"), {"brightness": 50})
        self.assertEqual(stack.pending, {"brightness": 50})
        self.assertEqual(stack.ref_counts, {"brightness": 1})

    def test_commit_release_only_removes_committed_property(self) -> None:
        stack = RestoreStack()
        stack.record("rule-a", "brightness", 50)
        stack.record("rule-a", "volume", 70)
        # 只成功恢复了 brightness，volume 失败：volume 初始值必须保留待重试。
        stack.commit_release("rule-a", ["brightness"])
        self.assertEqual(stack.pending, {"volume": 70})
        # 保留的 volume 可在下一次启动/重试流程中继续被清除。
        stack.clear_pending_property("volume")
        self.assertEqual(stack.pending, {})

    def test_shared_property_not_released_until_last_holder_commits(self) -> None:
        stack = RestoreStack()
        stack.record("rule-a", "brightness", 50)
        stack.record("rule-b", "brightness", 90)
        self.assertEqual(stack.pending_for_release("rule-a"), {})
        stack.commit_release("rule-a", [])
        self.assertEqual(stack.pending, {"brightness": 50})
        self.assertEqual(stack.pending_for_release("rule-b"), {"brightness": 50})

    def test_clear_pending_property_removes_single_item(self) -> None:
        stack = RestoreStack()
        stack.record("rule-a", "brightness", 50)
        stack.record("rule-a", "volume", 70)
        stack.clear_pending_property("brightness")
        self.assertEqual(stack.pending, {"volume": 70})


if __name__ == "__main__":
    unittest.main()
