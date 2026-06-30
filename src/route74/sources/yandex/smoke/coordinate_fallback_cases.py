from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from route74.domain.eta import EtaConfidence
from route74.domain.profiles import EVENING
from route74.sources.yandex.config import YandexSourceConfig
from route74.sources.yandex.live_evidence import (
    LiveEtaEvidenceAdjustment,
    live_eta_evidence_adjustment,
)
from route74.sources.yandex.models import (
    YandexLiveForecast,
    YandexSourceMethod,
    YandexSourceStatus,
    YandexVehicle,
)
from route74.sources.yandex.parser import parse_vehicles_payload
from route74.sources.yandex.smoke.assertions import assert_equal
from route74.sources.yandex.transport import YandexTransportSource
from route74.sources.yandex.vehicle_prediction import parse_vehicle_prediction_payload


def run_raw_vehicle_invalid_coordinate_smoke(current_time: datetime) -> None:
    forecast = parse_vehicles_payload(
        {
            "data": {
                "vehicles": [
                    {"id": "bad-direct", "lat": 120, "lng": 83.1, "ageSeconds": 10},
                    {
                        "id": "bad-geometry",
                        "geometry": {"coordinates": [183.1, 54.9]},
                        "ageSeconds": 12,
                    },
                    {
                        "id": "swapped-local",
                        "geometry": {"coordinates": [54.940956, 83.115828]},
                        "ageSeconds": 14,
                    },
                ]
            }
        },
        source_method=YandexSourceMethod.BROWSER,
        current_time=current_time,
    )
    assert_equal(forecast.status, YandexSourceStatus.COORDINATES_ONLY)
    assert_equal(forecast.vehicle_count, 3)
    assert_equal(forecast.vehicles[0].lat, None)
    assert_equal(forecast.vehicles[0].lng, None)
    assert_equal(forecast.vehicles[1].lat, None)
    assert_equal(forecast.vehicles[1].lng, None)
    assert_equal(forecast.vehicles[2].lat, None)
    assert_equal(forecast.vehicles[2].lng, None)

    nested = parse_vehicles_payload(
        {
            "data": {
                "vehicles": [
                    {
                        "id": "nested-geometry",
                        "geometry": {
                            "type": "MultiLineString",
                            "coordinates": [[[[83.110656, 54.840692], [83.110924, 54.840811]]]],
                        },
                        "ageSeconds": 15,
                    }
                ]
            }
        },
        source_method=YandexSourceMethod.BROWSER,
        current_time=current_time,
    )
    assert_equal(nested.status, YandexSourceStatus.COORDINATES_ONLY)
    assert_equal(nested.vehicles[0].lat, 54.840811)
    assert_equal(nested.vehicles[0].lng, 83.110924)


def run_vehicle_prediction_coordinate_fallback_smoke(current_time: datetime) -> None:
    forecast = parse_vehicle_prediction_payload(
        {
            "predictions": [
                {
                    "vehicleId": "target-missing-with-coordinates",
                    "threadId": "2161326764",
                    "coordinates": [83.11582825444825, 54.94095686809654],
                    "stops": [{"stopId": "stop__9982194", "arrivalEstimation": "20:34"}],
                }
            ]
        },
        profile=EVENING,
        current_time=current_time,
    )
    assert_equal(forecast.available, False)
    assert_equal(forecast.status, YandexSourceStatus.COORDINATES_ONLY)
    assert_equal(forecast.vehicle_count, 1)
    assert_equal(forecast.newest_age_seconds, 0)
    assert_equal(forecast.vehicles[0].vehicle_id, "target-missing-with-coordinates")
    assert_equal(forecast.vehicles[0].thread_id, "2161326764")
    assert_equal(forecast.vehicles[0].arrival_minutes, None)
    assert_equal(forecast.vehicles[0].age_seconds, 0)
    assert_equal("target_stop_not_found" in forecast.fallback_reason, True)


def run_vehicle_prediction_invalid_coordinate_smoke(current_time: datetime) -> None:
    forecast = parse_vehicle_prediction_payload(
        {
            "predictions": [
                {
                    "vehicleId": "target-with-invalid-coordinates",
                    "threadId": "2161326764",
                    "coordinates": [183.1, 54.94095686809654],
                    "stops": [{"stopId": "stop__9982094", "arrivalEstimation": "07:06"}],
                }
            ]
        },
        profile=EVENING,
        current_time=current_time,
    )
    assert_equal(forecast.available, True)
    assert_equal(forecast.arrival_minutes, (6,))
    assert_equal(forecast.vehicles[0].lat, None)
    assert_equal(forecast.vehicles[0].lng, None)
    evidence = live_eta_evidence_adjustment(forecast, arrival_minutes=6)
    assert_equal(evidence.safety_wait_minutes, 2)
    assert_equal(evidence.reason, "short_vehicle_prediction_eta_without_vehicle_coordinates")


def run_live_eta_evidence_guard_smoke() -> None:
    _assert_rejects(
        lambda: LiveEtaEvidenceAdjustment(safety_wait_minutes=-1),
        "safety wait",
    )
    _assert_rejects(
        lambda: LiveEtaEvidenceAdjustment(safety_wait_minutes=True),
        "safety wait",
    )
    _assert_rejects(
        lambda: LiveEtaEvidenceAdjustment(safety_wait_minutes=2),
        "scope and reason",
    )
    _assert_rejects(
        lambda: LiveEtaEvidenceAdjustment(
            safety_wait_minutes=2,
            scope="live-eta",
            reason="short ETA",
        ),
        "plain key",
    )
    _assert_rejects(
        lambda: LiveEtaEvidenceAdjustment(
            safety_wait_minutes=2,
            scope="live_eta_no_coordinates",
            reason="short ETA\nspoofed",
        ),
        "compact single-line",
    )
    _assert_rejects(
        lambda: LiveEtaEvidenceAdjustment(
            safety_wait_minutes=2,
            scope="live_eta_no_coordinates",
            reason="x" * 121,
        ),
        "compact single-line",
    )
    _assert_rejects(
        lambda: LiveEtaEvidenceAdjustment(scope="live_eta_no_coordinates"),
        "inactive",
    )

    forecast = YandexLiveForecast(
        enabled=True,
        available=True,
        source_method=YandexSourceMethod.STOP_INFO,
        status=YandexSourceStatus.OK,
        arrival_minutes=(3,),
        confidence=EtaConfidence.HIGH,
    )
    assert_equal(live_eta_evidence_adjustment(forecast, arrival_minutes=-1).applied, False)
    assert_equal(live_eta_evidence_adjustment(forecast, arrival_minutes=True).applied, False)


def run_vehicle_prediction_source_coordinate_fallback_smoke(
    current_time: datetime,
) -> None:
    source = _VehiclePredictionCoordinatesFallbackSource(
        YandexSourceConfig(cache_seconds=0, browser_cooldown_seconds=20)
    )
    forecast = source.get_forecast(EVENING, current_time)
    assert_equal(forecast.available, False)
    assert_equal(forecast.source_method, YandexSourceMethod.VEHICLE_PREDICTION)
    assert_equal(forecast.status, YandexSourceStatus.COORDINATES_ONLY)
    assert_equal(forecast.vehicle_count, 1)
    assert_equal(forecast.newest_age_seconds, 0)
    assert_equal(forecast.vehicles[0].vehicle_id, "vehicle-prediction-coordinate")
    assert_equal(forecast.vehicles[0].thread_id, "2161326764")
    assert_equal(forecast.vehicles[0].age_seconds, 0)


class _VehiclePredictionCoordinatesFallbackSource(YandexTransportSource):
    def _fetch_http(self, *_args: object) -> YandexLiveForecast:
        return YandexLiveForecast.unavailable(status=YandexSourceStatus.NEEDS_SIGNATURE, reason="test_needs_signature")

    def _fetch_vehicle_prediction_browser(self, *_args: object) -> YandexLiveForecast:
        return YandexLiveForecast(
            enabled=True,
            available=False,
            source_method=YandexSourceMethod.VEHICLE_PREDICTION,
            status=YandexSourceStatus.COORDINATES_ONLY,
            vehicles=(
                YandexVehicle(
                    vehicle_id="vehicle-prediction-coordinate",
                    lat=54.94,
                    lng=83.12,
                    age_seconds=0,
                    thread_id="2161326764",
                ),
            ),
            vehicle_count=1,
            newest_age_seconds=0,
            confidence=EtaConfidence.LOW,
            fallback_reason="target_stop_not_found:stop__9982094",
        )

    def _fetch_stop_info_browser(self, *_args: object) -> YandexLiveForecast:
        return YandexLiveForecast.unavailable(
            status=YandexSourceStatus.FREQUENCY_ONLY,
            source_method=YandexSourceMethod.STOP_INFO,
            reason="интервал 30 мин",
        )

    def _fetch_browser(self, *_args: object) -> YandexLiveForecast:
        return YandexLiveForecast.unavailable(status=YandexSourceStatus.NO_TARGET, reason="direction_thread_not_found")


def _assert_rejects(factory: Callable[[], object], expected: str) -> None:
    try:
        factory()
    except ValueError as error:
        if expected not in str(error):
            raise AssertionError(f"expected {expected!r} in {str(error)!r}") from error
    else:
        raise AssertionError(f"expected validation error: {expected}")
