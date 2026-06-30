from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta

from route74.domain.commute import CommuteProfile
from route74.sources.yandex.browser_client import ReusableChromium, YandexBrowserClient
from route74.sources.yandex.config import YandexSourceConfig
from route74.sources.yandex.fallback_policy import better_fallback, browser_result_is_final, http_result_is_final
from route74.sources.yandex.health import YandexSourceHealth
from route74.sources.yandex.http_client import YandexHttpClient
from route74.sources.yandex.line import YandexLineTopology, parse_line_payload
from route74.sources.yandex.models import (
    YandexLiveForecast,
    YandexSourceMethod,
    YandexSourceMode,
    YandexSourceStatus,
)
from route74.sources.yandex.raw_forecast import (
    forecast_from_raw,
    forecast_from_stop_info_raw,
    forecast_from_vehicle_prediction_raw,
    with_diagnostics,
)


class YandexTransportSource:
    def __init__(
        self,
        config: YandexSourceConfig | None = None,
        *,
        browser_session: ReusableChromium | None = None,
    ) -> None:
        self._config = config or YandexSourceConfig()
        self._cache: dict[str, tuple[datetime, YandexLiveForecast]] = {}
        self._health = YandexSourceHealth()
        self._browser_client = YandexBrowserClient(
            timeout_seconds=self._config.timeout_seconds,
            min_interval_seconds=self._config.browser_min_interval_seconds,
            persistent_browser=self._config.persistent_browser,
            browser_session=browser_session,
        )

    def close(self) -> None:
        self._browser_client.close()

    def consume_line_topologies(self) -> tuple[YandexLineTopology, ...]:
        return tuple(
            topology
            for payload in self._browser_client.consume_line_payloads()
            if (topology := parse_line_payload(payload)).threads
        )

    def get_forecast(self, profile: CommuteProfile, current_time: datetime) -> YandexLiveForecast:
        if not self._config.enabled or self._config.mode == YandexSourceMode.OFF:
            return YandexLiveForecast.disabled()
        cached = self._cached(profile.key, current_time)
        if cached is not None:
            return cached

        diagnostics: list[str] = []
        forecast = self._fetch(profile, current_time, diagnostics)
        if diagnostics and self._config.debug:
            forecast = with_diagnostics(forecast, diagnostics)
        self._cache[profile.key] = (current_time, forecast)
        return forecast

    def _cached(self, profile_key: str, current_time: datetime) -> YandexLiveForecast | None:
        cached = self._cache.get(profile_key)
        if cached is None:
            return None
        fetched_at, forecast = cached
        if current_time - fetched_at <= timedelta(seconds=self._config.cache_seconds):
            return forecast
        return None

    def _fetch(
        self,
        profile: CommuteProfile,
        current_time: datetime,
        diagnostics: list[str],
    ) -> YandexLiveForecast:
        fallback_candidate: YandexLiveForecast | None = None
        if self._config.mode in {YandexSourceMode.AUTO, YandexSourceMode.HTTP}:
            forecast = self._fetch_http(profile, current_time, diagnostics)
            if http_result_is_final(forecast, self._config.mode):
                return forecast
            fallback_candidate = better_fallback(fallback_candidate, forecast)

        if self._config.mode == YandexSourceMode.AUTO:
            forecast = self._fetch_with_cooldown(
                profile,
                current_time,
                diagnostics,
                method=YandexSourceMethod.VEHICLE_PREDICTION,
                cooldown_seconds=self._config.browser_cooldown_seconds,
                fetch=lambda: self._fetch_vehicle_prediction_browser(
                    profile,
                    current_time,
                    diagnostics,
                ),
            )
            if forecast.available:
                return forecast
            fallback_candidate = better_fallback(fallback_candidate, forecast)

        if self._config.mode == YandexSourceMode.AUTO:
            forecast = self._fetch_with_cooldown(
                profile,
                current_time,
                diagnostics,
                method=YandexSourceMethod.STOP_INFO,
                cooldown_seconds=self._config.browser_cooldown_seconds,
                fetch=lambda: self._fetch_stop_info_browser(profile, current_time, diagnostics),
            )
            if forecast.available:
                return forecast
            fallback_candidate = better_fallback(fallback_candidate, forecast)

        if self._config.mode in {YandexSourceMode.AUTO, YandexSourceMode.BROWSER}:
            forecast = self._fetch_with_cooldown(
                profile,
                current_time,
                diagnostics,
                method=YandexSourceMethod.BROWSER,
                cooldown_seconds=self._config.browser_cooldown_seconds,
                fetch=lambda: self._fetch_browser(profile, current_time, diagnostics),
            )
            if browser_result_is_final(forecast, self._config.mode):
                return forecast
            fallback_candidate = better_fallback(fallback_candidate, forecast)

        if fallback_candidate is not None:
            return fallback_candidate

        if diagnostics:
            return YandexLiveForecast.unavailable(
                status=YandexSourceStatus.UNAVAILABLE,
                reason="; ".join(diagnostics),
                diagnostics=tuple(diagnostics),
            )
        return YandexLiveForecast.unavailable(status=YandexSourceStatus.UNAVAILABLE, reason="no_source")

    def _fetch_http(self, profile: CommuteProfile, current_time: datetime, diagnostics: list[str]) -> YandexLiveForecast:
        with YandexHttpClient(timeout_seconds=self._config.timeout_seconds) as client:
            raw = client.get_vehicles_info(profile)
        forecast = forecast_from_raw(raw, YandexSourceMethod.HTTP, current_time, profile)
        if not forecast.available:
            diagnostics.append(f"http:{forecast.status}:{forecast.fallback_reason}")
        return forecast

    def _fetch_browser(
        self,
        profile: CommuteProfile,
        current_time: datetime,
        diagnostics: list[str],
    ) -> YandexLiveForecast:
        raw = self._browser_client.get_vehicles_info(profile)
        forecast = forecast_from_raw(raw, YandexSourceMethod.BROWSER, current_time, profile)
        if not forecast.available:
            diagnostics.append(f"browser:{forecast.status}:{forecast.fallback_reason}")
        return forecast

    def _fetch_vehicle_prediction_browser(
        self,
        profile: CommuteProfile,
        current_time: datetime,
        diagnostics: list[str],
    ) -> YandexLiveForecast:
        raw = self._browser_client.get_vehicle_predictions(profile)
        forecast = forecast_from_vehicle_prediction_raw(raw, profile, current_time)
        if not forecast.available:
            diagnostics.append(f"vehicle_prediction:{forecast.status}:{forecast.fallback_reason}")
        return forecast

    def _fetch_stop_info_browser(
        self,
        profile: CommuteProfile,
        current_time: datetime,
        diagnostics: list[str],
    ) -> YandexLiveForecast:
        raw = self._browser_client.get_stop_info(profile)
        forecast = forecast_from_stop_info_raw(raw, profile, current_time)
        if not forecast.available:
            diagnostics.append(f"stop_info:{forecast.status}:{forecast.fallback_reason}")
        return forecast

    def _fetch_with_cooldown(
        self,
        profile: CommuteProfile,
        current_time: datetime,
        diagnostics: list[str],
        *,
        method: YandexSourceMethod,
        cooldown_seconds: int,
        fetch: Callable[[], YandexLiveForecast],
    ) -> YandexLiveForecast:
        cooldown = self._health.cooldown(profile.key, method, current_time)
        if cooldown is not None:
            reason = f"{method.value}_cooldown_{cooldown.remaining_seconds(current_time)}s"
            diagnostics.append(f"{method.value}:{YandexSourceStatus.UNAVAILABLE}:{reason}")
            return YandexLiveForecast.unavailable(
                status=YandexSourceStatus.UNAVAILABLE,
                source_method=method,
                reason=reason,
            )
        forecast = fetch()
        self._health.record(
            profile.key,
            method,
            forecast.status,
            current_time,
            cooldown_seconds,
            forecast.fallback_reason,
        )
        return forecast
