from __future__ import annotations

from dataclasses import dataclass
from math import isfinite


@dataclass(frozen=True)
class RouteTrafficSnapshot:
    provider: str
    status: str
    jams_level: int | None = None
    route_duration_seconds: int | None = None
    route_duration_in_traffic_seconds: int | None = None
    delay_seconds: int | None = None
    distance_meters: int | None = None
    raw: dict[str, object] | None = None

    def __post_init__(self) -> None:
        _ensure_plain_key("traffic provider", self.provider)
        _ensure_plain_key("traffic status", self.status)
        if self.raw is not None:
            _ensure_json_object("traffic raw", self.raw)
        if self.jams_level is not None and (
            isinstance(self.jams_level, bool)
            or not isinstance(self.jams_level, int)
            or self.jams_level < 0
            or self.jams_level > 10
        ):
            raise ValueError("traffic jams level must be from 0 to 10")
        for label, value in (
            ("route duration", self.route_duration_seconds),
            ("traffic route duration", self.route_duration_in_traffic_seconds),
            ("traffic distance", self.distance_meters),
        ):
            _ensure_positive_integer(label, value)
        _ensure_non_negative_integer("traffic delay", self.delay_seconds)


def _ensure_plain_key(label: str, value: str) -> None:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} is required")
    if value != value.strip() or any(char.isspace() for char in value):
        raise ValueError(f"{label} must be a plain key")
    if not value.isascii() or any(not (char.isalnum() or char == "_") for char in value):
        raise ValueError(f"{label} must be a plain key")


def _ensure_json_object(label: str, value: object) -> None:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")
    for key, item in value.items():
        if not isinstance(key, str) or not key:
            raise ValueError(f"{label} must use string keys")
        _ensure_json_value(label, item)


def _ensure_json_value(label: str, value: object) -> None:
    if value is None or isinstance(value, str | bool | int):
        return
    if isinstance(value, float):
        if isfinite(value):
            return
        raise ValueError(f"{label} must not contain non-finite numbers")
    if isinstance(value, list):
        for item in value:
            _ensure_json_value(label, item)
        return
    if isinstance(value, dict):
        _ensure_json_object(label, value)
        return
    raise ValueError(f"{label} must contain only JSON values")


def _ensure_positive_integer(label: str, value: int | None) -> None:
    if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value <= 0):
        raise ValueError(f"{label} must be a positive integer")


def _ensure_non_negative_integer(label: str, value: int | None) -> None:
    if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
        raise ValueError(f"{label} must be a non-negative integer")
