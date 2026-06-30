from __future__ import annotations

from route74.diagnostics import sanitize_diagnostic_text
from route74.domain.commute import DepartureSource
from route74.domain.commute_change import DepartureChange
from route74.domain.prediction_sources import (
    SOURCE_CORRECTED_LIVE,
    SOURCE_HISTORY_HEADWAY,
    SOURCE_TARGET_STOP_LIVE,
    SOURCE_VEHICLE_PROGRESS,
)
from route74.presenters.commute_lines import format_duration_minutes


SOURCE_LABELS = {
    DepartureSource.YANDEX.value: "Яндекс live",
    DepartureSource.YANDEX_CORRECTED.value: "Яндекс+поправка",
    DepartureSource.VEHICLE_PROGRESS.value: "координата",
    DepartureSource.YANDEX_HISTORY.value: "история Яндекса",
    DepartureSource.NONE.value: "нет ETA",
    SOURCE_TARGET_STOP_LIVE: "Яндекс live",
    SOURCE_CORRECTED_LIVE: "Яндекс+поправка",
    SOURCE_VEHICLE_PROGRESS: "координата",
    SOURCE_HISTORY_HEADWAY: "история Яндекса",
}


def format_departure_change_line(change: DepartureChange | None) -> str:
    details = format_departure_change_details(change)
    if not details:
        return ""
    return f"🔁 С прошлого ответа: {details}"


def format_departure_change_details(change: DepartureChange | None) -> str:
    if change is None:
        return ""
    details = tuple(part for part in (_arrival_change_text(change), _source_change_text(change)) if part)
    if not details:
        return ""
    return " · ".join(details)


def _arrival_change_text(change: DepartureChange) -> str:
    if change.current_arrival_at is None:
        if change.previous_arrival_at is None:
            return "ETA всё ещё нет"
        return f"ETA пропал · было {change.previous_arrival_at:%H:%M}"
    if change.previous_arrival_at is None:
        return f"ETA появился: {change.current_arrival_at:%H:%M}"
    shift = change.arrival_shift_minutes
    if shift is None:
        return ""
    if abs(shift) <= 1:
        return "время 74-го почти без изменений"
    if shift > 0:
        return f"74-й позже на {format_duration_minutes(shift)}"
    return f"74-й раньше на {format_duration_minutes(abs(shift))}"


def _source_change_text(change: DepartureChange) -> str:
    if not change.source_changed:
        return ""
    return f"источник {_source_text(change.previous_source)} -> {_source_text(change.current_source)}"


def _source_text(value: str) -> str:
    return SOURCE_LABELS.get(value, sanitize_diagnostic_text(value, fallback="источник неизвестен", limit=40))
