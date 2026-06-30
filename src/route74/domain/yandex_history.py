from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

DEFAULT_HISTORY_PERCENTILE = 80
MAX_HISTORY_REASON_LENGTH = 120


class YandexHistoryScope(StrEnum):
    REPORT_WINDOW = "report_window"
    PROFILE_TIME = "profile_time"


@dataclass(frozen=True)
class YandexHistoryPrediction:
    available: bool
    arrival_minutes: int | None
    sample_count: int
    bucket_minutes: int
    window_days: int
    percentile: int
    fallback_reason: str
    scope: YandexHistoryScope = YandexHistoryScope.PROFILE_TIME
    report_window_key: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.available, bool):
            raise ValueError("history prediction available must be a boolean")
        _validate_optional_non_negative_int("arrival_minutes", self.arrival_minutes)
        _validate_non_negative_int("sample_count", self.sample_count)
        _validate_non_negative_int("bucket_minutes", self.bucket_minutes)
        _validate_non_negative_int("window_days", self.window_days)
        _validate_percentile(self.percentile)
        _validate_reason_text(self.fallback_reason)
        _validate_scope(self.scope)
        _validate_report_window_key(self.report_window_key)
        _validate_scope_window_pair(self.scope, self.report_window_key)
        if self.available:
            _validate_available_prediction(self)
        else:
            _validate_unavailable_prediction(self)

    @classmethod
    def unavailable(
        cls,
        *,
        sample_count: int = 0,
        bucket_minutes: int = 0,
        window_days: int = 0,
        percentile: int = DEFAULT_HISTORY_PERCENTILE,
        reason: str = "history_unavailable",
        scope: YandexHistoryScope = YandexHistoryScope.PROFILE_TIME,
        report_window_key: str = "",
    ) -> YandexHistoryPrediction:
        return cls(
            available=False,
            arrival_minutes=None,
            sample_count=sample_count,
            bucket_minutes=bucket_minutes,
            window_days=window_days,
            percentile=percentile,
            fallback_reason=reason,
            scope=scope,
            report_window_key=report_window_key,
        )


def _validate_available_prediction(prediction: YandexHistoryPrediction) -> None:
    if prediction.arrival_minutes is None:
        raise ValueError("available history prediction needs arrival_minutes")
    if prediction.sample_count <= 0:
        raise ValueError("available history prediction needs positive sample_count")
    if prediction.bucket_minutes <= 0:
        raise ValueError("available history prediction needs positive bucket_minutes")
    if prediction.window_days <= 0:
        raise ValueError("available history prediction needs positive window_days")
    if prediction.fallback_reason:
        raise ValueError("available history prediction must not have fallback_reason")


def _validate_unavailable_prediction(prediction: YandexHistoryPrediction) -> None:
    if prediction.arrival_minutes is not None:
        raise ValueError("unavailable history prediction must not have arrival_minutes")
    if not prediction.fallback_reason:
        raise ValueError("unavailable history prediction needs fallback_reason")


def _validate_reason_text(value: object) -> None:
    if not isinstance(value, str):
        raise ValueError("history prediction fallback_reason must be a string")
    if len(value) > MAX_HISTORY_REASON_LENGTH or value != " ".join(value.split()):
        raise ValueError("history prediction fallback_reason must be compact text")


def _validate_non_negative_int(name: str, value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"history prediction {name} needs non-negative integer")


def _validate_optional_non_negative_int(name: str, value: int | None) -> None:
    if value is None:
        return
    _validate_non_negative_int(name, value)


def _validate_percentile(value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 100:
        raise ValueError("history prediction percentile must be an integer from 1 to 100")


def _validate_scope(value: object) -> None:
    if not isinstance(value, YandexHistoryScope):
        raise ValueError("history prediction scope needs YandexHistoryScope")


def _validate_report_window_key(value: object) -> None:
    if not isinstance(value, str):
        raise ValueError("history prediction report_window_key needs text")
    if not value:
        return
    if value != value.strip() or not value.isascii() or not all(char.isalnum() or char == "_" for char in value):
        raise ValueError("history prediction report_window_key needs plain key text")


def _validate_scope_window_pair(scope: YandexHistoryScope, report_window_key: str) -> None:
    if scope == YandexHistoryScope.REPORT_WINDOW and not report_window_key:
        raise ValueError("report-window history prediction needs report_window_key")
    if scope != YandexHistoryScope.REPORT_WINDOW and report_window_key:
        raise ValueError("unscoped history prediction must not have report_window_key")
