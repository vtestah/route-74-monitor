from __future__ import annotations

from route74.sources.yandex.models import (
    YandexLiveForecast,
    YandexSourceMethod,
    YandexSourceMode,
    YandexSourceStatus,
)

FALLBACK_PRIORITY = {
    YandexSourceStatus.COORDINATES_ONLY: 0,
    YandexSourceStatus.SCHEDULE_ONLY: 1,
    YandexSourceStatus.FREQUENCY_ONLY: 1,
    YandexSourceStatus.NO_TARGET: 1,
}
FALLBACK_METHOD_PRIORITY = {
    YandexSourceMethod.STOP_INFO: 0,
    YandexSourceMethod.VEHICLE_PREDICTION: 1,
    YandexSourceMethod.BROWSER: 2,
    YandexSourceMethod.HTTP: 3,
}


def http_result_is_final(forecast: YandexLiveForecast, mode: YandexSourceMode) -> bool:
    return forecast.available or mode == YandexSourceMode.HTTP


def browser_result_is_final(forecast: YandexLiveForecast, mode: YandexSourceMode) -> bool:
    return (
        forecast.available or mode == YandexSourceMode.BROWSER or forecast.status == YandexSourceStatus.COORDINATES_ONLY
    )


def better_fallback(
    current: YandexLiveForecast | None,
    candidate: YandexLiveForecast,
) -> YandexLiveForecast | None:
    candidate_priority = FALLBACK_PRIORITY.get(candidate.status)
    if candidate_priority is None:
        return current
    if current is None:
        return candidate
    current_priority = FALLBACK_PRIORITY.get(current.status)
    if current_priority is None or candidate_priority < current_priority:
        return candidate
    if candidate_priority == current_priority and _method_priority(candidate) < _method_priority(current):
        return candidate
    return current


def _method_priority(forecast: YandexLiveForecast) -> int:
    return FALLBACK_METHOD_PRIORITY.get(forecast.source_method, len(FALLBACK_METHOD_PRIORITY))
