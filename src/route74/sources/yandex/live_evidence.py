from __future__ import annotations

from dataclasses import dataclass

from route74.sources.yandex.freshness import vehicle_is_fresh
from route74.sources.yandex.models import (
    YandexLiveForecast,
    YandexSourceMethod,
    YandexVehicle,
)

SHORT_LIVE_ETA_MAX_MINUTES = 8
STOP_INFO_NO_COORDINATE_BUFFER_MINUTES = 3
VEHICLE_PREDICTION_NO_COORDINATE_BUFFER_MINUTES = 2
MAX_LIVE_EVIDENCE_TEXT_LENGTH = 120


@dataclass(frozen=True)
class LiveEtaEvidenceAdjustment:
    safety_wait_minutes: int = 0
    scope: str = ""
    reason: str = ""

    def __post_init__(self) -> None:
        if (
            isinstance(self.safety_wait_minutes, bool)
            or not isinstance(self.safety_wait_minutes, int)
            or self.safety_wait_minutes < 0
        ):
            raise ValueError("live ETA evidence safety wait needs non-negative integer")
        _validate_scope(self.scope)
        _validate_reason(self.reason)
        if self.applied and (not self.scope or not self.reason):
            raise ValueError("applied live ETA evidence needs scope and reason")
        if not self.applied and (self.scope or self.reason):
            raise ValueError("inactive live ETA evidence must not have scope or reason")

    @property
    def applied(self) -> bool:
        return self.safety_wait_minutes > 0


def live_eta_evidence_adjustment(
    forecast: YandexLiveForecast,
    *,
    arrival_minutes: int,
) -> LiveEtaEvidenceAdjustment:
    if not _is_short_arrival_minutes(arrival_minutes):
        return LiveEtaEvidenceAdjustment()

    matching = _matching_eta_vehicles(forecast.vehicles, arrival_minutes)
    if _has_fresh_coordinates(matching):
        return LiveEtaEvidenceAdjustment()

    if forecast.source_method == YandexSourceMethod.STOP_INFO:
        return LiveEtaEvidenceAdjustment(
            safety_wait_minutes=STOP_INFO_NO_COORDINATE_BUFFER_MINUTES,
            scope="live_eta_no_coordinates",
            reason="short_stop_info_eta_without_vehicle_coordinates",
        )

    if forecast.source_method == YandexSourceMethod.VEHICLE_PREDICTION:
        return LiveEtaEvidenceAdjustment(
            safety_wait_minutes=VEHICLE_PREDICTION_NO_COORDINATE_BUFFER_MINUTES,
            scope="live_eta_no_coordinates",
            reason="short_vehicle_prediction_eta_without_vehicle_coordinates",
        )

    return LiveEtaEvidenceAdjustment()


def _matching_eta_vehicles(
    vehicles: tuple[YandexVehicle, ...],
    arrival_minutes: int,
) -> tuple[YandexVehicle, ...]:
    matching = tuple(
        vehicle
        for vehicle in vehicles
        if vehicle.arrival_minutes is not None and abs(vehicle.arrival_minutes - arrival_minutes) <= 1
    )
    return matching or vehicles


def _has_fresh_coordinates(vehicles: tuple[YandexVehicle, ...]) -> bool:
    return any(
        vehicle.lat is not None and vehicle.lng is not None and vehicle_is_fresh(vehicle) for vehicle in vehicles
    )


def _is_short_arrival_minutes(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= SHORT_LIVE_ETA_MAX_MINUTES


def _validate_scope(value: object) -> None:
    if not isinstance(value, str):
        raise ValueError("live ETA evidence scope needs text")
    if value and (
        value != value.strip() or not value.isascii() or any(not (char.isalnum() or char == "_") for char in value)
    ):
        raise ValueError("live ETA evidence scope must be a plain key")


def _validate_reason(value: object) -> None:
    if not isinstance(value, str):
        raise ValueError("live ETA evidence reason needs text")
    if len(value) > MAX_LIVE_EVIDENCE_TEXT_LENGTH or value != " ".join(value.split()):
        raise ValueError("live ETA evidence reason must be compact single-line text")
