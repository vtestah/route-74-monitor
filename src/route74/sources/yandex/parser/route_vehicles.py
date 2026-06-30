from __future__ import annotations

from dataclasses import replace

from route74.domain.eta import EtaConfidence
from route74.sources.yandex.models import YandexLiveForecast, YandexSourceMethod, YandexSourceStatus, YandexVehicle


def route_vehicle_forecast(
    vehicles: tuple[YandexVehicle, ...],
    source_method: YandexSourceMethod,
    eta_limit_reason: str,
) -> YandexLiveForecast:
    ignored_eta = any(vehicle.arrival_minutes is not None for vehicle in vehicles)
    coordinates = tuple(replace(vehicle, arrival_minutes=None) for vehicle in vehicles)
    newest_age = newest_age_seconds(coordinates)
    return YandexLiveForecast(
        enabled=True,
        available=False,
        source_method=source_method,
        status=YandexSourceStatus.COORDINATES_ONLY,
        vehicles=coordinates,
        vehicle_count=len(coordinates),
        newest_age_seconds=newest_age,
        confidence=confidence_for_age(newest_age),
        fallback_reason=eta_limit_reason or ("route_vehicle_eta_ignored" if ignored_eta else "vehicles_without_eta"),
    )


def newest_age_seconds(vehicles: tuple[YandexVehicle, ...]) -> int | None:
    ages = [
        age
        for vehicle in vehicles
        for age in (_valid_age_seconds(vehicle.age_seconds),)
        if age is not None
    ]
    return min(ages) if ages else None


def confidence_for_age(age_seconds: int | None) -> EtaConfidence:
    age_seconds = _valid_age_seconds(age_seconds)
    if age_seconds is None:
        return EtaConfidence.LOW
    if age_seconds <= 45:
        return EtaConfidence.HIGH
    if age_seconds <= 120:
        return EtaConfidence.MEDIUM
    return EtaConfidence.LOW


def _valid_age_seconds(value: int | None) -> int | None:
    if value is None or isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value
