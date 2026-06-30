from __future__ import annotations

from datetime import datetime, timedelta

from route74.domain.commute import CommuteProfile
from route74.domain.eta import EtaConfidence
from route74.domain.profiles import EVENING, MORNING
from route74.sources.yandex.config import YandexSourceConfig
from route74.sources.yandex.models import (
    YandexLiveForecast,
    YandexSourceMethod,
    YandexSourceStatus,
)
from route74.sources.yandex.smoke.assertions import assert_equal
from route74.sources.yandex.transport import YandexTransportSource


def run_browser_cooldown_smoke(current_time: datetime) -> None:
    source = _CountingYandexSource(YandexSourceConfig(cache_seconds=0, browser_cooldown_seconds=20))
    first = source.get_forecast(MORNING, current_time)
    second = source.get_forecast(MORNING, current_time + timedelta(seconds=5))
    third = source.get_forecast(MORNING, current_time + timedelta(seconds=25))

    assert_equal(first.status, YandexSourceStatus.UNAVAILABLE)
    assert_equal(second.status, YandexSourceStatus.UNAVAILABLE)
    assert_equal("browser_cooldown" in second.fallback_reason, True)
    assert_equal(third.status, YandexSourceStatus.UNAVAILABLE)
    assert_equal(source.browser_calls, 2)


def run_stop_info_fallback_smoke(current_time: datetime) -> None:
    source = _StopInfoFallbackSource(YandexSourceConfig(cache_seconds=0, browser_cooldown_seconds=20))
    forecast = source.get_forecast(MORNING, current_time)

    assert_equal(forecast.available, False)
    assert_equal(forecast.status, YandexSourceStatus.FREQUENCY_ONLY)
    assert_equal(forecast.source_method, YandexSourceMethod.STOP_INFO)
    assert_equal("интервал 30 мин" in forecast.fallback_reason, True)


def run_vehicle_prediction_source_smoke(current_time: datetime) -> None:
    source = _VehiclePredictionSource(YandexSourceConfig(cache_seconds=0, browser_cooldown_seconds=20))
    forecast = source.get_forecast(EVENING, current_time)

    assert_equal(forecast.available, True)
    assert_equal(forecast.source_method, YandexSourceMethod.VEHICLE_PREDICTION)
    assert_equal(forecast.arrival_minutes, (8,))


class _CountingYandexSource(YandexTransportSource):
    def __init__(self, config: YandexSourceConfig) -> None:
        super().__init__(config)
        self.browser_calls = 0

    def _fetch_http(
        self,
        _profile: CommuteProfile,
        _current_time: datetime,
        _diagnostics: list[str],
    ) -> YandexLiveForecast:
        return YandexLiveForecast.unavailable(
            status=YandexSourceStatus.NEEDS_SIGNATURE,
            source_method=YandexSourceMethod.HTTP,
            reason="test_needs_signature",
        )

    def _fetch_browser(
        self,
        _profile: CommuteProfile,
        _current_time: datetime,
        _diagnostics: list[str],
    ) -> YandexLiveForecast:
        self.browser_calls += 1
        return YandexLiveForecast.unavailable(
            status=YandexSourceStatus.EMPTY,
            source_method=YandexSourceMethod.BROWSER,
            reason="vehicles_empty",
        )

    def _fetch_vehicle_prediction_browser(
        self,
        _profile: CommuteProfile,
        _current_time: datetime,
        _diagnostics: list[str],
    ) -> YandexLiveForecast:
        return YandexLiveForecast.unavailable(
            status=YandexSourceStatus.EMPTY,
            source_method=YandexSourceMethod.VEHICLE_PREDICTION,
            reason="vehicle_prediction_empty",
        )

    def _fetch_stop_info_browser(
        self,
        _profile: CommuteProfile,
        _current_time: datetime,
        _diagnostics: list[str],
    ) -> YandexLiveForecast:
        return YandexLiveForecast.unavailable(
            status=YandexSourceStatus.EMPTY,
            source_method=YandexSourceMethod.STOP_INFO,
            reason="stop_info_empty",
        )


class _StopInfoFallbackSource(_CountingYandexSource):
    def _fetch_stop_info_browser(
        self,
        _profile: CommuteProfile,
        _current_time: datetime,
        _diagnostics: list[str],
    ) -> YandexLiveForecast:
        return YandexLiveForecast.unavailable(
            status=YandexSourceStatus.FREQUENCY_ONLY,
            source_method=YandexSourceMethod.STOP_INFO,
            reason="интервал 30 мин",
        )


class _VehiclePredictionSource(_CountingYandexSource):
    def _fetch_vehicle_prediction_browser(
        self,
        _profile: CommuteProfile,
        _current_time: datetime,
        _diagnostics: list[str],
    ) -> YandexLiveForecast:
        return YandexLiveForecast(
            enabled=True,
            available=True,
            source_method=YandexSourceMethod.VEHICLE_PREDICTION,
            status=YandexSourceStatus.OK,
            arrival_minutes=(8,),
            confidence=EtaConfidence.HIGH,
        )
