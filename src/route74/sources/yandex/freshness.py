from __future__ import annotations

from route74.sources.yandex.models import YandexLiveForecast, YandexVehicle

DEFAULT_FRESH_VEHICLE_MAX_AGE_SECONDS = 180


def effective_vehicle_age_seconds(
    vehicle_age_seconds: int | None,
    *,
    snapshot_age_seconds: int = 0,
) -> int | None:
    snapshot_age_seconds = _valid_age_seconds(snapshot_age_seconds) or 0
    if vehicle_age_seconds is None:
        return snapshot_age_seconds if snapshot_age_seconds > 0 else None
    age_seconds = _valid_age_seconds(vehicle_age_seconds)
    if age_seconds is None:
        return None
    return age_seconds + snapshot_age_seconds


def effective_forecast_age_seconds(forecast: YandexLiveForecast) -> int | None:
    if forecast.newest_age_seconds is not None:
        return _valid_age_seconds(forecast.newest_age_seconds)
    ages = tuple(
        age
        for vehicle in forecast.vehicles
        if vehicle.age_seconds is not None
        for age in (_valid_age_seconds(vehicle.age_seconds),)
        if age is not None
    )
    return min(ages) if ages else None


def is_fresh_age(
    age_seconds: int | None,
    *,
    max_age_seconds: int | None = DEFAULT_FRESH_VEHICLE_MAX_AGE_SECONDS,
) -> bool:
    if age_seconds is None or max_age_seconds is None:
        return True
    if _valid_age_seconds(age_seconds) is None or _valid_age_seconds(max_age_seconds) is None:
        return False
    return age_seconds <= max_age_seconds


def forecast_is_fresh(
    forecast: YandexLiveForecast,
    *,
    max_age_seconds: int | None = DEFAULT_FRESH_VEHICLE_MAX_AGE_SECONDS,
) -> bool:
    if _has_invalid_forecast_age(forecast):
        return False
    return is_fresh_age(effective_forecast_age_seconds(forecast), max_age_seconds=max_age_seconds)


def vehicle_is_fresh(
    vehicle: YandexVehicle,
    *,
    max_age_seconds: int | None = DEFAULT_FRESH_VEHICLE_MAX_AGE_SECONDS,
) -> bool:
    return is_fresh_age(vehicle.age_seconds, max_age_seconds=max_age_seconds)


def _valid_age_seconds(value: int) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _has_invalid_forecast_age(forecast: YandexLiveForecast) -> bool:
    if forecast.newest_age_seconds is not None:
        return _valid_age_seconds(forecast.newest_age_seconds) is None
    return any(
        vehicle.age_seconds is not None and _valid_age_seconds(vehicle.age_seconds) is None
        for vehicle in forecast.vehicles
    )
