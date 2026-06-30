from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, time, timedelta
from typing import Any

from route74.domain.commute import CommuteProfile
from route74.domain.eta import EtaConfidence
from route74.models import require_local_datetime
from route74.sources.yandex.constants import expected_thread_ids, prediction_stop_ids
from route74.sources.yandex.models import (
    YandexLiveForecast,
    YandexSourceMethod,
    YandexSourceStatus,
    YandexVehicle,
)
from route74.sources.yandex.parser.coordinates import coord_pair

MAX_REASONABLE_PREDICTION_MINUTES = 180
THREAD_MATCH = "match"
THREAD_MISSING = "missing"
THREAD_MISMATCH = "mismatch"
THREAD_UNVALIDATED = "unvalidated"


@dataclass(frozen=True)
class _PredictionCandidate:
    vehicle: YandexVehicle
    thread_status: str


def parse_vehicle_prediction_payload(
    payload: dict[str, Any],
    *,
    profile: CommuteProfile,
    current_time: datetime,
) -> YandexLiveForecast:
    current_time = require_local_datetime(current_time, name="Yandex vehicle prediction current_time")
    predictions = _prediction_payloads(payload)
    if not predictions:
        return YandexLiveForecast.unavailable(
            status=YandexSourceStatus.EMPTY,
            source_method=YandexSourceMethod.VEHICLE_PREDICTION,
            reason="vehicle_prediction_empty",
        )

    targets = set(prediction_stop_ids(profile))
    expected_threads = set(expected_thread_ids(profile))
    candidates: list[_PredictionCandidate] = []
    for index, prediction in enumerate(predictions):
        thread_id = str(prediction.get("threadId") or "")
        target = _target_stop(prediction, targets, current_time)
        if target is None:
            continue
        minutes, _ = target
        lat, lng = _coordinates(prediction)
        candidates.append(
            _PredictionCandidate(
                vehicle=YandexVehicle(
                    vehicle_id=str(prediction.get("vehicleId") or f"vehicle-prediction-{index}"),
                    lat=lat,
                    lng=lng,
                    arrival_minutes=minutes,
                    thread_id=thread_id,
                ),
                thread_status=_thread_status(thread_id, expected_threads),
            )
        )

    if not candidates:
        reason = f"target_stop_not_found:{','.join(prediction_stop_ids(profile))}"
        coordinate_vehicles = _coordinate_vehicles(predictions)
        if coordinate_vehicles:
            return YandexLiveForecast(
                enabled=True,
                available=False,
                source_method=YandexSourceMethod.VEHICLE_PREDICTION,
                status=YandexSourceStatus.COORDINATES_ONLY,
                vehicles=coordinate_vehicles,
                vehicle_count=len(coordinate_vehicles),
                newest_age_seconds=0,
                confidence=EtaConfidence.LOW,
                fallback_reason=reason,
            )
        return YandexLiveForecast.unavailable(
            status=YandexSourceStatus.NO_TARGET,
            source_method=YandexSourceMethod.VEHICLE_PREDICTION,
            reason=reason,
        )

    selected, confidence, fallback_reason, raw_status = _select_candidates(candidates, expected_threads)
    if raw_status == "vehicle_prediction_thread_fallback":
        coordinate_vehicles = _coordinate_vehicles_from_candidates(selected)
        if coordinate_vehicles:
            return YandexLiveForecast(
                enabled=True,
                available=False,
                source_method=YandexSourceMethod.VEHICLE_PREDICTION,
                status=YandexSourceStatus.COORDINATES_ONLY,
                vehicles=coordinate_vehicles,
                vehicle_count=len(coordinate_vehicles),
                newest_age_seconds=0,
                confidence=confidence,
                fallback_reason=fallback_reason,
                raw_status=raw_status,
            )
        return YandexLiveForecast(
            enabled=True,
            available=False,
            source_method=YandexSourceMethod.VEHICLE_PREDICTION,
            status=YandexSourceStatus.NO_TARGET,
            confidence=confidence,
            fallback_reason=fallback_reason,
            raw_status=raw_status,
        )
    vehicles = tuple(candidate.vehicle for candidate in selected)
    arrivals = tuple(sorted({item.arrival_minutes for item in vehicles if item.arrival_minutes is not None}))
    return YandexLiveForecast(
        enabled=True,
        available=True,
        source_method=YandexSourceMethod.VEHICLE_PREDICTION,
        status=YandexSourceStatus.OK,
        arrival_minutes=arrivals,
        vehicles=vehicles,
        vehicle_count=len(vehicles),
        newest_age_seconds=0,
        confidence=confidence,
        fallback_reason=fallback_reason,
        raw_status=raw_status,
    )


def _thread_status(thread_id: str, expected_threads: set[str]) -> str:
    if not expected_threads:
        return THREAD_UNVALIDATED
    if not thread_id:
        return THREAD_MISSING
    if thread_id in expected_threads:
        return THREAD_MATCH
    return THREAD_MISMATCH


def _select_candidates(
    candidates: list[_PredictionCandidate],
    expected_threads: set[str],
) -> tuple[tuple[_PredictionCandidate, ...], EtaConfidence, str, str]:
    matched = tuple(candidate for candidate in candidates if candidate.thread_status == THREAD_MATCH)
    if matched:
        return matched, EtaConfidence.HIGH, "vehicle_prediction", "vehicle_prediction"
    if not expected_threads:
        return (
            tuple(candidates),
            EtaConfidence.HIGH,
            "vehicle_prediction",
            "vehicle_prediction",
        )
    reason = _thread_fallback_reason(candidates, expected_threads)
    return (
        tuple(candidates),
        EtaConfidence.LOW,
        reason,
        "vehicle_prediction_thread_fallback",
    )


def _thread_fallback_reason(candidates: list[_PredictionCandidate], expected_threads: set[str]) -> str:
    statuses = {candidate.thread_status for candidate in candidates}
    detail = "missing" if statuses == {THREAD_MISSING} else "not_found"
    expected = ",".join(sorted(expected_threads))
    return f"vehicle_prediction_thread_fallback:{detail}:{expected}"


def _coordinate_vehicles(
    predictions: list[dict[str, Any]],
) -> tuple[YandexVehicle, ...]:
    vehicles: list[YandexVehicle] = []
    for index, prediction in enumerate(predictions):
        lat, lng = _coordinates(prediction)
        if lat is None or lng is None:
            continue
        vehicles.append(
            YandexVehicle(
                vehicle_id=str(prediction.get("vehicleId") or f"vehicle-prediction-{index}"),
                lat=lat,
                lng=lng,
                age_seconds=0,
                thread_id=str(prediction.get("threadId") or ""),
            )
        )
    return tuple(vehicles)


def _coordinate_vehicles_from_candidates(
    candidates: tuple[_PredictionCandidate, ...],
) -> tuple[YandexVehicle, ...]:
    return tuple(
        replace(candidate.vehicle, arrival_minutes=None, age_seconds=0)
        for candidate in candidates
        if candidate.vehicle.lat is not None and candidate.vehicle.lng is not None
    )


def _prediction_payloads(payload: dict[str, Any]) -> list[dict[str, Any]]:
    raw_predictions = payload.get("predictions")
    if isinstance(raw_predictions, list):
        return [item for item in raw_predictions if isinstance(item, dict)]
    data = payload.get("data")
    if isinstance(data, dict):
        data_predictions = data.get("predictions")
        if isinstance(data_predictions, list):
            return [item for item in data_predictions if isinstance(item, dict)]
        if isinstance(data.get("stops"), list):
            return [data]
    if isinstance(payload.get("stops"), list):
        return [payload]
    return []


def _target_stop(
    prediction: dict[str, Any],
    targets: set[str],
    current_time: datetime,
) -> tuple[int, str] | None:
    stops = prediction.get("stops")
    if not isinstance(stops, list):
        return None
    candidates: list[tuple[int, str]] = []
    for item in stops:
        if not isinstance(item, dict):
            continue
        stop_id = str(item.get("stopId") or "")
        if stop_id not in targets:
            continue
        minutes = _arrival_minutes(item.get("arrivalEstimation"), current_time)
        if minutes is not None:
            candidates.append((minutes, stop_id))
    return min(candidates, key=lambda item: item[0]) if candidates else None


def _arrival_minutes(value: Any, current_time: datetime) -> int | None:
    if not isinstance(value, str):
        return None
    try:
        hour, minute = [int(part) for part in value.split(":", maxsplit=1)]
    except ValueError:
        return None
    try:
        arrival = datetime.combine(current_time.date(), time(hour, minute), tzinfo=current_time.tzinfo)
    except ValueError:
        return None
    minutes = round((arrival - current_time).total_seconds() / 60)
    if minutes < 0:
        minutes = round((arrival + timedelta(days=1) - current_time).total_seconds() / 60)
    if 0 <= minutes <= MAX_REASONABLE_PREDICTION_MINUTES:
        return minutes
    return None


def _coordinates(prediction: dict[str, Any]) -> tuple[float | None, float | None]:
    return coord_pair(prediction.get("coordinates"))
