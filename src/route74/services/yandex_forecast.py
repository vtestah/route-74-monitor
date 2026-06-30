from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from typing import Protocol

from route74.domain.commute import CommuteProfile
from route74.sources.yandex.constants import max_raw_eta_minutes
from route74.sources.yandex.models import (
    YandexLiveForecast,
    YandexSourceStatus,
    YandexVehicle,
)

MAX_SOURCE_EXCEPTION_TYPE_LENGTH = 80


class YandexSource(Protocol):
    def get_forecast(
        self,
        profile: CommuteProfile,
        current_time: datetime,
    ) -> YandexLiveForecast: ...


def build_yandex_forecast(
    source: YandexSource | None,
    profile: CommuteProfile,
    current_time: datetime,
) -> YandexLiveForecast:
    if source is None:
        return YandexLiveForecast.disabled()
    try:
        return normalize_yandex_forecast(source.get_forecast(profile, current_time), profile)
    except Exception as exc:
        return YandexLiveForecast.unavailable(
            status=YandexSourceStatus.UNAVAILABLE,
            reason="source_exception",
            diagnostics=(_source_exception_diagnostic(exc),),
        )


def normalize_yandex_forecast(
    forecast: YandexLiveForecast,
    profile: CommuteProfile | None = None,
) -> YandexLiveForecast:
    max_minutes = max_raw_eta_minutes(profile)
    arrivals = _valid_arrivals(forecast.arrival_minutes, max_minutes)
    vehicles = _valid_vehicle_arrivals(forecast.vehicles, max_minutes)
    reason = forecast.fallback_reason

    if arrivals != forecast.arrival_minutes or vehicles != forecast.vehicles:
        reason = _append_reason(reason, "invalid_eta_filtered")
        forecast = replace(
            forecast,
            arrival_minutes=arrivals,
            vehicles=vehicles,
            fallback_reason=reason,
        )

    if forecast.available and not forecast.arrival_minutes:
        return replace(
            forecast,
            available=False,
            status=_status_without_arrivals(forecast),
            fallback_reason=_append_reason(forecast.fallback_reason, "available_without_eta"),
        )
    return forecast


def _valid_arrivals(arrival_minutes: tuple[int, ...], max_minutes: int) -> tuple[int, ...]:
    return tuple(sorted({minutes for minutes in arrival_minutes if _valid_eta_minutes(minutes, max_minutes)}))


def _valid_vehicle_arrivals(vehicles: tuple[YandexVehicle, ...], max_minutes: int) -> tuple[YandexVehicle, ...]:
    return tuple(
        replace(vehicle, arrival_minutes=None)
        if vehicle.arrival_minutes is not None and not _valid_eta_minutes(vehicle.arrival_minutes, max_minutes)
        else vehicle
        for vehicle in vehicles
    )


def _valid_eta_minutes(value: object, max_minutes: int) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= max_minutes


def _status_without_arrivals(forecast: YandexLiveForecast) -> YandexSourceStatus:
    if forecast.vehicles:
        return YandexSourceStatus.COORDINATES_ONLY
    return YandexSourceStatus.EMPTY


def _append_reason(current: str, reason: str) -> str:
    if not current:
        return reason
    if reason in current.split("; "):
        return current
    return f"{current}; {reason}"


def _source_exception_diagnostic(error: Exception) -> str:
    type_name = "".join(
        character
        for character in type(error).__name__
        if character.isascii() and (character.isalnum() or character == "_")
    )
    type_name = type_name[:MAX_SOURCE_EXCEPTION_TYPE_LENGTH] or "unknown"
    return f"source_exception:{type_name}"
