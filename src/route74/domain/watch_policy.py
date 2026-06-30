from __future__ import annotations

WATCH_DURATION_MINUTES = 30
WATCH_POLL_INTERVAL_SECONDS = 10
EARLY_ALERT_LEAVE_IN = 7
FINAL_ALERT_LEAVE_IN = 0


def validate_watch_policy(
    *,
    duration_minutes: object,
    poll_interval_seconds: object,
    early_alert_leave_in: object,
    final_alert_leave_in: object,
) -> None:
    _ensure_positive_int("watch duration", duration_minutes)
    _ensure_positive_int("watch poll interval", poll_interval_seconds)
    _ensure_non_negative_int("early alert leave-in", early_alert_leave_in)
    _ensure_non_negative_int("final alert leave-in", final_alert_leave_in)
    if final_alert_leave_in != 0:
        raise ValueError("final alert leave-in must be zero")
    if early_alert_leave_in <= final_alert_leave_in:
        raise ValueError("early alert leave-in must be greater than final alert")
    if early_alert_leave_in >= duration_minutes:
        raise ValueError("early alert leave-in must be below watch duration")
    if poll_interval_seconds > duration_minutes * 60:
        raise ValueError("watch poll interval must fit watch duration")


def _ensure_positive_int(label: str, value: object) -> None:
    if _invalid_int(value) or value <= 0:
        raise ValueError(f"{label} must be a positive integer")


def _ensure_non_negative_int(label: str, value: object) -> None:
    if _invalid_int(value) or value < 0:
        raise ValueError(f"{label} must be a non-negative integer")


def _invalid_int(value: object) -> bool:
    return isinstance(value, bool) or not isinstance(value, int)


validate_watch_policy(
    duration_minutes=WATCH_DURATION_MINUTES,
    poll_interval_seconds=WATCH_POLL_INTERVAL_SECONDS,
    early_alert_leave_in=EARLY_ALERT_LEAVE_IN,
    final_alert_leave_in=FINAL_ALERT_LEAVE_IN,
)
