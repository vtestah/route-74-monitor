from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from route74.domain.profiles import MORNING
from route74.models import NOVOSIBIRSK_TZ
from route74.sources.yandex.cache import CachedYandexForecastSource
from route74.sources.yandex.config import YandexSourceConfig
from route74.sources.yandex.http_client import YandexHttpClient
from route74.sources.yandex.models import (
    YandexRawResponse,
    YandexSourceMethod,
    YandexSourceMode,
    YandexSourceStatus,
)
from route74.storage import connect, init_db


def main() -> None:
    _run_yandex_config_validation_smoke()
    _run_raw_response_validation_smoke()
    _run_http_timeout_validation_smoke()
    _run_invalid_primary_json_smoke()
    _run_non_object_json_smoke()
    _run_invalid_refreshed_token_smoke()
    _run_invalid_refreshed_json_smoke()
    _run_cached_forecast_bad_first_eta_smoke()
    _run_cached_forecast_bad_vehicle_coordinates_smoke()
    print("OK | yandex http smoke passed")


def _run_yandex_config_validation_smoke() -> None:
    YandexSourceConfig(
        mode=YandexSourceMode.OFF,
        cache_seconds=0,
        timeout_seconds=0.1,
        browser_min_interval_seconds=0.0,
        browser_cooldown_seconds=0,
        snapshot_cache_max_age_seconds=0,
    )
    _assert_rejects(lambda: YandexSourceConfig(mode="off"), "mode")
    _assert_rejects(lambda: YandexSourceConfig(cache_seconds=True), "cache_seconds")
    _assert_rejects(lambda: YandexSourceConfig(timeout_seconds=float("nan")), "timeout_seconds")
    _assert_rejects(lambda: YandexSourceConfig(browser_min_interval_seconds=float("inf")), "browser_min_interval")
    _assert_rejects(lambda: YandexSourceConfig(browser_cooldown_seconds=-1), "browser_cooldown")


def _run_raw_response_validation_smoke() -> None:
    YandexRawResponse({"ok": True}, YandexSourceStatus.OK)
    compact = YandexRawResponse(None, YandexSourceStatus.UNAVAILABLE, reason="  network\nfailed\tbadly  ")
    _assert_equal(compact.reason, "network failed badly")
    truncated = YandexRawResponse(None, YandexSourceStatus.UNAVAILABLE, reason="x" * 250)
    _assert_equal(truncated.reason, "x" * 200)
    _assert_rejects(lambda: YandexRawResponse([], YandexSourceStatus.OK), "payload")
    _assert_rejects(lambda: YandexRawResponse(None, "ok"), "status")
    _assert_rejects(lambda: YandexRawResponse(None, YandexSourceStatus.EMPTY, reason=None), "reason")
    _assert_rejects(lambda: YandexRawResponse(None, YandexSourceStatus.OK), "OK")


def _run_http_timeout_validation_smoke() -> None:
    YandexHttpClient(timeout_seconds=0.1).close()
    _assert_rejects(lambda: YandexHttpClient(timeout_seconds=0), "timeout")
    _assert_rejects(lambda: YandexHttpClient(timeout_seconds=True), "timeout")
    _assert_rejects(lambda: YandexHttpClient(timeout_seconds=float("nan")), "timeout")
    _assert_rejects(lambda: YandexHttpClient(timeout_seconds=float("inf")), "timeout")


def _run_invalid_primary_json_smoke() -> None:
    client = _client_with(
        (
            _FakeResponse(text='{"csrfToken":"initial"}'),
            _FakeResponse(json_error=True),
        )
    )

    raw = client.get_vehicles_info(MORNING)

    _assert_equal(raw.status, YandexSourceStatus.PARSE_ERROR)
    _assert_equal(raw.reason, "vehicles_json_invalid")
    _assert_equal(raw.payload, None)


def _run_invalid_refreshed_json_smoke() -> None:
    client = _client_with(
        (
            _FakeResponse(text='{"csrfToken":"initial"}'),
            _FakeResponse(payload={"csrfToken": "refreshed"}),
            _FakeResponse(json_error=True),
        )
    )

    raw = client.get_vehicles_info(MORNING)

    _assert_equal(raw.status, YandexSourceStatus.PARSE_ERROR)
    _assert_equal(raw.reason, "refreshed_vehicles_json_invalid")
    _assert_equal(raw.payload, None)


def _run_invalid_refreshed_token_smoke() -> None:
    for token in (None, True, "", "   "):
        client = _client_with(
            (
                _FakeResponse(text='{"csrfToken":"initial"}'),
                _FakeResponse(payload={"csrfToken": token}),
                _FakeResponse(payload={"data": {"vehicles": []}}),
            )
        )

        raw = client.get_vehicles_info(MORNING)

        _assert_equal(raw.status, YandexSourceStatus.PARSE_ERROR)
        _assert_equal(raw.reason, "refreshed_csrf_token_invalid")
        _assert_equal(raw.payload, None)


def _run_non_object_json_smoke() -> None:
    client = _client_with(
        (
            _FakeResponse(text='{"csrfToken":"initial"}'),
            _FakeResponse(payload=[]),
        )
    )

    raw = client.get_vehicles_info(MORNING)

    _assert_equal(raw.status, YandexSourceStatus.PARSE_ERROR)
    _assert_equal(raw.reason, "vehicles_json_not_object")
    _assert_equal(raw.payload, None)


def _run_cached_forecast_bad_first_eta_smoke() -> None:
    current_time = datetime(2026, 6, 4, 7, 0, tzinfo=NOVOSIBIRSK_TZ)
    with TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "cache.sqlite"
        with connect(db_path) as connection:
            init_db(connection)
            snapshot_id = _insert_cached_snapshot(connection, current_time)
            connection.execute(
                """
                INSERT INTO yandex_forecast_samples(
                    yandex_snapshot_id, sampled_at, service_date, weekday, minute_of_day,
                    profile_key, source_method, source_status, available, arrival_minutes,
                    next_arrival_minutes_json, vehicle_count, newest_age_seconds, confidence,
                    fallback_reason, report_window_key, raw_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    snapshot_id,
                    current_time.isoformat(),
                    current_time.date().isoformat(),
                    current_time.weekday(),
                    current_time.hour * 60 + current_time.minute,
                    MORNING.key,
                    YandexSourceMethod.VEHICLE_PREDICTION.value,
                    YandexSourceStatus.OK.value,
                    1,
                    "bad",
                    "[12]",
                    "bad",
                    "bad",
                    "high",
                    "",
                    "",
                    "{}",
                ),
            )
            connection.commit()

        forecast = CachedYandexForecastSource(db_path).get_forecast(MORNING, current_time)

    _assert_equal(forecast.available, True)
    _assert_equal(forecast.arrival_minutes, (12,))
    _assert_equal(forecast.vehicle_count, 0)


def _run_cached_forecast_bad_vehicle_coordinates_smoke() -> None:
    current_time = datetime(2026, 6, 4, 7, 0, tzinfo=NOVOSIBIRSK_TZ)
    with TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "cache.sqlite"
        with connect(db_path) as connection:
            init_db(connection)
            snapshot_id = _insert_cached_snapshot(connection, current_time)
            _insert_cached_forecast_sample(connection, snapshot_id, current_time)
            connection.execute(
                """
                INSERT INTO yandex_vehicle_observations(
                    snapshot_id, profile_key, source_method, source_status,
                    vehicle_id, thread_id, lat, lng, arrival_minutes, age_seconds, raw_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    snapshot_id,
                    MORNING.key,
                    YandexSourceMethod.VEHICLE_PREDICTION.value,
                    YandexSourceStatus.OK.value,
                    "bad-coordinates",
                    "",
                    "nan",
                    "inf",
                    12,
                    5,
                    "{}",
                ),
            )
            connection.commit()

        forecast = CachedYandexForecastSource(db_path).get_forecast(MORNING, current_time)

    _assert_equal(forecast.available, True)
    _assert_equal(forecast.vehicles[0].lat, None)
    _assert_equal(forecast.vehicles[0].lng, None)
    _assert_equal(forecast.vehicles[0].arrival_minutes, 12)


def _insert_cached_snapshot(connection: Any, sampled_at: datetime) -> int:
    cursor = connection.execute(
        """
        INSERT INTO yandex_snapshots(
            sampled_at, profile_key, source_method, source_status,
            available, vehicle_count, arrival_minutes_json, fallback_reason, raw_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            sampled_at.isoformat(),
            MORNING.key,
            YandexSourceMethod.VEHICLE_PREDICTION.value,
            YandexSourceStatus.OK.value,
            1,
            0,
            "[12]",
            "",
            "{}",
        ),
    )
    return int(cursor.lastrowid)


def _insert_cached_forecast_sample(connection: Any, snapshot_id: int, sampled_at: datetime) -> None:
    connection.execute(
        """
        INSERT INTO yandex_forecast_samples(
            yandex_snapshot_id, sampled_at, service_date, weekday, minute_of_day,
            profile_key, source_method, source_status, available, arrival_minutes,
            next_arrival_minutes_json, vehicle_count, newest_age_seconds, confidence,
            fallback_reason, report_window_key, raw_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            snapshot_id,
            sampled_at.isoformat(),
            sampled_at.date().isoformat(),
            sampled_at.weekday(),
            sampled_at.hour * 60 + sampled_at.minute,
            MORNING.key,
            YandexSourceMethod.VEHICLE_PREDICTION.value,
            YandexSourceStatus.OK.value,
            1,
            12,
            "[]",
            1,
            5,
            "high",
            "",
            "",
            "{}",
        ),
    )


def _client_with(responses: Iterable["_FakeResponse"]) -> YandexHttpClient:
    client = object.__new__(YandexHttpClient)
    client._client = _FakeHttpClient(responses)
    return client


class _FakeHttpClient:
    def __init__(self, responses: Iterable["_FakeResponse"]) -> None:
        self._responses = iter(responses)

    def get(self, *_args: object, **_kwargs: object) -> "_FakeResponse":
        return next(self._responses)


class _FakeResponse:
    def __init__(
        self,
        *,
        status_code: int = 200,
        text: str = "",
        payload: Any = None,
        json_error: bool = False,
    ) -> None:
        self.status_code = status_code
        self.text = text
        self._payload = {} if payload is None else payload
        self._json_error = json_error

    def raise_for_status(self) -> None:
        return None

    def json(self) -> Any:
        if self._json_error:
            raise ValueError("invalid json")
        return self._payload


def _assert_equal(actual: object, expected: object) -> None:
    if actual != expected:
        raise AssertionError(f"expected {expected!r}, got {actual!r}")


def _assert_rejects(action: Any, expected: str) -> None:
    try:
        action()
    except ValueError as exc:
        if expected not in str(exc):
            raise AssertionError(f"expected {expected!r} in {exc!s}") from exc
    else:
        raise AssertionError(f"expected {expected!r} validation failure")


if __name__ == "__main__":
    main()
