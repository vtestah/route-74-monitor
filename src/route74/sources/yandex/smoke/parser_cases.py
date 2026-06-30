from __future__ import annotations

from datetime import datetime, timedelta

from route74.domain.eta import EtaConfidence
from route74.domain.profiles import EVENING
from route74.models import NOVOSIBIRSK_TZ
from route74.sources.yandex.models import (
    YandexSourceMethod,
    YandexSourceStatus,
    YandexVehicle,
)
from route74.sources.yandex.parser import parse_vehicles_payload
from route74.sources.yandex.parser.route_vehicles import (
    confidence_for_age,
    newest_age_seconds,
    route_vehicle_forecast,
)
from route74.sources.yandex.smoke.assertions import assert_equal
from route74.sources.yandex.vehicle_prediction import parse_vehicle_prediction_payload


def run_vehicle_parser_smoke(current_time: datetime) -> None:
    forecast = parse_vehicles_payload(
        {
            "data": {
                "vehicles": [
                    {
                        "id": "a",
                        "lat": 54.9,
                        "lng": 83.1,
                        "arrivalMinutes": 7,
                        "ageSeconds": 20,
                    },
                    {"id": "b", "lat": 54.8, "lng": 83.0, "eta": 15, "ageSeconds": 70},
                ]
            }
        },
        source_method=YandexSourceMethod.HTTP,
        current_time=current_time,
    )
    assert_equal(forecast.available, False)
    assert_equal(forecast.status, YandexSourceStatus.COORDINATES_ONLY)
    assert_equal(forecast.arrival_minutes, ())
    assert_equal(forecast.fallback_reason, "route_vehicle_eta_ignored")
    assert_equal(forecast.vehicle_count, 2)
    assert_equal(forecast.confidence, EtaConfidence.HIGH)

    coords = parse_vehicles_payload(
        {
            "data": {
                "vehicles": [
                    {
                        "id": "c",
                        "geometry": {"coordinates": [83.1, 54.9]},
                        "timestamp": int((current_time - timedelta(seconds=80)).timestamp()),
                    }
                ]
            }
        },
        source_method=YandexSourceMethod.BROWSER,
        current_time=current_time,
    )
    assert_equal(coords.available, False)
    assert_equal(coords.status, YandexSourceStatus.COORDINATES_ONLY)
    assert_equal(coords.vehicle_count, 1)
    assert_equal(coords.vehicles[0].lat, 54.9)
    assert_equal(coords.vehicles[0].lng, 83.1)
    assert_equal(coords.newest_age_seconds, 80)

    skewed = parse_vehicles_payload(
        {
            "data": {
                "vehicles": [
                    {
                        "id": "skewed-clock",
                        "geometry": {"coordinates": [83.1, 54.9]},
                        "timestamp": int((current_time + timedelta(seconds=30)).timestamp()),
                    }
                ]
            }
        },
        source_method=YandexSourceMethod.BROWSER,
        current_time=current_time,
    )
    assert_equal(skewed.newest_age_seconds, 0)

    future = parse_vehicles_payload(
        {
            "data": {
                "vehicles": [
                    {
                        "id": "bad-future-clock",
                        "geometry": {"coordinates": [83.1, 54.9]},
                        "timestamp": int((current_time + timedelta(minutes=10)).timestamp()),
                    }
                ]
            }
        },
        source_method=YandexSourceMethod.BROWSER,
        current_time=current_time,
    )
    assert_equal(future.newest_age_seconds, None)
    assert_equal(future.confidence, EtaConfidence.LOW)
    assert_equal(future.vehicles[0].age_seconds, None)

    _run_nested_vehicle_smoke(current_time)
    _run_route_vehicle_age_guard_smoke()
    empty = parse_vehicles_payload(
        {"data": {"vehicles": []}},
        source_method=YandexSourceMethod.HTTP,
        current_time=current_time,
    )
    assert_equal(empty.available, False)
    assert_equal(empty.status, YandexSourceStatus.EMPTY)

    bad = parse_vehicles_payload(
        {"csrfToken": "abc"},
        source_method=YandexSourceMethod.HTTP,
        current_time=current_time,
    )
    assert_equal(bad.available, False)
    assert_equal(bad.status, YandexSourceStatus.PARSE_ERROR)


def run_vehicle_prediction_smoke() -> None:
    current_time = datetime(2026, 6, 4, 20, 12, tzinfo=NOVOSIBIRSK_TZ)
    forecast = parse_vehicle_prediction_payload(
        {
            "data": {
                "predictions": [
                    {
                        "vehicleId": "1651901|route74",
                        "threadId": "2161326764",
                        "coordinates": [83.11582825444825, 54.94095686809654],
                        "stops": [
                            {"stopId": "stop__9982194", "arrivalEstimation": "20:34"},
                            {"stopId": "stop__9982094", "arrivalEstimation": "20:51"},
                        ],
                    }
                ]
            }
        },
        profile=EVENING,
        current_time=current_time,
    )
    assert_equal(forecast.available, True)
    assert_equal(forecast.source_method, YandexSourceMethod.VEHICLE_PREDICTION)
    assert_equal(forecast.arrival_minutes, (39,))
    assert_equal(forecast.newest_age_seconds, 0)
    assert_equal(forecast.confidence, EtaConfidence.HIGH)
    assert_equal(forecast.vehicles[0].lat, 54.94095686809654)
    assert_equal(forecast.vehicles[0].lng, 83.11582825444825)

    no_target = parse_vehicle_prediction_payload(
        {
            "data": {
                "threadId": "2161326764",
                "stops": [{"stopId": "stop__9982194", "arrivalEstimation": "20:34"}],
            }
        },
        profile=EVENING,
        current_time=current_time,
    )
    assert_equal(no_target.available, False)
    assert_equal(no_target.status, YandexSourceStatus.NO_TARGET)
    assert_equal("target_stop_not_found" in no_target.fallback_reason, True)

    invalid_target_time = parse_vehicle_prediction_payload(
        {
            "predictions": [
                {
                    "vehicleId": "invalid-target-time",
                    "threadId": "2161326764",
                    "stops": [{"stopId": "stop__9982094", "arrivalEstimation": "25:99"}],
                }
            ]
        },
        profile=EVENING,
        current_time=current_time,
    )
    assert_equal(invalid_target_time.available, False)
    assert_equal(invalid_target_time.status, YandexSourceStatus.NO_TARGET)
    assert_equal("target_stop_not_found" in invalid_target_time.fallback_reason, True)


def _run_nested_vehicle_smoke(current_time: datetime) -> None:
    nested = parse_vehicles_payload(
        {
            "data": {
                "vehicles": [
                    {
                        "features": [
                            {
                                "geometry": {
                                    "type": "LineString",
                                    "coordinates": [
                                        [83.110656, 54.840692],
                                        [83.110924, 54.840811],
                                    ],
                                },
                                "properties": {
                                    "TrajectorySegmentMetaData": {
                                        "duration": 28,
                                        "time": 1780641523,
                                    }
                                },
                            }
                        ],
                        "properties": {
                            "VehicleMetaData": {
                                "id": "1651901|nested",
                                "Transport": {
                                    "id": "1651901|nested",
                                    "threadId": "2161326764",
                                },
                            }
                        },
                    }
                ]
            }
        },
        source_method=YandexSourceMethod.BROWSER,
        current_time=current_time,
    )
    assert_equal(nested.status, YandexSourceStatus.COORDINATES_ONLY)
    assert_equal(nested.vehicles[0].vehicle_id, "1651901|nested")
    assert_equal(nested.vehicles[0].thread_id, "2161326764")
    assert_equal(nested.vehicles[0].lat, 54.840811)
    assert_equal(nested.vehicles[0].lng, 83.110924)
    assert_equal(nested.vehicles[0].arrival_minutes, None)


def _run_route_vehicle_age_guard_smoke() -> None:
    vehicles = (
        YandexVehicle("negative-age", lat=54.9, lng=83.1, age_seconds=-10),
        YandexVehicle("fresh-age", lat=54.8, lng=83.0, age_seconds=70),
    )
    assert_equal(newest_age_seconds(vehicles), 70)
    assert_equal(confidence_for_age(-1), EtaConfidence.LOW)
    assert_equal(confidence_for_age(True), EtaConfidence.LOW)
    forecast = route_vehicle_forecast((vehicles[0],), YandexSourceMethod.HTTP, "")
    assert_equal(forecast.newest_age_seconds, None)
    assert_equal(forecast.confidence, EtaConfidence.LOW)
