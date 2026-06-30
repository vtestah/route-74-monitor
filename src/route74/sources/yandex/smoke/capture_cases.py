from __future__ import annotations

from route74.sources.yandex.browser_client import (
    _capture_json_session_response,
    _captured_payload_response,
    _prediction_click_targets,
    capture_prediction_response,
)
from route74.sources.yandex.models import YandexSourceStatus
from route74.sources.yandex.smoke.assertions import assert_equal


def run_browser_capture_smoke() -> None:
    _run_prediction_thread_capture_smoke()
    _run_browser_capture_parse_error_smoke()
    _run_prediction_click_targets_smoke()


def _run_prediction_thread_capture_smoke() -> None:
    payloads: list[dict[str, object]] = []
    capture_prediction_response(
        _FakeResponse(
            "https://yandex.ru/maps/api/masstransit/getVehiclePredictionInfo?id=vehicle-1",
            {"data": {"stops": []}},
        ),
        payloads,
        {"vehicle-1": "2161326768"},
    )
    assert_equal(payloads[0]["vehicleId"], "vehicle-1")
    assert_equal(payloads[0]["threadId"], "2161326768")


def _run_browser_capture_parse_error_smoke() -> None:
    payloads: list[dict[str, object]] = []
    parse_errors: list[str] = []
    line_payloads: list[dict[str, object]] = []

    _capture_json_session_response(
        _FakeResponse("https://yandex.ru/maps/api/masstransit/getVehiclesInfo", []),
        payloads,
        "getVehiclesInfo",
        line_payloads,
        parse_errors,
        "browser_vehicles_json_invalid",
        "browser_vehicles_json_not_object",
    )
    raw = _captured_payload_response(payloads, parse_errors, "browser_no_vehicles_response")
    assert_equal(raw.status, YandexSourceStatus.PARSE_ERROR)
    assert_equal(raw.reason, "browser_vehicles_json_not_object")

    prediction_errors: list[str] = []
    capture_prediction_response(
        _FakeResponse("https://yandex.ru/maps/api/masstransit/getVehiclePredictionInfo", {"data": []}),
        [],
        parse_errors=prediction_errors,
    )
    assert_equal(prediction_errors, ["vehicle_prediction_data_not_object"])


def _run_prediction_click_targets_smoke() -> None:
    markers = [
        {"x": 880.0, "y": 610.0},
        {"x": 890.0, "y": 510.0},
        {"x": 830.0, "y": 300.0},
    ]
    projected_vehicles = [
        {"threadId": "2161326764", "x": 930.0, "y": 520.0},
        {"threadId": "2161326768", "x": 875.0, "y": 620.0},
        {"threadId": "2161326768", "x": 825.0, "y": 310.0},
    ]
    targets = _prediction_click_targets(markers, projected_vehicles, ("2161326768",))
    assert_equal(targets, [markers[0], markers[2]])


class _FakeResponse:
    def __init__(self, url: str, payload: object) -> None:
        self.url = url
        self._payload = payload

    def json(self) -> object:
        return self._payload
