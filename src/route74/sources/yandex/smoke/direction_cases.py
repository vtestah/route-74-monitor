from __future__ import annotations

from datetime import datetime

from route74.domain.profiles import EVENING, MORNING
from route74.sources.yandex.constants import route_map_url
from route74.sources.yandex.models import YandexSourceMethod, YandexSourceStatus
from route74.sources.yandex.parser import parse_vehicles_payload
from route74.sources.yandex.smoke.assertions import assert_equal
from route74.sources.yandex.vehicle_prediction import parse_vehicle_prediction_payload


def run_direction_smoke(current_time: datetime) -> None:
    _run_route_url_direction_smoke()
    _run_raw_vehicle_direction_smoke(current_time)
    _run_raw_eta_limit_smoke(current_time)
    _run_prediction_direction_smoke(current_time)


def _run_route_url_direction_smoke() -> None:
    morning_url = route_map_url(MORNING)
    evening_url = route_map_url(EVENING)
    assert_equal("threadId=2161326768" in morning_url, True)
    assert_equal("openedBy%5BstopId%5D=stop__9982194" in morning_url, True)
    assert_equal("ll=83.080433%2C54.869098" in morning_url, True)
    assert_equal("z=12" in morning_url, True)
    assert_equal("threadId=2161326764" in evening_url, True)
    assert_equal("openedBy%5BstopId%5D=stop__9982094" in evening_url, True)


def _run_raw_vehicle_direction_smoke(current_time: datetime) -> None:
    directed = parse_vehicles_payload(
        {
            "data": {
                "vehicles": [
                    _vehicle_with_thread("toward-color", "2161326768", 5),
                    _vehicle_with_thread("wrong-way", "2161326764", 7),
                ]
            }
        },
        source_method=YandexSourceMethod.BROWSER,
        current_time=current_time,
        profile=MORNING,
    )
    assert_equal(directed.available, False)
    assert_equal(directed.status, YandexSourceStatus.COORDINATES_ONLY)
    assert_equal(directed.arrival_minutes, ())
    assert_equal(directed.vehicle_count, 1)
    assert_equal(directed.vehicles[0].thread_id, "2161326768")
    assert_equal(directed.vehicles[0].arrival_minutes, None)
    assert_equal(directed.fallback_reason, "route_vehicle_eta_ignored")

    wrong_direction = parse_vehicles_payload(
        {"data": {"vehicles": [_vehicle_with_thread("wrong-way", "2161326764", 7)]}},
        source_method=YandexSourceMethod.BROWSER,
        current_time=current_time,
        profile=MORNING,
    )
    assert_equal(wrong_direction.available, False)
    assert_equal(wrong_direction.status, YandexSourceStatus.COORDINATES_ONLY)
    assert_equal(wrong_direction.fallback_reason, "direction_thread_not_found")
    assert_equal(wrong_direction.vehicle_count, 1)
    assert_equal(wrong_direction.vehicles[0].arrival_minutes, None)
    assert_equal(wrong_direction.vehicles[0].thread_id, "2161326764")

    missing_direction = parse_vehicles_payload(
        {"data": {"vehicles": [_vehicle_without_thread("unknown-thread", 7)]}},
        source_method=YandexSourceMethod.BROWSER,
        current_time=current_time,
        profile=MORNING,
    )
    assert_equal(missing_direction.available, False)
    assert_equal(missing_direction.status, YandexSourceStatus.COORDINATES_ONLY)
    assert_equal(missing_direction.fallback_reason, "direction_thread_missing")
    assert_equal(missing_direction.vehicle_count, 1)
    assert_equal(missing_direction.vehicles[0].arrival_minutes, None)
    assert_equal(missing_direction.vehicles[0].thread_id, "")


def _run_raw_eta_limit_smoke(current_time: datetime) -> None:
    forecast = parse_vehicles_payload(
        {
            "data": {
                "vehicles": [
                    _vehicle_with_thread("near", "2161326768", 8),
                    _vehicle_with_thread("loop", "2161326768", 74),
                ]
            }
        },
        source_method=YandexSourceMethod.BROWSER,
        current_time=current_time,
        profile=MORNING,
    )
    assert_equal(forecast.available, False)
    assert_equal(forecast.status, YandexSourceStatus.COORDINATES_ONLY)
    assert_equal(forecast.arrival_minutes, ())
    assert_equal(forecast.vehicles[0].arrival_minutes, None)
    assert_equal(forecast.vehicles[1].arrival_minutes, None)
    assert_equal(forecast.confidence.value, "low")
    assert_equal(forecast.fallback_reason, "raw_eta_over_limit:60")

    over_limit = parse_vehicles_payload(
        {"data": {"vehicles": [_vehicle_with_thread("loop", "2161326768", 74)]}},
        source_method=YandexSourceMethod.BROWSER,
        current_time=current_time,
        profile=MORNING,
    )
    assert_equal(over_limit.available, False)
    assert_equal(over_limit.status, YandexSourceStatus.COORDINATES_ONLY)
    assert_equal(over_limit.fallback_reason, "raw_eta_over_limit:60")


def _run_prediction_direction_smoke(current_time: datetime) -> None:
    current_time = datetime(2026, 6, 4, 20, 12, tzinfo=current_time.tzinfo)
    directed = parse_vehicle_prediction_payload(
        {
            "predictions": [
                _prediction("wrong-thread", "2161326768", "20:20"),
                _prediction("right-thread", "2161326764", "20:24"),
            ]
        },
        profile=EVENING,
        current_time=current_time,
    )
    assert_equal(directed.available, True)
    assert_equal(directed.arrival_minutes, (12,))
    assert_equal(directed.vehicles[0].vehicle_id, "right-thread")
    assert_equal(directed.vehicles[0].thread_id, "2161326764")

    wrong_thread = parse_vehicle_prediction_payload(
        {"predictions": [_prediction("wrong-thread", "2161326768", "20:20")]},
        profile=EVENING,
        current_time=current_time,
    )
    assert_equal(wrong_thread.available, False)
    assert_equal(wrong_thread.status, YandexSourceStatus.NO_TARGET)
    assert_equal(wrong_thread.arrival_minutes, ())
    assert_equal(wrong_thread.confidence.value, "low")
    assert_equal(wrong_thread.raw_status, "vehicle_prediction_thread_fallback")
    assert_equal(
        "vehicle_prediction_thread_fallback:not_found" in wrong_thread.fallback_reason,
        True,
    )

    wrong_thread_coordinates = parse_vehicle_prediction_payload(
        {"predictions": [_prediction_with_coordinates("wrong-thread-coordinates", "2161326768", "20:20")]},
        profile=EVENING,
        current_time=current_time,
    )
    assert_equal(wrong_thread_coordinates.available, False)
    assert_equal(wrong_thread_coordinates.status, YandexSourceStatus.COORDINATES_ONLY)
    assert_equal(wrong_thread_coordinates.arrival_minutes, ())
    assert_equal(wrong_thread_coordinates.vehicle_count, 1)
    assert_equal(wrong_thread_coordinates.vehicles[0].arrival_minutes, None)
    assert_equal(wrong_thread_coordinates.vehicles[0].age_seconds, 0)
    assert_equal(wrong_thread_coordinates.raw_status, "vehicle_prediction_thread_fallback")
    assert_equal(
        "vehicle_prediction_thread_fallback:not_found" in wrong_thread_coordinates.fallback_reason,
        True,
    )

    missing_thread = parse_vehicle_prediction_payload(
        {"predictions": [_prediction_without_thread("missing-thread", "20:24")]},
        profile=EVENING,
        current_time=current_time,
    )
    assert_equal(missing_thread.available, False)
    assert_equal(missing_thread.status, YandexSourceStatus.NO_TARGET)
    assert_equal(missing_thread.arrival_minutes, ())
    assert_equal(missing_thread.confidence.value, "low")
    assert_equal(missing_thread.raw_status, "vehicle_prediction_thread_fallback")
    assert_equal(
        "vehicle_prediction_thread_fallback:missing" in missing_thread.fallback_reason,
        True,
    )


def _vehicle_with_thread(vehicle_id: str, thread_id: str, arrival_minutes: int) -> dict[str, object]:
    return {
        "id": vehicle_id,
        "lat": 54.94,
        "lng": 83.12,
        "arrivalMinutes": arrival_minutes,
        "properties": {"VehicleMetaData": {"Transport": {"threadId": thread_id}}},
    }


def _vehicle_without_thread(vehicle_id: str, arrival_minutes: int) -> dict[str, object]:
    return {
        "id": vehicle_id,
        "lat": 54.94,
        "lng": 83.12,
        "arrivalMinutes": arrival_minutes,
    }


def _prediction(vehicle_id: str, thread_id: str, arrival_time: str) -> dict[str, object]:
    return {
        "vehicleId": vehicle_id,
        "threadId": thread_id,
        "stops": [{"stopId": "stop__9982094", "arrivalEstimation": arrival_time}],
    }


def _prediction_with_coordinates(vehicle_id: str, thread_id: str, arrival_time: str) -> dict[str, object]:
    prediction = _prediction(vehicle_id, thread_id, arrival_time)
    prediction["coordinates"] = [83.11582825444825, 54.94095686809654]
    return prediction


def _prediction_without_thread(vehicle_id: str, arrival_time: str) -> dict[str, object]:
    return {
        "vehicleId": vehicle_id,
        "stops": [{"stopId": "stop__9982094", "arrivalEstimation": arrival_time}],
    }
