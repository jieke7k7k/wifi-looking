"""Global reference-counted restore stack (XiaoMaLink v1.0.0, spec section 4.3).

When a rule with its "restore on exit" switch on modifies a system
setting (bluetooth, brightness, volume, ...), the value *before* the
modification is recorded here. Several rules may modify the same
property at the same time; only the first recorded value is the true
system initial value, and it is only restored when the last rule
holding a reference to that property exits.

Persistence: after every mutation the pending snapshot is written
atomically (temp file + ``os.replace``) to ``restore_state.json``, so
a crash or a reboot mid-game leaves a snapshot the next startup can
rebuild with :meth:`RestoreStack.load_pending` and restore (or offer
to restore). This module deliberately implements its own atomic
write instead of importing config's private helper.

Reference model (one reference per rule per property): ``record`` and
``add_ref`` both give the calling rule one reference to a property;
re-recording the same rule/property is idempotent and never bumps a
count. ``release`` drops all of the exiting rule's references and
returns the properties whose reference set just became empty, together
with their initial values. ``discard`` drops the rule's references
without returning anything (rule deleted: nothing is restored).

Values are JSON types only (None / bool / int / float / str / list /
dict of JSON values); anything else is rejected so the snapshot file
is always valid JSON.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import tempfile
import threading
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Set

logger = logging.getLogger(__name__)

__all__ = ["RestoreStack", "ALLOWED_PROPERTIES", "RestoreError"]


class RestoreError(Exception):
    """Raised when a restore snapshot cannot be rebuilt."""


# Fixed property names (spec section 4.3 / task definition). No other
# property name is accepted by record() / add_ref().
ALLOWED_PROPERTIES = frozenset(
    {
        "bluetooth_enabled",
        "brightness",
        "volume",
        "muted",
        "dark_mode",
        "wallpaper_path",
        "power_plan",
        "screen_timeout_minutes",
    }
)


def _check_property(property_name: str) -> None:
    if property_name not in ALLOWED_PROPERTIES:
        raise ValueError(
            f"unknown restore property: {property_name!r}; "
            f"allowed: {sorted(ALLOWED_PROPERTIES)}"
        )


def _check_json_value(value: Any) -> None:
    """Reject anything that is not a plain JSON value.

    Strictly recursive (no json.dumps coercion): tuples, sets,
    bytes, non-string dict keys and non-finite floats are all
    rejected, so a recorded value always round-trips through the
    snapshot file exactly.
    """
    import math

    if value is None or isinstance(value, (bool, str)):
        return
    if isinstance(value, int):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("restore value is not a JSON value: non-finite float")
        return
    if isinstance(value, list):
        for item in value:
            _check_json_value(item)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError(
                    "restore value is not a JSON value: non-string dict key"
                )
            _check_json_value(item)
        return
    raise ValueError(f"restore value is not a JSON value: {type(value).__name__}")


def _write_text_atomic(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` atomically (temp file + os.replace).

    Implemented locally (not imported from config) so this module
    stays importable on its own. A crash mid-write never leaves a
    truncated snapshot behind.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=path.name + ".", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


class RestoreStack:
    """Reference-counted store of system values pending restore."""

    def __init__(self, state_path: Path | str | None = None) -> None:
        self.state_path: Optional[Path] = (
            Path(state_path) if state_path is not None else None
        )
        self._initial: Dict[str, Any] = {}
        self._refs: Dict[str, Set[str]] = {}
        self._lock = threading.Lock()

    # -- queries ------------------------------------------------------

    @property
    def pending(self) -> Dict[str, Any]:
        """Pending restores: ``{property: initial_value}`` (a copy)."""
        with self._lock:
            return copy.deepcopy(self._initial)

    @property
    def ref_counts(self) -> Dict[str, int]:
        """Current reference count per pending property."""
        with self._lock:
            return {prop: len(rules) for prop, rules in self._refs.items()}

    def __len__(self) -> int:
        with self._lock:
            return len(self._initial)

    # -- mutation -----------------------------------------------------

    def record(self, rule_id: str, property_name: str, value: Any) -> None:
        """Record ``rule_id`` modifying ``property_name``.

        Only the first recorded value for a property is kept as the
        true initial value; later records for the same property only
        add a reference and never overwrite it.
        """
        _check_property(property_name)
        _check_json_value(value)
        with self._lock:
            if property_name not in self._initial:
                self._initial[property_name] = copy.deepcopy(value)
            self._refs.setdefault(property_name, set()).add(rule_id)
        self._persist()

    def add_ref(self, rule_id: str, property_name: str) -> None:
        """Add ``rule_id`` as another holder of ``property_name``.

        Used when a rule modifies a property whose initial value was
        already recorded by an earlier rule. If no initial value has
        been recorded yet, the reference is still tracked but there
        is nothing to restore for it until a ``record`` supplies the
        initial value.
        """
        _check_property(property_name)
        with self._lock:
            self._refs.setdefault(property_name, set()).add(rule_id)
        self._persist()

    def release(self, rule_id: str) -> Dict[str, Any]:
        """Drop ``rule_id``'s references; return newly restorable values.

        Returns ``{property: initial_value}`` for every property whose
        reference set became empty because of this exit. Properties
        still held by other rules are not returned and their initial
        values are kept. Unknown rules return ``{}``.
        """
        restored: Dict[str, Any] = {}
        changed = False
        with self._lock:
            for property_name in list(self._refs):
                holders = self._refs[property_name]
                if rule_id not in holders:
                    continue
                holders.discard(rule_id)
                changed = True
                if not holders:
                    del self._refs[property_name]
                    if property_name in self._initial:
                        restored[property_name] = self._initial.pop(property_name)
        if changed:
            self._persist()
        return restored

    def pending_for_release(self, rule_id: str) -> Dict[str, Any]:
        """查看 ``rule_id`` 退出时将释放的属性原值，但不修改栈。

        P0-4 修复：恢复动作必须真正成功后才提交移除，故先取“待恢复值”，
        执行成功后再调用 :meth:`commit_release` 提交，失败则保留以便重试。
        """
        restored: Dict[str, Any] = {}
        with self._lock:
            for property_name, holders in self._refs.items():
                if rule_id in holders and len(holders) == 1:
                    if property_name in self._initial:
                        restored[property_name] = copy.deepcopy(
                            self._initial[property_name]
                        )
        return restored

    def commit_release(
        self, rule_id: str, restored_properties: Iterable[str]
    ) -> None:
        """提交 ``rule_id`` 的退出：引用全部退出，仅恢复成功的属性清原值。

        ``restored_properties`` 是已成功执行恢复动作的属性集合。退出规则
        在所有属性上的引用都会被移除；引用归零且恢复成功的属性才清除初始
        值，引用归零但恢复失败的属性保留初始值（可待下次启动/重试恢复）。
        """
        restored_set = set(restored_properties)
        changed = False
        with self._lock:
            for property_name in list(self._refs):
                holders = self._refs[property_name]
                if rule_id not in holders:
                    continue
                holders.discard(rule_id)
                changed = True
                if not holders:
                    del self._refs[property_name]
                    if property_name in restored_set:
                        self._initial.pop(property_name, None)
        if changed:
            self._persist()

    def clear_pending_property(self, property_name: str) -> None:
        """清除单个属性的待恢复状态（启动恢复成功一项就清一项）。"""
        changed = False
        with self._lock:
            if property_name in self._initial or property_name in self._refs:
                self._initial.pop(property_name, None)
                self._refs.pop(property_name, None)
                changed = True
        if changed:
            self._persist()

    def discard(self, rule_id: str) -> None:
        """Drop all of ``rule_id``'s records/references, restoring nothing.

        Used when a rule is deleted: its references go away, but no
        restore is triggered. If the discarded rule was the sole holder
        of a property, that property's pending initial value is dropped
        silently; if other rules still hold the property, its initial
        value is kept for them.
        """
        changed = False
        with self._lock:
            for property_name in list(self._refs):
                holders = self._refs[property_name]
                if rule_id not in holders:
                    continue
                holders.discard(rule_id)
                changed = True
                if not holders:
                    del self._refs[property_name]
                    self._initial.pop(property_name, None)
        if changed:
            self._persist()

    # -- persistence --------------------------------------------------

    def _snapshot_text(self) -> str:
        with self._lock:
            snapshot = {
                "version": 1,
                "initial": copy.deepcopy(self._initial),
                "refs": {
                    prop: sorted(rules) for prop, rules in self._refs.items()
                },
            }
        return json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n"

    def _persist(self) -> None:
        """Atomically rewrite the snapshot file after a mutation."""
        if self.state_path is None:
            return
        try:
            _write_text_atomic(self.state_path, self._snapshot_text())
        except OSError:
            # A failed snapshot write must not crash the rule engine;
            # the in-memory stack is still correct and the next
            # mutation retries the write. Log loudly: a missing
            # snapshot means a crash would lose pending restores.
            logger.warning(
                "Could not persist restore snapshot to %s", self.state_path,
                exc_info=True,
            )

    @classmethod
    def load_pending(cls, path: Path | str) -> "RestoreStack":
        """Rebuild a stack from an unfinished ``restore_state.json``.

        Missing file (or empty file) yields an empty stack bound to
        that path. A malformed file raises :class:`RestoreError` so
        the caller can alert instead of silently losing restores.
        """
        state_path = Path(path)
        stack = cls(state_path)
        if not state_path.exists():
            return stack
        try:
            raw = state_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise RestoreError(f"cannot read restore snapshot {state_path}: {exc}") from exc
        if not raw.strip():
            return stack
        try:
            snapshot = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RestoreError(
                f"invalid JSON in restore snapshot {state_path}: {exc}"
            ) from exc
        if not isinstance(snapshot, dict):
            raise RestoreError(
                f"invalid restore snapshot {state_path}: root must be an object"
            )
        initial = snapshot.get("initial", {})
        refs = snapshot.get("refs", {})
        if not isinstance(initial, dict) or not isinstance(refs, dict):
            raise RestoreError(
                f"invalid restore snapshot {state_path}: "
                "'initial' and 'refs' must be objects"
            )
        for property_name, value in initial.items():
            if property_name not in ALLOWED_PROPERTIES:
                continue
            try:
                _check_json_value(value)
            except ValueError:
                continue
            stack._initial[property_name] = copy.deepcopy(value)
        for property_name, holders in refs.items():
            if property_name not in ALLOWED_PROPERTIES:
                continue
            if isinstance(holders, list):
                rules = {h for h in holders if isinstance(h, str)}
            elif isinstance(holders, dict):
                rules = {h for h in holders if isinstance(h, str)}
            else:
                continue
            if rules:
                stack._refs[property_name] = rules
        # Drop initial values with no holders left (nothing to restore
        # them for) and holders with no initial value (nothing to
        # restore); both are inert leftovers of an interrupted write.
        for property_name in list(stack._initial):
            if property_name not in stack._refs:
                del stack._initial[property_name]
        for property_name in list(stack._refs):
            if property_name not in stack._initial:
                del stack._refs[property_name]
        return stack
