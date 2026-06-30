from __future__ import annotations

from route74.sources.yandex.constants import max_raw_eta_minutes
from route74.sources.yandex.freshness import DEFAULT_FRESH_VEHICLE_MAX_AGE_SECONDS, forecast_is_fresh
from route74.sources.yandex.models import YandexLiveForecast


TRUSTED_ETA_SOURCE_METHODS = ("vehicle_prediction", "stop_info")
UNTRUSTED_ETA_RAW_STATUSES = ("vehicle_prediction_thread_fallback",)
UNTRUSTED_ETA_REASON_PREFIXES = ("vehicle_prediction_thread_fallback:",)


def is_trusted_eta_source(source_method: object) -> bool:
    return str(source_method) in TRUSTED_ETA_SOURCE_METHODS


def is_trusted_eta_observation(
    source_method: object,
    *,
    fallback_reason: object = "",
    raw_status: object = "",
) -> bool:
    if not is_trusted_eta_source(source_method):
        return False
    reason = _diagnostic_text(fallback_reason)
    status = _diagnostic_text(raw_status)
    if _matches_untrusted_status(status):
        return False
    return not any(reason.startswith(prefix) for prefix in UNTRUSTED_ETA_REASON_PREFIXES)


def trusted_arrivals_for_forecast(forecast: YandexLiveForecast) -> tuple[int, ...]:
    if not forecast.available:
        return ()
    if not is_trusted_eta_observation(
        forecast.source_method.value,
        fallback_reason=forecast.fallback_reason,
        raw_status=forecast.raw_status,
    ):
        return ()
    return _valid_arrivals(forecast.arrival_minutes)


def forecast_has_trusted_fresh_eta(
    forecast: YandexLiveForecast,
    *,
    max_age_seconds: int | None = DEFAULT_FRESH_VEHICLE_MAX_AGE_SECONDS,
) -> bool:
    return (
        forecast.available
        and bool(forecast.arrival_minutes)
        and bool(trusted_arrivals_for_forecast(forecast))
        and forecast_is_fresh(forecast, max_age_seconds=max_age_seconds)
    )


def _valid_arrivals(arrival_minutes: tuple[int, ...]) -> tuple[int, ...]:
    max_minutes = max_raw_eta_minutes(None)
    return tuple(
        sorted({minutes for minutes in arrival_minutes if _valid_arrival(minutes, max_minutes)})
    )


def _valid_arrival(value: object, max_minutes: int) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= max_minutes


def _diagnostic_text(value: object) -> str:
    return str(value or "").strip()


def _matches_untrusted_status(status: str) -> bool:
    return any(status.startswith(raw_status) for raw_status in UNTRUSTED_ETA_RAW_STATUSES)
