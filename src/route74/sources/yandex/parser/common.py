from __future__ import annotations

from collections.abc import Iterable
from math import isfinite
from typing import Any


def as_float(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if isfinite(number) else None


def number_at(item: dict[str, Any], keys: Iterable[str]) -> float | None:
    for key in keys:
        value = as_float(item.get(key))
        if value is not None:
            return value
    return None
