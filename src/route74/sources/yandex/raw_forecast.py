from __future__ import annotations

from dataclasses import replace
from datetime import datetime

from route74.domain.commute import CommuteProfile
from route74.sources.yandex.models import (
    YandexLiveForecast,
    YandexRawResponse,
    YandexSourceMethod,
    YandexSourceStatus,
)
from route74.sources.yandex.parser import parse_vehicles_payload
from route74.sources.yandex.stop_info import parse_stop_info_payload
from route74.sources.yandex.vehicle_prediction import parse_vehicle_prediction_payload


def forecast_from_raw(
    raw: YandexRawResponse,
    method: YandexSourceMethod,
    current_time: datetime,
    profile: CommuteProfile | None = None,
) -> YandexLiveForecast:
    if raw.status != YandexSourceStatus.OK:
        return YandexLiveForecast.unavailable(
            status=raw.status,
            source_method=method,
            reason=raw.reason,
        )
    if raw.payload is None:
        return YandexLiveForecast.unavailable(
            status=YandexSourceStatus.EMPTY,
            source_method=method,
            reason=raw.reason or "empty_payload",
        )
    return parse_vehicles_payload(
        raw.payload,
        source_method=method,
        current_time=current_time,
        profile=profile,
    )


def forecast_from_stop_info_raw(
    raw: YandexRawResponse,
    profile: CommuteProfile,
    current_time: datetime,
) -> YandexLiveForecast:
    if raw.status != YandexSourceStatus.OK:
        return YandexLiveForecast.unavailable(
            status=raw.status,
            source_method=YandexSourceMethod.STOP_INFO,
            reason=raw.reason,
        )
    if raw.payload is None:
        return YandexLiveForecast.unavailable(
            status=YandexSourceStatus.EMPTY,
            source_method=YandexSourceMethod.STOP_INFO,
            reason=raw.reason or "empty_payload",
        )
    return parse_stop_info_payload(raw.payload, profile=profile, current_time=current_time)


def forecast_from_vehicle_prediction_raw(
    raw: YandexRawResponse,
    profile: CommuteProfile,
    current_time: datetime,
) -> YandexLiveForecast:
    if raw.status != YandexSourceStatus.OK:
        return YandexLiveForecast.unavailable(
            status=raw.status,
            source_method=YandexSourceMethod.VEHICLE_PREDICTION,
            reason=raw.reason,
        )
    if raw.payload is None:
        return YandexLiveForecast.unavailable(
            status=YandexSourceStatus.EMPTY,
            source_method=YandexSourceMethod.VEHICLE_PREDICTION,
            reason=raw.reason or "empty_payload",
        )
    return parse_vehicle_prediction_payload(raw.payload, profile=profile, current_time=current_time)


def with_diagnostics(
    forecast: YandexLiveForecast,
    diagnostics: list[str],
) -> YandexLiveForecast:
    return replace(forecast, diagnostics=tuple(diagnostics))
