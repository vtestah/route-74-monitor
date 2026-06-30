from __future__ import annotations

import json
import math
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from statistics import median

from route74.domain.eta import EtaFactorKind
from route74.domain.runtime_sources import (
    BOT_EVENT_KINDS,
    BOT_EVENT_USER_REPLY,
    RUNTIME_SOURCE_WEB_APP,
)
from route74.storage.models import percent

BOT_RUNTIME_SOURCE = RUNTIME_SOURCE_WEB_APP
GUARDRAIL_UNAVAILABLE_FACTOR_KIND = EtaFactorKind.GUARDRAIL_UNAVAILABLE.value


@dataclass(frozen=True)
class BotRuntimePrediction:
    id: int
    sampled_at: datetime
    profile_key: str
    report_window_key: str
    source: str
    source_method: str
    predicted_minutes: int
    predicted_arrival_at: datetime | None
    confidence: str
    urgency: str
    selected_departure_source: str
    leave_in_minutes: int | None
    target_wait_minutes: int | None
    history_scope: str
    history_report_window_key: str
    history_sample_count: int | None
    history_bucket_minutes: int | None
    history_percentile: int | None
    yandex_status: str
    eta_factors: tuple[dict[str, object], ...]
    warning: str
    actual_minutes: int | None
    error_minutes: int | None
    evaluated_at: datetime | None
    event_kind: str


@dataclass(frozen=True)
class BotRuntimePredictionQualityGroup:
    key: str
    total: int
    evaluated: int
    pending: int
    misses: int
    guardrail_unavailable: int
    average_error_minutes: int | None
    p50_abs_error_minutes: int | None
    latest_sampled_at: datetime | None
    latest_evaluated_at: datetime | None
    oldest_pending_sampled_at: datetime | None

    @property
    def evaluated_percent(self) -> int:
        return percent(self.evaluated, self.total)

    @property
    def pending_percent(self) -> int:
        return percent(self.pending, self.total)

    @property
    def miss_rate_percent(self) -> int:
        return percent(self.misses, self.evaluated)

    @property
    def guardrail_unavailable_percent(self) -> int:
        return percent(self.guardrail_unavailable, self.total)


@dataclass(frozen=True)
class BotRuntimePredictionQuality:
    hours: int
    total: int
    evaluated: int
    pending: int
    misses: int
    guardrail_unavailable: int
    average_error_minutes: int | None
    p50_abs_error_minutes: int | None
    latest_sampled_at: datetime | None
    latest_evaluated_at: datetime | None
    oldest_pending_sampled_at: datetime | None
    by_profile: tuple[BotRuntimePredictionQualityGroup, ...]
    by_source: tuple[BotRuntimePredictionQualityGroup, ...]
    by_profile_source: tuple[BotRuntimePredictionQualityGroup, ...]
    by_event_kind: tuple[BotRuntimePredictionQualityGroup, ...]

    @property
    def evaluated_percent(self) -> int:
        return percent(self.evaluated, self.total)

    @property
    def pending_percent(self) -> int:
        return percent(self.pending, self.total)

    @property
    def miss_rate_percent(self) -> int:
        return percent(self.misses, self.evaluated)

    @property
    def guardrail_unavailable_percent(self) -> int:
        return percent(self.guardrail_unavailable, self.total)


@dataclass(frozen=True)
class BotRuntimeCalibrationGroup:
    key: str
    total: int
    evaluated: int
    misses: int
    p80_early_minutes: int | None
    p50_extra_wait_minutes: int | None
    suggested_buffer_minutes: int
    status: str
    action: str

    @property
    def evaluated_percent(self) -> int:
        return percent(self.evaluated, self.total)

    @property
    def miss_rate_percent(self) -> int:
        return percent(self.misses, self.evaluated)


@dataclass(frozen=True)
class BotRuntimeCalibration:
    hours: int
    total: int
    evaluated: int
    misses: int
    p80_early_minutes: int | None
    p50_extra_wait_minutes: int | None
    suggested_buffer_minutes: int
    status: str
    action: str
    by_profile: tuple[BotRuntimeCalibrationGroup, ...]
    by_source: tuple[BotRuntimeCalibrationGroup, ...]
    by_profile_source: tuple[BotRuntimeCalibrationGroup, ...]

    @property
    def evaluated_percent(self) -> int:
        return percent(self.evaluated, self.total)

    @property
    def miss_rate_percent(self) -> int:
        return percent(self.misses, self.evaluated)


def load_recent_bot_runtime_predictions(
    connection: sqlite3.Connection,
    *,
    current_time: datetime,
    hours: int = 24,
    limit: int = 8,
    profile_key: str | None = None,
    event_kind: str | None = None,
) -> tuple[BotRuntimePrediction, ...]:
    _positive_int("hours", hours)
    _positive_int("limit", limit)
    _validate_event_kind(event_kind)
    predictions = _load_bot_runtime_predictions(
        connection,
        current_time=current_time,
        hours=hours,
        limit=None if event_kind is not None else limit,
        profile_key=profile_key,
    )
    if event_kind is not None:
        return _filter_event_kind(predictions, event_kind)[:limit]
    return predictions


def summarize_bot_runtime_predictions(
    connection: sqlite3.Connection,
    *,
    current_time: datetime,
    hours: int = 24,
    profile_key: str | None = None,
    event_kind: str | None = None,
) -> BotRuntimePredictionQuality:
    _positive_int("hours", hours)
    _validate_event_kind(event_kind)
    predictions = _load_bot_runtime_predictions(
        connection,
        current_time=current_time,
        hours=hours,
        profile_key=profile_key,
    )
    predictions = _filter_event_kind(predictions, event_kind)
    return BotRuntimePredictionQuality(
        hours=hours,
        total=len(predictions),
        evaluated=_evaluated_count(predictions),
        pending=_pending_count(predictions),
        misses=_miss_count(predictions),
        guardrail_unavailable=_guardrail_unavailable_count(predictions),
        average_error_minutes=_average_error_minutes(predictions),
        p50_abs_error_minutes=_p50_abs_error_minutes(predictions),
        latest_sampled_at=predictions[0].sampled_at if predictions else None,
        latest_evaluated_at=_latest_evaluated_at(predictions),
        oldest_pending_sampled_at=_oldest_pending_sampled_at(predictions),
        by_profile=_quality_groups(predictions, lambda item: item.profile_key),
        by_source=_quality_groups(predictions, lambda item: item.source),
        by_profile_source=_quality_groups(predictions, lambda item: f"{item.profile_key}/{item.source}"),
        by_event_kind=_quality_groups(predictions, lambda item: item.event_kind),
    )


def summarize_bot_runtime_calibration(
    connection: sqlite3.Connection,
    *,
    current_time: datetime,
    hours: int = 24,
    min_evaluated: int = 3,
    profile_key: str | None = None,
    event_kind: str | None = None,
) -> BotRuntimeCalibration:
    _positive_int("hours", hours)
    _positive_int("min_evaluated", min_evaluated)
    _validate_event_kind(event_kind)
    predictions = _load_bot_runtime_predictions(
        connection,
        current_time=current_time,
        hours=hours,
        profile_key=profile_key,
    )
    predictions = _filter_event_kind(predictions, event_kind)
    summary = _calibration_group("all", predictions, min_evaluated=min_evaluated)
    return BotRuntimeCalibration(
        hours=hours,
        total=summary.total,
        evaluated=summary.evaluated,
        misses=summary.misses,
        p80_early_minutes=summary.p80_early_minutes,
        p50_extra_wait_minutes=summary.p50_extra_wait_minutes,
        suggested_buffer_minutes=summary.suggested_buffer_minutes,
        status=summary.status,
        action=summary.action,
        by_profile=_calibration_groups(predictions, lambda item: item.profile_key, min_evaluated=min_evaluated),
        by_source=_calibration_groups(predictions, lambda item: item.source, min_evaluated=min_evaluated),
        by_profile_source=_calibration_groups(
            predictions,
            lambda item: f"{item.profile_key}/{item.source}",
            min_evaluated=min_evaluated,
        ),
    )


def _load_bot_runtime_predictions(
    connection: sqlite3.Connection,
    *,
    current_time: datetime,
    hours: int,
    limit: int | None = None,
    profile_key: str | None = None,
) -> tuple[BotRuntimePrediction, ...]:
    since = (current_time - timedelta(hours=hours)).isoformat()
    limit_sql = "LIMIT ?" if limit is not None else ""
    filters = [
        "prediction_events.runtime_source = ?",
        "prediction_events.sampled_at >= ?",
        "prediction_events.sampled_at <= ?",
    ]
    params: list[object] = [BOT_RUNTIME_SOURCE, since, current_time.isoformat()]
    if profile_key is not None:
        filters.append("prediction_events.profile_key = ?")
        params.append(profile_key)
    if limit is not None:
        params.append(limit)
    rows = connection.execute(
        f"""
        SELECT
            prediction_events.id,
            prediction_events.sampled_at,
            prediction_events.profile_key,
            prediction_events.report_window_key,
            prediction_events.source,
            prediction_events.source_method,
            prediction_events.predicted_minutes,
            prediction_events.predicted_arrival_at,
            prediction_events.confidence,
            prediction_events.raw_json,
            prediction_evaluations.actual_minutes,
            prediction_evaluations.error_minutes,
            prediction_evaluations.evaluated_at
        FROM prediction_events
        LEFT JOIN prediction_evaluations
          ON prediction_evaluations.prediction_event_id = prediction_events.id
        WHERE {" AND ".join(filters)}
        ORDER BY prediction_events.sampled_at DESC, prediction_events.id DESC
        {limit_sql}
        """,
        tuple(params),
    ).fetchall()
    return tuple(_runtime_prediction_from_row(row) for row in rows)


def _validate_event_kind(event_kind: str | None) -> None:
    if event_kind is not None and event_kind not in BOT_EVENT_KINDS:
        raise ValueError("bot runtime event_kind is unknown")


def _filter_event_kind(
    predictions: tuple[BotRuntimePrediction, ...],
    event_kind: str | None,
) -> tuple[BotRuntimePrediction, ...]:
    if event_kind is None:
        return predictions
    return tuple(item for item in predictions if item.event_kind == event_kind)


def _quality_groups(
    predictions: tuple[BotRuntimePrediction, ...],
    key_fn: Callable[[BotRuntimePrediction], str],
) -> tuple[BotRuntimePredictionQualityGroup, ...]:
    grouped: dict[str, list[BotRuntimePrediction]] = {}
    for item in predictions:
        key = key_fn(item) or "-"
        grouped.setdefault(key, []).append(item)
    return tuple(
        _quality_group(key, tuple(items))
        for key, items in sorted(grouped.items(), key=lambda entry: (-len(entry[1]), entry[0]))
    )


def _quality_group(key: str, predictions: tuple[BotRuntimePrediction, ...]) -> BotRuntimePredictionQualityGroup:
    return BotRuntimePredictionQualityGroup(
        key=key,
        total=len(predictions),
        evaluated=_evaluated_count(predictions),
        pending=_pending_count(predictions),
        misses=_miss_count(predictions),
        guardrail_unavailable=_guardrail_unavailable_count(predictions),
        average_error_minutes=_average_error_minutes(predictions),
        p50_abs_error_minutes=_p50_abs_error_minutes(predictions),
        latest_sampled_at=predictions[0].sampled_at if predictions else None,
        latest_evaluated_at=_latest_evaluated_at(predictions),
        oldest_pending_sampled_at=_oldest_pending_sampled_at(predictions),
    )


def _calibration_groups(
    predictions: tuple[BotRuntimePrediction, ...],
    key_fn: Callable[[BotRuntimePrediction], str],
    *,
    min_evaluated: int,
) -> tuple[BotRuntimeCalibrationGroup, ...]:
    grouped: dict[str, list[BotRuntimePrediction]] = {}
    for item in predictions:
        key = key_fn(item) or "-"
        grouped.setdefault(key, []).append(item)
    return tuple(
        _calibration_group(key, tuple(items), min_evaluated=min_evaluated)
        for key, items in sorted(
            grouped.items(),
            key=lambda entry: (-_evaluated_count(tuple(entry[1])), entry[0]),
        )
    )


def _calibration_group(
    key: str,
    predictions: tuple[BotRuntimePrediction, ...],
    *,
    min_evaluated: int,
) -> BotRuntimeCalibrationGroup:
    evaluated_errors = tuple(item.error_minutes for item in predictions if item.error_minutes is not None)
    early_minutes = tuple(abs(value) for value in evaluated_errors if value < 0)
    extra_wait_minutes = tuple(value for value in evaluated_errors if value > 0)
    p80_early = _percentile_minutes(early_minutes, 80)
    p50_extra = round(median(extra_wait_minutes)) if extra_wait_minutes else None
    evaluated = len(evaluated_errors)
    misses = len(early_minutes)
    suggested_buffer = _suggested_buffer_minutes(
        evaluated=evaluated,
        misses=misses,
        miss_rate_percent=percent(misses, evaluated),
        p80_early_minutes=p80_early,
        min_evaluated=min_evaluated,
    )
    status = _calibration_status(
        evaluated=evaluated,
        misses=misses,
        miss_rate_percent=percent(misses, evaluated),
        p80_early_minutes=p80_early,
        p50_extra_wait_minutes=p50_extra,
        min_evaluated=min_evaluated,
    )
    return BotRuntimeCalibrationGroup(
        key=key,
        total=len(predictions),
        evaluated=evaluated,
        misses=misses,
        p80_early_minutes=p80_early,
        p50_extra_wait_minutes=p50_extra,
        suggested_buffer_minutes=suggested_buffer,
        status=status,
        action=_calibration_action(status, suggested_buffer),
    )


def _calibration_status(
    *,
    evaluated: int,
    misses: int,
    miss_rate_percent: int,
    p80_early_minutes: int | None,
    p50_extra_wait_minutes: int | None,
    min_evaluated: int,
) -> str:
    if evaluated < min_evaluated:
        return "insufficient"
    if misses and (miss_rate_percent >= 30 or (p80_early_minutes or 0) >= 2):
        return "late_risk"
    if misses == 0 and p50_extra_wait_minutes is not None and p50_extra_wait_minutes >= 5:
        return "extra_wait"
    return "balanced"


def _suggested_buffer_minutes(
    *,
    evaluated: int,
    misses: int,
    miss_rate_percent: int,
    p80_early_minutes: int | None,
    min_evaluated: int,
) -> int:
    if evaluated < min_evaluated or not misses:
        return 0
    if miss_rate_percent < 30 and (p80_early_minutes or 0) < 2:
        return 0
    return max(1, min(15, p80_early_minutes or 1))


def _calibration_action(status: str, suggested_buffer_minutes: int) -> str:
    if status == "insufficient":
        return "collect more evaluated bot replies"
    if status == "late_risk":
        return f"review +{suggested_buffer_minutes}m buffer for affected profile"
    if status == "extra_wait":
        return "watch extra waiting before lowering buffer"
    return "keep current buffers"


def _percentile_minutes(values: tuple[int, ...], percentile: int) -> int | None:
    if not values:
        return None
    sorted_values = sorted(values)
    index = max(
        0,
        min(len(sorted_values) - 1, math.ceil(percentile * len(sorted_values) / 100) - 1),
    )
    return sorted_values[index]


def _evaluated_count(predictions: tuple[BotRuntimePrediction, ...]) -> int:
    return sum(1 for item in predictions if item.error_minutes is not None)


def _pending_count(predictions: tuple[BotRuntimePrediction, ...]) -> int:
    return sum(1 for item in predictions if item.error_minutes is None)


def _miss_count(predictions: tuple[BotRuntimePrediction, ...]) -> int:
    return sum(1 for item in predictions if item.error_minutes is not None and item.error_minutes < 0)


def _guardrail_unavailable_count(predictions: tuple[BotRuntimePrediction, ...]) -> int:
    return sum(1 for item in predictions if _has_eta_factor(item, GUARDRAIL_UNAVAILABLE_FACTOR_KIND))


def _has_eta_factor(prediction: BotRuntimePrediction, kind: str) -> bool:
    return any(factor.get("kind") == kind for factor in prediction.eta_factors)


def _average_error_minutes(predictions: tuple[BotRuntimePrediction, ...]) -> int | None:
    values = tuple(item.error_minutes for item in predictions if item.error_minutes is not None)
    return round(sum(values) / len(values)) if values else None


def _p50_abs_error_minutes(predictions: tuple[BotRuntimePrediction, ...]) -> int | None:
    values = tuple(abs(item.error_minutes) for item in predictions if item.error_minutes is not None)
    return round(median(values)) if values else None


def _latest_evaluated_at(
    predictions: tuple[BotRuntimePrediction, ...],
) -> datetime | None:
    values = tuple(item.evaluated_at for item in predictions if item.evaluated_at is not None)
    return max(values) if values else None


def _oldest_pending_sampled_at(
    predictions: tuple[BotRuntimePrediction, ...],
) -> datetime | None:
    values = tuple(item.sampled_at for item in predictions if item.error_minutes is None)
    return min(values) if values else None


def _runtime_prediction_from_row(row: sqlite3.Row) -> BotRuntimePrediction:
    raw = _json_object(row["raw_json"])
    return BotRuntimePrediction(
        id=int(row["id"]),
        sampled_at=_required_datetime(row["sampled_at"], field_name="sampled_at"),
        profile_key=str(row["profile_key"]),
        report_window_key=str(row["report_window_key"]),
        source=str(row["source"]),
        source_method=str(row["source_method"]),
        predicted_minutes=int(row["predicted_minutes"]),
        predicted_arrival_at=_optional_datetime(row["predicted_arrival_at"]),
        confidence=str(row["confidence"]),
        urgency=_raw_text(raw, "urgency"),
        selected_departure_source=_raw_text(raw, "selected_departure_source"),
        leave_in_minutes=_raw_optional_int(raw, "leave_in_minutes"),
        target_wait_minutes=_raw_optional_int(raw, "target_wait_minutes"),
        history_scope=_raw_text(raw, "history_scope"),
        history_report_window_key=_raw_text(raw, "history_report_window_key"),
        history_sample_count=_raw_optional_int(raw, "history_sample_count"),
        history_bucket_minutes=_raw_optional_int(raw, "history_bucket_minutes"),
        history_percentile=_raw_optional_int(raw, "history_percentile"),
        yandex_status=_raw_text(raw, "yandex_status"),
        eta_factors=_raw_factor_payloads(raw),
        warning=_raw_text(raw, "warning"),
        actual_minutes=_optional_int(row["actual_minutes"]),
        error_minutes=_optional_int(row["error_minutes"]),
        evaluated_at=_optional_datetime(row["evaluated_at"]),
        event_kind=_raw_text(raw, "event_kind") or BOT_EVENT_USER_REPLY,
    )


def _raw_factor_payloads(raw: dict[str, object]) -> tuple[dict[str, object], ...]:
    value = raw.get("eta_factors")
    if not isinstance(value, list):
        return ()
    history_percentile = _raw_optional_int(raw, "history_percentile")
    return tuple(
        payload
        for item in value
        for payload in (_raw_factor_payload(item, history_percentile=history_percentile),)
        if payload
    )


def _raw_factor_payload(value: object, *, history_percentile: int | None) -> dict[str, object]:
    if not isinstance(value, dict):
        return {}
    kind = value.get("kind")
    if not isinstance(kind, str) or not kind:
        return {}
    percent = _optional_int(value.get("percent")) or 0
    if kind == EtaFactorKind.HISTORY_SAMPLE.value and percent <= 0 and _valid_history_percentile(history_percentile):
        percent = history_percentile
    return {
        "kind": kind,
        "minutes": _optional_int(value.get("minutes")) or 0,
        "sample_count": _optional_int(value.get("sample_count")) or 0,
        "percent": percent,
        "scope": _raw_text(value, "scope"),
    }


def _json_object(value: object) -> dict[str, object]:
    try:
        raw = json.loads(str(value))
    except json.JSONDecodeError:
        return {}
    return raw if isinstance(raw, dict) else {}


def _raw_text(raw: dict[str, object], key: str) -> str:
    value = raw.get(key)
    return value if isinstance(value, str) else ""


def _raw_optional_int(raw: dict[str, object], key: str) -> int | None:
    return _optional_int(raw.get(key))


def _optional_int(value: object) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed


def _valid_history_percentile(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= 100


def _required_datetime(value: object, *, field_name: str) -> datetime:
    parsed = _optional_datetime(value)
    if parsed is None:
        raise ValueError(f"bot runtime prediction {field_name} needs ISO datetime")
    return parsed


def _optional_datetime(value: object) -> datetime | None:
    if value is None:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def _positive_int(name: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value
