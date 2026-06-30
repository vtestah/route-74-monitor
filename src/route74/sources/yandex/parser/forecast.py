from __future__ import annotations

from datetime import datetime
from typing import Any

from route74.domain.commute import CommuteProfile
from route74.sources.yandex.models import (
    YandexLiveForecast,
    YandexSourceMethod,
    YandexSourceStatus,
    YandexVehicle,
)
from route74.sources.yandex.parser.containers import find_vehicles
from route74.sources.yandex.parser.direction import filter_expected_thread
from route74.sources.yandex.parser.eta_limits import filter_raw_eta_limit
from route74.sources.yandex.parser.route_vehicles import confidence_for_age, newest_age_seconds, route_vehicle_forecast
from route74.sources.yandex.parser.vehicle import parse_vehicle


def parse_vehicles_payload(
    payload: dict[str, Any],
    *,
    source_method: YandexSourceMethod,
    current_time: datetime,
    profile: CommuteProfile | None = None,
) -> YandexLiveForecast:
    vehicles_raw = find_vehicles(payload)
    if vehicles_raw is None:
        return YandexLiveForecast.unavailable(
            status=YandexSourceStatus.PARSE_ERROR,
            source_method=source_method,
            reason="vehicles_not_found",
        )
    if not vehicles_raw:
        return YandexLiveForecast(
            enabled=True,
            available=False,
            source_method=source_method,
            status=YandexSourceStatus.EMPTY,
            vehicle_count=0,
            fallback_reason="vehicles_empty",
        )

    parsed_vehicles = tuple(
        parse_vehicle(item, index, current_time)
        for index, item in enumerate(vehicles_raw)
        if isinstance(item, dict)
    )
    vehicles, direction_reason = filter_expected_thread(parsed_vehicles, profile)
    vehicles, eta_limit_reason = filter_raw_eta_limit(vehicles, profile)
    if not vehicles:
        coordinate_fallback = _vehicles_with_coordinates(parsed_vehicles)
        if source_method in {YandexSourceMethod.HTTP, YandexSourceMethod.BROWSER} and coordinate_fallback:
            return route_vehicle_forecast(
                coordinate_fallback,
                source_method,
                direction_reason or "direction_thread_not_found",
            )
        return YandexLiveForecast.unavailable(
            status=YandexSourceStatus.NO_TARGET,
            source_method=source_method,
            reason=direction_reason or "direction_thread_not_found",
        )
    if source_method in {YandexSourceMethod.HTTP, YandexSourceMethod.BROWSER}:
        return route_vehicle_forecast(vehicles, source_method, eta_limit_reason)
    return _forecast_from_vehicles(vehicles, source_method, eta_limit_reason)


def _vehicles_with_coordinates(vehicles: tuple[YandexVehicle, ...]) -> tuple[YandexVehicle, ...]:
    return tuple(vehicle for vehicle in vehicles if vehicle.lat is not None and vehicle.lng is not None)


def _forecast_from_vehicles(
    vehicles: tuple[YandexVehicle, ...],
    source_method: YandexSourceMethod,
    eta_limit_reason: str = "",
) -> YandexLiveForecast:
    arrivals = tuple(
        sorted(
            {
                vehicle.arrival_minutes
                for vehicle in vehicles
                if vehicle.arrival_minutes is not None and vehicle.arrival_minutes >= 0
            }
        )
    )
    newest_age = newest_age_seconds(vehicles)
    if arrivals:
        return YandexLiveForecast(
            enabled=True,
            available=True,
            source_method=source_method,
            status=YandexSourceStatus.OK,
            arrival_minutes=arrivals,
            vehicles=vehicles,
            vehicle_count=len(vehicles),
            newest_age_seconds=newest_age,
            confidence=confidence_for_age(newest_age),
        )
    return YandexLiveForecast(
        enabled=True,
        available=False,
        source_method=source_method,
        status=YandexSourceStatus.COORDINATES_ONLY,
        vehicles=vehicles,
        vehicle_count=len(vehicles),
        newest_age_seconds=newest_age,
        confidence=confidence_for_age(newest_age),
        fallback_reason=eta_limit_reason or "vehicles_without_eta",
    )
