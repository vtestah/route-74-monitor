from __future__ import annotations

from math import isfinite

from route74.sources.yandex.models import YandexSourceMode


def require_bool(name: str, value: object) -> None:
    if not isinstance(value, bool):
        raise ValueError(f"Yandex config {name} must be a boolean")


def require_mode(name: str, value: object) -> None:
    if not isinstance(value, YandexSourceMode):
        raise ValueError(f"Yandex config {name} must be YandexSourceMode")


def require_non_negative_int(name: str, value: object) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"Yandex config {name} must be a non-negative integer")


def require_positive_float(name: str, value: object) -> None:
    if _invalid_number(value) or value <= 0:
        raise ValueError(f"Yandex config {name} must be a positive finite number")


def require_non_negative_float(name: str, value: object) -> None:
    if _invalid_number(value) or value < 0:
        raise ValueError(f"Yandex config {name} must be a non-negative finite number")


def _invalid_number(value: object) -> bool:
    return isinstance(value, bool) or not isinstance(value, int | float) or not isfinite(value)
