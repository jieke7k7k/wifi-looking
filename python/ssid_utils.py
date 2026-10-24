"""Single source of truth for SSID comparison.

Every place that asks "is this SSID the same as that one" — the rule
engine, the startup guard, duplicate detection and the legacy-config
migration fingerprint — must use the same normalization, otherwise the
components disagree (review v0.3.0 bug 6: the engine matched
case-insensitively while the guard compared exactly).
"""

from __future__ import annotations

from typing import Any, Iterable, List


def normalize_ssid(value: Any) -> str:
    """Canonical SSID form: stripped and case-folded ("" for missing)."""
    if value is None:
        return ""
    return str(value).strip().lower()


def ssids_of(value: Any) -> List[str]:
    """Normalize an SSID field (str | list | missing) to a list of strs."""
    if isinstance(value, list):
        return [str(item) for item in value if str(item)]
    if value:
        return [str(value)]
    return []


def normalized_ssid_set(value: Any) -> frozenset:
    """The set of normalized SSIDs in a str|list field ("" dropped)."""
    return frozenset(
        normalized for normalized in (normalize_ssid(v) for v in ssids_of(value)) if normalized
    )


def ssid_matches(patterns: Any, actual: Any) -> bool:
    """True if ``actual`` matches any pattern ("*" matches everything).

    An empty/missing pattern matches only an empty/missing actual SSID,
    mirroring the rule engine's historical semantics.
    """
    if patterns is None:
        return True
    if not isinstance(patterns, list):
        patterns = [patterns]
    actual_norm = normalize_ssid(actual)
    for pattern in patterns:
        if isinstance(pattern, str) and pattern.strip() == "*":
            return True
        pattern_norm = normalize_ssid(pattern)
        if not pattern_norm:
            if not actual_norm:
                return True
            continue
        if pattern_norm == actual_norm:
            return True
    return False


def same_ssid_set(a: Iterable[str], b: Iterable[str]) -> bool:
    return normalized_ssid_set(list(a)) == normalized_ssid_set(list(b))
