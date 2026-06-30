from __future__ import annotations

from typing import Protocol

from route74.diagnostics import sanitize_diagnostic_text
from route74.domain.prediction_sources import (
    SOURCE_CORRECTED_LIVE,
    SOURCE_HISTORY_HEADWAY,
    SOURCE_TARGET_STOP_LIVE,
    SOURCE_VEHICLE_PROGRESS,
)


class RuntimeCalibrationGroupView(Protocol):
    key: str
    total: int
    evaluated: int
    misses: int
    p80_early_minutes: int | None
    p50_extra_wait_minutes: int | None
    suggested_buffer_minutes: int
    status: str

    @property
    def miss_rate_percent(self) -> int: ...


class RuntimeCalibrationView(Protocol):
    by_profile_source: tuple[RuntimeCalibrationGroupView, ...]


def format_runtime_source_calibration_line(calibration: RuntimeCalibrationView | None, profile_key: str) -> str:
    if calibration is None:
        return ""
    group = profile_source_group(calibration.by_profile_source, profile_key)
    if group is None:
        return ""
    _profile, source_key = profile_source_key(group.key)
    source = runtime_source_text(source_key)
    if group.status == "late_risk":
        return (
            f"🔎 Источник риска: {source} · "
            f"промахи {group.misses}/{group.evaluated} ({group.miss_rate_percent}%) · "
            f"p80 раннего прихода {_minutes(group.p80_early_minutes)}"
        )
    if group.status == "extra_wait":
        return f"🔎 Источник ожидания: {source} · p50 лишнего ожидания {_minutes(group.p50_extra_wait_minutes)}"
    if group.status == "insufficient":
        return f"🔎 Источник фактов: {source} · нужно больше проверок ({group.evaluated}/{group.total})"
    if group.status == "balanced":
        return f"🔎 Источник фактов: {source} · выглядит ровно ({group.evaluated}/{group.total})"
    return ""


def profile_source_group(
    groups: tuple[RuntimeCalibrationGroupView, ...],
    profile_key: str,
) -> RuntimeCalibrationGroupView | None:
    profile_groups = tuple(group for group in groups if profile_source_key(getattr(group, "key", ""))[0] == profile_key)
    if not profile_groups:
        return None
    return max(profile_groups, key=source_calibration_priority)


def profile_source_key(value: object) -> tuple[str, str]:
    text = str(value)
    if "/" not in text:
        return text, ""
    profile, source = text.split("/", 1)
    return profile, source


def source_calibration_priority(group: RuntimeCalibrationGroupView) -> tuple[int, int, int, int]:
    status_rank = {
        "late_risk": 4,
        "extra_wait": 3,
        "insufficient": 2,
        "balanced": 1,
    }.get(str(getattr(group, "status", "")), 0)
    return (
        status_rank,
        int(getattr(group, "suggested_buffer_minutes", 0) or 0),
        int(getattr(group, "miss_rate_percent", 0) or 0),
        int(getattr(group, "evaluated", 0) or 0),
    )


def runtime_source_text(value: str) -> str:
    return {
        SOURCE_CORRECTED_LIVE: "Яндекс+поправка",
        SOURCE_TARGET_STOP_LIVE: "Яндекс live",
        SOURCE_VEHICLE_PROGRESS: "координата",
        SOURCE_HISTORY_HEADWAY: "история Яндекса",
    }.get(value, sanitize_diagnostic_text(value, fallback="источник неизвестен", limit=40))


def runtime_prediction_source_text(value: str) -> str:
    return runtime_source_text(value)


def _minutes(value: int | None) -> str:
    if value is None:
        return "нет"
    return f"{value} мин"
