from __future__ import annotations

from datetime import datetime
from urllib.parse import parse_qs, urlsplit

from route74.domain.commute import CommuteProfile
from route74.domain.profiles import EVENING, MORNING
from route74.models import NOVOSIBIRSK_TZ
from route74.sources.yandex.constants import route_map_url, stop_map_url, terminal_stop_id
from route74.sources.yandex.models import YandexSourceMethod, YandexSourceStatus
from route74.sources.yandex.parser import parse_vehicles_payload
from route74.sources.yandex.vehicle_prediction import parse_vehicle_prediction_payload


def main() -> None:
    _assert_route_url_contract(MORNING, expected_thread="2161326768", expected_stop="stop__9982194")
    _assert_route_url_contract(EVENING, expected_thread="2161326764", expected_stop="stop__9982094")
    _assert_stop_url_contract(MORNING, expected_stop="stop__9982194")
    _assert_stop_url_contract(EVENING, expected_stop="stop__9982094")
    _assert_terminal_stop_contract(MORNING, expected_terminal="3174363647")
    _assert_terminal_stop_contract(EVENING, expected_terminal="stop__9982203")
    _assert_vehicle_prediction_thread_guard()
    _assert_route_vehicle_eta_is_diagnostic()
    print("OK | yandex contract smoke passed")


def _assert_route_url_contract(profile: CommuteProfile, *, expected_thread: str, expected_stop: str) -> None:
    params = parse_qs(urlsplit(route_map_url(profile)).query)
    _assert_equal(params.get("threadId"), [expected_thread])
    _assert_equal(params.get("openedBy[stopId]"), [expected_stop])


def _assert_stop_url_contract(profile: CommuteProfile, *, expected_stop: str) -> None:
    path = urlsplit(stop_map_url(profile)).path.rstrip("/")
    _assert_equal(path.endswith(f"/stops/{expected_stop}"), True)


def _assert_terminal_stop_contract(profile: CommuteProfile, *, expected_terminal: str) -> None:
    _assert_equal(terminal_stop_id(profile), expected_terminal)


def _assert_vehicle_prediction_thread_guard() -> None:
    current_time = datetime(2026, 6, 4, 20, 12, tzinfo=NOVOSIBIRSK_TZ)
    forecast = parse_vehicle_prediction_payload(
        {
            "predictions": [
                {
                    "vehicleId": "wrong-thread",
                    "threadId": "2161326768",
                    "stops": [{"stopId": "stop__9982094", "arrivalEstimation": "20:24"}],
                }
            ]
        },
        profile=EVENING,
        current_time=current_time,
    )
    _assert_equal(forecast.available, False)
    _assert_equal(forecast.arrival_minutes, ())
    _assert_equal(forecast.status, YandexSourceStatus.NO_TARGET)
    _assert_equal(forecast.raw_status, "vehicle_prediction_thread_fallback")


def _assert_route_vehicle_eta_is_diagnostic() -> None:
    current_time = datetime(2026, 6, 4, 7, 0, tzinfo=NOVOSIBIRSK_TZ)
    forecast = parse_vehicles_payload(
        {
            "data": {
                "vehicles": [
                    {
                        "id": "raw-route-eta",
                        "lat": 54.94,
                        "lng": 83.12,
                        "arrivalMinutes": 4,
                        "properties": {"VehicleMetaData": {"Transport": {"threadId": "2161326768"}}},
                    }
                ]
            }
        },
        source_method=YandexSourceMethod.BROWSER,
        current_time=current_time,
        profile=MORNING,
    )
    _assert_equal(forecast.available, False)
    _assert_equal(forecast.status, YandexSourceStatus.COORDINATES_ONLY)
    _assert_equal(forecast.arrival_minutes, ())
    _assert_equal(forecast.vehicles[0].arrival_minutes, None)
    _assert_equal(forecast.fallback_reason, "route_vehicle_eta_ignored")


def _assert_equal(actual: object, expected: object) -> None:
    if actual != expected:
        raise AssertionError(f"expected {expected!r}, got {actual!r}")


if __name__ == "__main__":
    main()
