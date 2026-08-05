"""Shared parameter validation for strategy params dicts.

Strategy params arrive as JSON from versioned DB rows, so every strategy must
defend against wrong types. Bools are explicitly rejected where numbers are
expected (JSON ``true`` must never pass as ``1``).
"""

from __future__ import annotations

from typing import Any


def require_int(params: dict[str, Any], key: str, default: int) -> int:
    value = params.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"param {key!r} must be an integer, got {value!r}")
    return value


def require_number(params: dict[str, Any], key: str, default: float) -> float:
    value = params.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"param {key!r} must be a number, got {value!r}")
    return float(value)


def optional_number(params: dict[str, Any], key: str) -> float | None:
    """A number param whose absence (or JSON null) disables the feature."""
    value = params.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"param {key!r} must be a number or null, got {value!r}")
    return float(value)
