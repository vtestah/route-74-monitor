from __future__ import annotations

from route74.domain.eta_policy import (
    HIGH_TARGET_WAIT_MINUTES,
    HISTORY_TARGET_WAIT_MINUTES,
    LOW_TARGET_WAIT_MINUTES,
    MEDIUM_TARGET_WAIT_MINUTES,
)


TARGET_STOP_WAIT_MINUTES = 2


def validate_wait_policy(
    *,
    target_stop_wait_minutes: object,
    high_confidence_target_wait_minutes: object,
    medium_confidence_target_wait_minutes: object,
    low_confidence_target_wait_minutes: object,
    history_target_wait_minutes: object,
) -> None:
    target_wait = _positive_int("target stop wait", target_stop_wait_minutes)
    high_wait = _positive_int("high confidence target wait", high_confidence_target_wait_minutes)
    medium_wait = _positive_int("medium confidence target wait", medium_confidence_target_wait_minutes)
    low_wait = _positive_int("low confidence target wait", low_confidence_target_wait_minutes)
    history_wait = _positive_int("history target wait", history_target_wait_minutes)
    if target_wait != high_wait:
        raise ValueError("target stop wait must match high confidence target wait")
    if target_wait >= medium_wait:
        raise ValueError("target stop wait must stay below medium confidence target wait")
    if medium_wait >= low_wait:
        raise ValueError("medium confidence target wait must stay below low confidence target wait")
    if low_wait >= history_wait:
        raise ValueError("low confidence target wait must stay below history target wait")


def _positive_int(label: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{label} must be a positive integer")
    return value


validate_wait_policy(
    target_stop_wait_minutes=TARGET_STOP_WAIT_MINUTES,
    high_confidence_target_wait_minutes=HIGH_TARGET_WAIT_MINUTES,
    medium_confidence_target_wait_minutes=MEDIUM_TARGET_WAIT_MINUTES,
    low_confidence_target_wait_minutes=LOW_TARGET_WAIT_MINUTES,
    history_target_wait_minutes=HISTORY_TARGET_WAIT_MINUTES,
)
