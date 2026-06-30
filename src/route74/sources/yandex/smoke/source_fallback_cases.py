from __future__ import annotations

from datetime import datetime

from route74.domain.commute import CommuteProfile
from route74.domain.eta import EtaConfidence
from route74.domain.profiles import EVENING
from route74.sources.yandex.config import YandexSourceConfig
from route74.sources.yandex.models import (
    YandexLiveForecast,
    YandexSourceMethod,
    YandexSourceStatus,
    YandexVehicle,
)
from route74.sources.yandex.smoke.assertions import assert_equal
from route74.sources.yandex.transport import YandexTransportSource


def run_auto_http_coordinates_continue_to_vehicle_prediction_smoke(
    current_time: datetime,
) -> None:
    source = _HttpCoordinatesThenVehiclePredictionSource(
        YandexSourceConfig(cache_seconds=0, browser_cooldown_seconds=20)
    )
    forecast = source.get_forecast(EVENING, current_time)

    assert_equal(forecast.available, True)
    assert_equal(forecast.source_method, YandexSourceMethod.VEHICLE_PREDICTION)
    assert_equal(forecast.arrival_minutes, (7,))
    assert_equal(source.vehicle_prediction_calls, 1)


def run_stop_info_fallback_wins_http_schedule_smoke(current_time: datetime) -> None:
    source = _HttpScheduleThenStopInfoFrequencySource(YandexSourceConfig(cache_seconds=0, browser_cooldown_seconds=20))
    forecast = source.get_forecast(EVENING, current_time)

    assert_equal(forecast.available, False)
    assert_equal(forecast.source_method, YandexSourceMethod.STOP_INFO)
    assert_equal(forecast.status, YandexSourceStatus.FREQUENCY_ONLY)
    assert_equal(forecast.fallback_reason, "интервал 30 мин")


def run_vehicle_prediction_no_target_fallback_smoke(current_time: datetime) -> None:
    source = _VehiclePredictionNoTargetSource(YandexSourceConfig(cache_seconds=0, browser_cooldown_seconds=20))
    forecast = source.get_forecast(EVENING, current_time)

    assert_equal(forecast.available, False)
    assert_equal(forecast.source_method, YandexSourceMethod.VEHICLE_PREDICTION)
    assert_equal(forecast.status, YandexSourceStatus.NO_TARGET)
    assert_equal(forecast.fallback_reason, "target_stop_not_found")


class _HttpCoordinatesThenVehiclePredictionSource(YandexTransportSource):
    def __init__(self, config: YandexSourceConfig) -> None:
        super().__init__(config)
        self.vehicle_prediction_calls = 0

    def _fetch_http(self, *_args: object) -> YandexLiveForecast:
        return YandexLiveForecast(
            enabled=True,
            available=False,
            source_method=YandexSourceMethod.HTTP,
            status=YandexSourceStatus.COORDINATES_ONLY,
            vehicles=(YandexVehicle(vehicle_id="http-coordinate", lat=54.94, lng=83.12, age_seconds=20),),
            vehicle_count=1,
            newest_age_seconds=20,
            confidence=EtaConfidence.LOW,
            fallback_reason="route_vehicle_eta_ignored",
        )

    def _fetch_vehicle_prediction_browser(
        self,
        _profile: CommuteProfile,
        _current_time: datetime,
        _diagnostics: list[str],
    ) -> YandexLiveForecast:
        self.vehicle_prediction_calls += 1
        return YandexLiveForecast(
            enabled=True,
            available=True,
            source_method=YandexSourceMethod.VEHICLE_PREDICTION,
            status=YandexSourceStatus.OK,
            arrival_minutes=(7,),
            confidence=EtaConfidence.HIGH,
        )

    def _fetch_stop_info_browser(self, *_args: object) -> YandexLiveForecast:
        return YandexLiveForecast.unavailable(status=YandexSourceStatus.EMPTY, reason="stop_info_empty")

    def _fetch_browser(self, *_args: object) -> YandexLiveForecast:
        return YandexLiveForecast.unavailable(status=YandexSourceStatus.EMPTY, reason="browser_empty")


class _HttpScheduleThenStopInfoFrequencySource(YandexTransportSource):
    def _fetch_http(self, *_args: object) -> YandexLiveForecast:
        return YandexLiveForecast.unavailable(
            status=YandexSourceStatus.SCHEDULE_ONLY,
            source_method=YandexSourceMethod.HTTP,
            reason="план Яндекса: 07:30",
        )

    def _fetch_vehicle_prediction_browser(self, *_args: object) -> YandexLiveForecast:
        return YandexLiveForecast.unavailable(
            status=YandexSourceStatus.EMPTY,
            source_method=YandexSourceMethod.VEHICLE_PREDICTION,
            reason="vehicle_prediction_empty",
        )

    def _fetch_stop_info_browser(self, *_args: object) -> YandexLiveForecast:
        return YandexLiveForecast.unavailable(
            status=YandexSourceStatus.FREQUENCY_ONLY,
            source_method=YandexSourceMethod.STOP_INFO,
            reason="интервал 30 мин",
        )

    def _fetch_browser(self, *_args: object) -> YandexLiveForecast:
        return YandexLiveForecast.unavailable(
            status=YandexSourceStatus.EMPTY,
            source_method=YandexSourceMethod.BROWSER,
            reason="browser_empty",
        )


class _VehiclePredictionNoTargetSource(YandexTransportSource):
    def _fetch_http(self, *_args: object) -> YandexLiveForecast:
        return YandexLiveForecast.unavailable(
            status=YandexSourceStatus.NEEDS_SIGNATURE,
            source_method=YandexSourceMethod.HTTP,
            reason="http_needs_signature",
        )

    def _fetch_vehicle_prediction_browser(self, *_args: object) -> YandexLiveForecast:
        return YandexLiveForecast.unavailable(
            status=YandexSourceStatus.NO_TARGET,
            source_method=YandexSourceMethod.VEHICLE_PREDICTION,
            reason="target_stop_not_found",
        )

    def _fetch_stop_info_browser(self, *_args: object) -> YandexLiveForecast:
        return YandexLiveForecast.unavailable(
            status=YandexSourceStatus.EMPTY,
            source_method=YandexSourceMethod.STOP_INFO,
            reason="stop_info_empty",
        )

    def _fetch_browser(self, *_args: object) -> YandexLiveForecast:
        return YandexLiveForecast.unavailable(
            status=YandexSourceStatus.EMPTY,
            source_method=YandexSourceMethod.BROWSER,
            reason="browser_empty",
        )
