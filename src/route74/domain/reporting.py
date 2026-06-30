from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, time
from types import MappingProxyType

from route74.domain.profiles import PROFILE_KEYS
from route74.models import NOVOSIBIRSK_TZ


WEEKDAY_COUNT = 5


@dataclass(frozen=True)
class ReportWindow:
    key: str
    profile_key: str
    title: str
    start: time
    end: time

    def __post_init__(self) -> None:
        _validate_window_key(self.key)
        for label, value in (
            ("profile key", self.profile_key),
            ("title", self.title),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"report window {label} is required")
        if self.profile_key not in PROFILE_KEYS:
            expected = ", ".join(PROFILE_KEYS)
            raise ValueError(f"report window profile key must be one of {expected}")
        _validate_window_time("start", self.start)
        _validate_window_time("end", self.end)
        if self.start >= self.end:
            raise ValueError("report window end must be after start")


def _validate_window_key(value: object) -> None:
    if not isinstance(value, str) or not value:
        raise ValueError("report window key is required")
    has_plain_chars = all(char.isalnum() or char == "_" for char in value)
    if value != value.strip() or not value.isascii() or not has_plain_chars:
        raise ValueError("report window key must be a plain ASCII key")


def validate_report_windows(windows: Iterable[ReportWindow]) -> tuple[ReportWindow, ...]:
    validated = tuple(windows)
    if not validated:
        raise ValueError("report windows need at least one window")
    for window in validated:
        if not isinstance(window, ReportWindow):
            raise ValueError("report windows need ReportWindow entries")
    seen_keys: set[str] = set()
    by_profile: dict[str, list[ReportWindow]] = {}
    for window in validated:
        if window.key in seen_keys:
            raise ValueError(f"duplicate report window key: {window.key}")
        seen_keys.add(window.key)
        profile_windows = by_profile.setdefault(window.profile_key, [])
        for existing in profile_windows:
            if _windows_overlap(existing, window):
                raise ValueError(
                    f"report windows overlap for profile {window.profile_key}: {existing.key}, {window.key}"
                )
        profile_windows.append(window)
    return validated


def _validate_window_time(label: str, value: time) -> None:
    if not isinstance(value, time):
        raise ValueError(f"report window {label} must be a time")
    if value.tzinfo is not None:
        raise ValueError(f"report window {label} must be timezone-naive")
    if value.second or value.microsecond:
        raise ValueError(f"report window {label} must use minute precision")


def _windows_overlap(left: ReportWindow, right: ReportWindow) -> bool:
    return left.start < right.end and right.start < left.end


REPORT_WINDOWS: tuple[ReportWindow, ...] = validate_report_windows(
    (
        ReportWindow(
            key="weekday_morning_09_12",
            profile_key="morning",
            title="Будни утром 09-12",
            start=time(9, 0),
            end=time(12, 0),
        ),
        ReportWindow(
            key="weekday_evening_19_22",
            profile_key="evening",
            title="Будни вечером 19-22",
            start=time(19, 0),
            end=time(22, 0),
        ),
    )
)
REPORT_WINDOWS_BY_KEY: Mapping[str, ReportWindow] = MappingProxyType(
    {window.key: window for window in REPORT_WINDOWS}
)
REPORT_WINDOW_KEYS: tuple[str, ...] = tuple(REPORT_WINDOWS_BY_KEY)
ALL_REPORT_WINDOWS_KEY = "all"
REPORT_WINDOW_SELECTORS: tuple[str, ...] = (*REPORT_WINDOW_KEYS, ALL_REPORT_WINDOWS_KEY)


def report_window_by_key(key: str) -> ReportWindow:
    try:
        return REPORT_WINDOWS_BY_KEY[key]
    except KeyError as exc:
        expected = ", ".join(REPORT_WINDOW_KEYS)
        raise ValueError(f"unknown report window: {key} (expected {expected})") from exc


def report_window_for_profile(profile_key: str) -> ReportWindow:
    _validate_profile_filter(profile_key)
    windows = tuple(window for window in REPORT_WINDOWS if window.profile_key == profile_key)
    if len(windows) != 1:
        keys = ", ".join(window.key for window in windows) or "-"
        raise ValueError(f"profile {profile_key} needs exactly one report window, got {keys}")
    return windows[0]


def matching_report_window(sampled_at: datetime, profile_key: str | None = None) -> ReportWindow | None:
    _validate_sampled_at(sampled_at)
    _validate_profile_filter(profile_key)
    if sampled_at.weekday() >= WEEKDAY_COUNT:
        return None
    sample_time = sampled_at.timetz().replace(tzinfo=None)
    for window in REPORT_WINDOWS:
        if profile_key is not None and window.profile_key != profile_key:
            continue
        if window.start <= sample_time < window.end:
            return window
    return None


def report_profiles_for_time(sampled_at: datetime) -> tuple[str, ...]:
    _validate_sampled_at(sampled_at)
    return tuple(window.profile_key for window in REPORT_WINDOWS if matching_report_window(sampled_at, window.profile_key))


def validate_report_datetime(label: str, value: datetime) -> None:
    if not isinstance(value, datetime):
        raise ValueError(f"{label} must be a datetime")
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise ValueError(f"{label} must be timezone-aware")
    expected_offset = NOVOSIBIRSK_TZ.utcoffset(value.replace(tzinfo=None))
    if value.utcoffset() != expected_offset:
        raise ValueError(f"{label} must use Asia/Novosibirsk timezone")


def _validate_sampled_at(sampled_at: datetime) -> None:
    validate_report_datetime("report window sampled_at", sampled_at)


def _validate_profile_filter(profile_key: str | None) -> None:
    if profile_key is None:
        return
    if not isinstance(profile_key, str) or not profile_key.strip():
        raise ValueError("report window profile key must be a string")
    if profile_key not in PROFILE_KEYS:
        expected = ", ".join(PROFILE_KEYS)
        raise ValueError(f"report window profile key must be one of {expected}")
