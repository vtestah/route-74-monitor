from __future__ import annotations

from route74.domain.profiles import EVENING, MORNING


MORNING_AUTO_START = MORNING.window_start
MORNING_AUTO_END = MORNING.window_end
EVENING_AUTO_START = EVENING.window_start
EVENING_AUTO_END = EVENING.window_end
GO_NOW_THRESHOLD_MINUTES = 0
GET_READY_THRESHOLD_MINUTES = 5


def validate_departure_thresholds(
    *,
    go_now_threshold_minutes: object,
    get_ready_threshold_minutes: object,
) -> None:
    _ensure_int("go-now threshold", go_now_threshold_minutes)
    _ensure_int("get-ready threshold", get_ready_threshold_minutes)
    if go_now_threshold_minutes != 0:
        raise ValueError("go-now threshold must be zero")
    if get_ready_threshold_minutes <= go_now_threshold_minutes:
        raise ValueError("get-ready threshold must be greater than go-now threshold")


def _ensure_int(label: str, value: object) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{label} must be an integer")
