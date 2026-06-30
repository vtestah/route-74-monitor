from __future__ import annotations

from collections.abc import Callable
from contextlib import redirect_stderr
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory

from route74.cli import build_parser
from route74.domain.eta import EtaConfidence
from route74.domain.profiles import MORNING
from route74.models import NOVOSIBIRSK_TZ
from route74.services.yandex_forecast import build_yandex_forecast
from route74.sources.yandex.freshness import forecast_is_fresh, vehicle_is_fresh
from route74.sources.yandex.models import YandexLiveForecast, YandexSourceMethod, YandexSourceStatus, YandexVehicle
from route74.sources.yandex.parser.time_fields import arrival_minutes
from route74.sources.yandex.trust import (
    forecast_has_trusted_fresh_eta,
    is_trusted_eta_observation,
    trusted_arrivals_for_forecast,
)
from route74.storage import connect, init_db
from route74.storage.eta_quality import sanitize_untrusted_eta
from route74.storage.forecast_samples import insert_yandex_forecast_sample


class _InvalidEtaSource:
    def get_forecast(self, *_args: object) -> YandexLiveForecast:
        return YandexLiveForecast(
            enabled=True,
            available=True,
            source_method=YandexSourceMethod.VEHICLE_PREDICTION,
            status=YandexSourceStatus.OK,
            arrival_minutes=(-2, True, 8, 8),
            vehicles=(
                YandexVehicle("bad", arrival_minutes=-2),
                YandexVehicle("good", arrival_minutes=8),
                YandexVehicle("bool", arrival_minutes=True),
            ),
            vehicle_count=3,
            newest_age_seconds=0,
            confidence=EtaConfidence.HIGH,
        )


class _AvailableWithoutEtaSource:
    def get_forecast(self, *_args: object) -> YandexLiveForecast:
        return YandexLiveForecast(
            enabled=True,
            available=True,
            source_method=YandexSourceMethod.VEHICLE_PREDICTION,
            status=YandexSourceStatus.OK,
            arrival_minutes=(-1,),
            vehicles=(YandexVehicle("coords", lat=54.85, lng=83.10, arrival_minutes=-1),),
            vehicle_count=1,
            newest_age_seconds=0,
            confidence=EtaConfidence.HIGH,
        )


class _OutOfRangeEtaSource:
    def get_forecast(self, *_args: object) -> YandexLiveForecast:
        return YandexLiveForecast(
            enabled=True,
            available=True,
            source_method=YandexSourceMethod.VEHICLE_PREDICTION,
            status=YandexSourceStatus.OK,
            arrival_minutes=(8, 90),
            vehicles=(
                YandexVehicle("good", arrival_minutes=8),
                YandexVehicle("too-far", arrival_minutes=90),
            ),
            vehicle_count=2,
            newest_age_seconds=0,
            confidence=EtaConfidence.HIGH,
        )


class _FailingSource:
    def get_forecast(self, *_args: object) -> YandexLiveForecast:
        raise RuntimeError("boom")


def main() -> None:
    current_time = datetime(2026, 6, 4, 7, 0, tzinfo=NOVOSIBIRSK_TZ)

    _assert_equal(arrival_minutes({"eta": "nan"}, current_time), None)
    _assert_equal(arrival_minutes({"etaSeconds": "inf"}, current_time), None)
    _assert_equal(arrival_minutes({"eta": {"distanceLeft": 120}, "timeLeft": 4}, current_time), 4)
    _assert_equal(arrival_minutes({"secondsLeft": 120}, current_time), 2)
    _assert_rejects(
        lambda: arrival_minutes({"secondsLeft": 120}, datetime(2026, 6, 4, 7, 0)),
        "timezone-aware",
    )
    _assert_rejects(
        lambda: arrival_minutes({"secondsLeft": 120}, datetime(2026, 6, 4, 0, 0, tzinfo=timezone.utc)),
        "Asia/Novosibirsk",
    )
    _assert_vehicle_model_guardrails()
    _assert_forecast_model_guardrails()
    _assert_freshness_guards()
    _assert_trusted_arrival_guards()
    _assert_unavailable_eta_not_stored(current_time)
    _assert_untrusted_snapshot_sanitizer(current_time)

    normalized = build_yandex_forecast(_InvalidEtaSource(), MORNING, current_time)
    _assert_equal(normalized.available, True)
    _assert_equal(normalized.arrival_minutes, (8,))
    _assert_equal(normalized.vehicles[0].arrival_minutes, None)
    _assert_equal(normalized.vehicles[1].arrival_minutes, 8)
    _assert_equal(normalized.vehicles[2].arrival_minutes, None)
    _assert_equal(normalized.fallback_reason, "invalid_eta_filtered")

    unavailable = build_yandex_forecast(_AvailableWithoutEtaSource(), MORNING, current_time)
    _assert_equal(unavailable.available, False)
    _assert_equal(unavailable.status, YandexSourceStatus.COORDINATES_ONLY)
    _assert_equal(unavailable.arrival_minutes, ())
    _assert_equal(unavailable.vehicles[0].arrival_minutes, None)
    _assert_equal(unavailable.fallback_reason, "invalid_eta_filtered; available_without_eta")

    bounded = build_yandex_forecast(_OutOfRangeEtaSource(), MORNING, current_time)
    _assert_equal(bounded.available, True)
    _assert_equal(bounded.arrival_minutes, (8,))
    _assert_equal(bounded.vehicles[0].arrival_minutes, 8)
    _assert_equal(bounded.vehicles[1].arrival_minutes, None)
    _assert_equal(bounded.fallback_reason, "invalid_eta_filtered")

    failed = build_yandex_forecast(_FailingSource(), MORNING, current_time)
    _assert_equal(failed.available, False)
    _assert_equal(failed.status, YandexSourceStatus.UNAVAILABLE)
    _assert_equal(failed.fallback_reason, "source_exception")
    _assert_equal(failed.diagnostics, ("source_exception:RuntimeError",))
    _assert_equal("boom" in ",".join(failed.diagnostics), False)

    _assert_cli_rejects(
        ("forecast-readiness", "--profile", "morning", "--primary-bucket", "0"),
        "must be a positive integer",
    )
    _assert_cli_rejects(
        ("forecast-coverage", "--window", "weekday_morning_09_12", "--days", "0"),
        "must be a positive integer",
    )
    _assert_cli_rejects(
        ("forecast-health", "--max-heartbeat-age", "0"),
        "must be a positive integer",
    )
    _assert_cli_rejects(
        ("forecast-backtest", "--window", "weekday_morning_09_12", "--max-age-seconds", "0"),
        "must be a positive integer",
    )
    print("OK | yandex forecast smoke passed")


def _assert_equal(actual: object, expected: object) -> None:
    if actual != expected:
        raise AssertionError(f"expected {expected!r}, got {actual!r}")


def _assert_rejects(factory: Callable[[], object], expected: str) -> None:
    try:
        factory()
    except ValueError as error:
        if expected not in str(error):
            raise AssertionError(f"expected {expected!r} in {str(error)!r}") from error
    else:
        raise AssertionError(f"expected validation error: {expected}")


def _assert_cli_rejects(args: tuple[str, ...], expected: str) -> None:
    stderr = StringIO()
    with redirect_stderr(stderr):
        try:
            build_parser().parse_args(args)
        except SystemExit as exc:
            if exc.code != 2:
                raise AssertionError(f"expected argparse exit 2, got {exc.code}") from exc
        else:
            raise AssertionError(f"expected CLI to reject {args!r}")
    output = stderr.getvalue()
    if "Traceback" in output or expected not in output:
        raise AssertionError(f"expected {expected!r} without traceback in {output!r}")


def _assert_vehicle_model_guardrails() -> None:
    YandexVehicle("vehicle", lat=54.85, lng=83.10, thread_id="thread")
    _assert_rejects(lambda: YandexVehicle(""), "vehicle id")
    _assert_rejects(lambda: YandexVehicle("vehicle", thread_id=None), "thread id")
    _assert_rejects(lambda: YandexVehicle("vehicle", lat=54.85), "coordinates")
    _assert_rejects(lambda: YandexVehicle("vehicle", lat=float("nan"), lng=83.10), "latitude")
    _assert_rejects(lambda: YandexVehicle("vehicle", lat=91, lng=83.10), "latitude")
    _assert_rejects(lambda: YandexVehicle("vehicle", lat=54.85, lng=181), "longitude")


def _assert_forecast_model_guardrails() -> None:
    _forecast_model()
    for overrides, expected in (
        ({"enabled": "yes"}, "enabled"),
        ({"available": 1}, "available"),
        ({"source_method": "vehicle_prediction"}, "source method"),
        ({"status": "ok"}, "status"),
        ({"arrival_minutes": [7]}, "arrival minutes"),
        ({"vehicles": (object(),)}, "vehicles"),
        ({"vehicle_count": True}, "vehicle count"),
        ({"confidence": "high"}, "confidence"),
        ({"fallback_reason": None}, "fallback reason"),
        ({"raw_status": None}, "raw status"),
        ({"diagnostics": ("ok", object())}, "diagnostics"),
        ({"enabled": False, "available": True}, "disabled"),
    ):
        _assert_rejects(lambda overrides=overrides: _forecast_model(**overrides), expected)


def _assert_freshness_guards() -> None:
    _assert_equal(vehicle_is_fresh(YandexVehicle("negative-age", age_seconds=-1)), False)
    _assert_equal(vehicle_is_fresh(YandexVehicle("bool-age", age_seconds=True)), False)

    invalid_forecast_age = YandexLiveForecast(
        enabled=True,
        available=True,
        source_method=YandexSourceMethod.VEHICLE_PREDICTION,
        status=YandexSourceStatus.OK,
        arrival_minutes=(7,),
        newest_age_seconds=-1,
        confidence=EtaConfidence.HIGH,
    )
    bool_forecast_age = YandexLiveForecast(
        enabled=True,
        available=True,
        source_method=YandexSourceMethod.VEHICLE_PREDICTION,
        status=YandexSourceStatus.OK,
        arrival_minutes=(7,),
        newest_age_seconds=True,
        confidence=EtaConfidence.HIGH,
    )
    _assert_equal(forecast_is_fresh(invalid_forecast_age), False)
    _assert_equal(forecast_has_trusted_fresh_eta(invalid_forecast_age), False)
    _assert_equal(forecast_is_fresh(bool_forecast_age), False)


def _assert_trusted_arrival_guards() -> None:
    mixed = _trusted_raw_forecast((-1, True, 8, 8, 181))
    _assert_equal(trusted_arrivals_for_forecast(mixed), (8,))
    _assert_equal(forecast_has_trusted_fresh_eta(mixed), True)

    only_invalid = _trusted_raw_forecast((-1, True, 181))
    _assert_equal(trusted_arrivals_for_forecast(only_invalid), ())
    _assert_equal(forecast_has_trusted_fresh_eta(only_invalid), False)
    _assert_equal(
        is_trusted_eta_observation(
            YandexSourceMethod.VEHICLE_PREDICTION.value,
            fallback_reason=" vehicle_prediction_thread_fallback:not_found ",
        ),
        False,
    )
    _assert_equal(
        is_trusted_eta_observation(
            YandexSourceMethod.VEHICLE_PREDICTION.value,
            raw_status=" vehicle_prediction_thread_fallback:legacy ",
        ),
        False,
    )


def _assert_unavailable_eta_not_stored(sampled_at: datetime) -> None:
    forecast = _trusted_raw_forecast((8,), available=False)
    _assert_equal(trusted_arrivals_for_forecast(forecast), ())
    with TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "unavailable-eta.sqlite"
        with connect(db_path) as connection:
            init_db(connection)
            snapshot_id = connection.execute(
                """
                INSERT INTO yandex_snapshots(
                    sampled_at, profile_key, source_method, source_status,
                    available, vehicle_count, arrival_minutes_json, fallback_reason, raw_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    sampled_at.isoformat(),
                    "morning",
                    YandexSourceMethod.VEHICLE_PREDICTION.value,
                    YandexSourceStatus.OK.value,
                    0,
                    0,
                    "[8]",
                    "",
                    "{}",
                ),
            ).lastrowid
            insert_yandex_forecast_sample(
                connection,
                yandex_snapshot_id=int(snapshot_id),
                profile_key="morning",
                forecast=forecast,
                sampled_at=sampled_at,
            )
            row = connection.execute(
                "SELECT available, arrival_minutes, next_arrival_minutes_json FROM yandex_forecast_samples"
            ).fetchone()
    _assert_equal(row["available"], 0)
    _assert_equal(row["arrival_minutes"], None)
    _assert_equal(row["next_arrival_minutes_json"], "[]")


def _assert_untrusted_snapshot_sanitizer(sampled_at: datetime) -> None:
    with TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "eta-quality.sqlite"
        with connect(db_path) as connection:
            init_db(connection)
            for source_method, arrivals in (
                (YandexSourceMethod.BROWSER.value, "[0,2]"),
                (YandexSourceMethod.VEHICLE_PREDICTION.value, "[3]"),
            ):
                connection.execute(
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
                        source_method,
                        YandexSourceStatus.OK.value,
                        1,
                        1,
                        arrivals,
                        "smoke",
                        "{}",
                    ),
                )
            _assert_equal(sanitize_untrusted_eta(connection) > 0, True)
            rows = connection.execute(
                """
                SELECT source_method, arrival_minutes_json
                FROM yandex_snapshots
                ORDER BY source_method
                """,
            ).fetchall()
    _assert_equal(
        tuple((row["source_method"], row["arrival_minutes_json"]) for row in rows),
        (
            (YandexSourceMethod.BROWSER.value, "[]"),
            (YandexSourceMethod.VEHICLE_PREDICTION.value, "[3]"),
        ),
    )


def _trusted_raw_forecast(arrival_minutes: tuple[int, ...], *, available: bool = True) -> YandexLiveForecast:
    return YandexLiveForecast(
        enabled=True,
        available=available,
        source_method=YandexSourceMethod.VEHICLE_PREDICTION,
        status=YandexSourceStatus.OK,
        arrival_minutes=arrival_minutes,
        newest_age_seconds=0,
        confidence=EtaConfidence.HIGH,
    )


def _forecast_model(**overrides: object) -> YandexLiveForecast:
    values = {
        "enabled": True,
        "available": False,
        "source_method": YandexSourceMethod.VEHICLE_PREDICTION,
        "status": YandexSourceStatus.EMPTY,
    } | overrides
    return YandexLiveForecast(**values)  # type: ignore[arg-type]


if __name__ == "__main__":
    main()
