from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from route74.domain.eta import EtaConfidence
from route74.domain.profiles import MORNING
from route74.domain.reporting import (
    ALL_REPORT_WINDOWS_KEY,
    REPORT_WINDOW_KEYS,
    REPORT_WINDOW_SELECTORS,
    REPORT_WINDOWS,
    REPORT_WINDOWS_BY_KEY,
    ReportWindow,
    matching_report_window,
    report_window_by_key,
    report_window_for_profile,
    report_profiles_for_time,
    validate_report_windows,
)
from route74.models import NOVOSIBIRSK_TZ
from route74.sources.yandex.models import YandexLiveForecast, YandexSourceMethod, YandexSourceStatus
from route74.storage import (
    connect,
    init_db,
    insert_yandex_snapshot,
    summarize_report_windows,
)


def main() -> None:
    _assert_report_window_contracts()
    with TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "report-windows.sqlite"
        sampled_at = datetime(2026, 6, 4, 9, 15, tzinfo=NOVOSIBIRSK_TZ)
        with connect(db_path) as connection:
            init_db(connection)
            _insert_report_sample(connection, sampled_at)
            bad_id = _insert_report_sample(connection, sampled_at + timedelta(minutes=1))
            connection.execute("UPDATE report_window_snapshots SET sampled_at = 'not-a-date' WHERE id = ?", (bad_id,))
            connection.commit()

            summary = summarize_report_windows(
                connection,
                days=7,
                report_window_key=REPORT_WINDOWS[0].key,
                profile_key=MORNING.key,
                current_time=sampled_at + timedelta(days=1),
            )
            _assert_report_summary_rejects_invalid_inputs(connection, sampled_at)

    _assert_equal(summary.total_samples, 2)
    _assert_equal(summary.latest_sampled_at, sampled_at)
    _assert_equal(summary.eta_samples, 2)
    print("OK | report windows smoke passed")


def _assert_report_window_contracts() -> None:
    sampled_at = datetime(2026, 6, 4, 9, 15, tzinfo=NOVOSIBIRSK_TZ)
    weekend = datetime(2026, 6, 6, 9, 15, tzinfo=NOVOSIBIRSK_TZ)
    _assert_equal(REPORT_WINDOW_KEYS, ("weekday_morning_09_12", "weekday_evening_19_22"))
    _assert_equal(REPORT_WINDOW_SELECTORS, (*REPORT_WINDOW_KEYS, ALL_REPORT_WINDOWS_KEY))
    _assert_equal(REPORT_WINDOWS_BY_KEY[REPORT_WINDOWS[0].key], REPORT_WINDOWS[0])
    _assert_equal(report_window_by_key(REPORT_WINDOWS[1].key), REPORT_WINDOWS[1])
    _assert_equal(report_window_for_profile(MORNING.key), REPORT_WINDOWS[0])
    _assert_equal(matching_report_window(sampled_at, MORNING.key), REPORT_WINDOWS[0])
    _assert_equal(matching_report_window(sampled_at, "evening"), None)
    _assert_equal(matching_report_window(weekend, MORNING.key), None)
    _assert_equal(report_profiles_for_time(sampled_at), (MORNING.key,))
    fixed_local = datetime(2026, 6, 4, 9, 15, tzinfo=timezone(timedelta(hours=7)))
    _assert_equal(matching_report_window(fixed_local, MORNING.key), REPORT_WINDOWS[0])
    _assert_invalid_window(
        lambda: matching_report_window(datetime(2026, 6, 4, 9, 15), MORNING.key),
        "timezone-aware",
    )
    _assert_invalid_window(
        lambda: matching_report_window(datetime(2026, 6, 4, 9, 15, tzinfo=timezone.utc), MORNING.key),
        "Asia/Novosibirsk",
    )
    _assert_invalid_window(
        lambda: report_profiles_for_time(datetime(2026, 6, 4, 9, 15)),
        "timezone-aware",
    )
    _assert_invalid_window(
        lambda: ReportWindow("", MORNING.key, "bad", time(9, 0), time(10, 0)),
        "key",
    )
    _assert_invalid_window(
        lambda: ReportWindow(" bad_key", MORNING.key, "bad", time(9, 0), time(10, 0)),
        "plain ASCII key",
    )
    _assert_invalid_window(
        lambda: ReportWindow("bad-key", MORNING.key, "bad", time(9, 0), time(10, 0)),
        "plain ASCII key",
    )
    _assert_invalid_window(
        lambda: ReportWindow("ключ", MORNING.key, "bad", time(9, 0), time(10, 0)),
        "plain ASCII key",
    )
    _assert_invalid_window(
        lambda: ReportWindow("blank_key", MORNING.key, " ", time(9, 0), time(10, 0)),
        "title",
    )
    _assert_invalid_window(
        lambda: ReportWindow("bad_window", MORNING.key, "bad", time(9, 0), time(9, 0)),
        "after start",
    )
    _assert_invalid_window(
        lambda: ReportWindow("bad_profile", "night", "bad", time(9, 0), time(10, 0)),
        "profile key",
    )
    _assert_invalid_window(
        lambda: ReportWindow(
            "bad_precision",
            MORNING.key,
            "bad",
            time(9, 0, 1),
            time(10, 0),
        ),
        "minute precision",
    )
    _assert_invalid_window(
        lambda: validate_report_windows(
            (
                ReportWindow("duplicate", MORNING.key, "first", time(9, 0), time(10, 0)),
                ReportWindow("duplicate", "evening", "second", time(19, 0), time(20, 0)),
            )
        ),
        "duplicate",
    )
    _assert_invalid_window(
        lambda: validate_report_windows(()),
        "at least one",
    )
    _assert_invalid_window(
        lambda: validate_report_windows(
            (
                ReportWindow("first", MORNING.key, "first", time(9, 0), time(10, 0)),
                ReportWindow("second", MORNING.key, "second", time(9, 30), time(10, 30)),
            )
        ),
        "overlap",
    )
    _assert_invalid_window(
        lambda: validate_report_windows((object(),)),  # type: ignore[arg-type]
        "ReportWindow",
    )
    _assert_invalid_window(
        lambda: report_window_by_key("night"),
        "unknown report window",
    )
    _assert_invalid_window(
        lambda: report_window_for_profile("night"),
        "profile key",
    )
    _assert_invalid_window(
        lambda: matching_report_window("now", MORNING.key),  # type: ignore[arg-type]
        "datetime",
    )
    _assert_invalid_window(
        lambda: matching_report_window(sampled_at, 123),  # type: ignore[arg-type]
        "profile key",
    )


def _insert_report_sample(connection: sqlite3.Connection, sampled_at: datetime) -> int:
    snapshot_id = insert_yandex_snapshot(connection, MORNING.key, _forecast(), sampled_at)
    row = connection.execute(
        "SELECT id FROM report_window_snapshots WHERE yandex_snapshot_id = ?",
        (snapshot_id,),
    ).fetchone()
    if row is None:
        raise AssertionError("expected report-window snapshot")
    return int(row["id"])


def _assert_report_summary_rejects_invalid_inputs(connection: sqlite3.Connection, current_time: datetime) -> None:
    _assert_invalid_window(
        lambda: summarize_report_windows(
            connection,
            days=0,
            current_time=current_time,
        ),
        "days must be a positive integer",
    )
    _assert_invalid_window(
        lambda: summarize_report_windows(  # type: ignore[arg-type]
            connection,
            days=True,
            current_time=current_time,
        ),
        "days must be a positive integer",
    )
    _assert_invalid_window(
        lambda: summarize_report_windows(
            connection,
            days=7,
            report_window_key="night",
            current_time=current_time,
        ),
        "unknown report_window_key",
    )
    _assert_invalid_window(
        lambda: summarize_report_windows(
            connection,
            days=7,
            profile_key="night",
            current_time=current_time,
        ),
        "profile_key must be one of",
    )
    _assert_invalid_window(
        lambda: summarize_report_windows(
            connection,
            days=7,
            current_time=datetime(2026, 6, 4, 9, 15),
        ),
        "timezone-aware",
    )


def _forecast() -> YandexLiveForecast:
    return YandexLiveForecast(
        enabled=True,
        available=True,
        source_method=YandexSourceMethod.VEHICLE_PREDICTION,
        status=YandexSourceStatus.OK,
        arrival_minutes=(6,),
        vehicle_count=1,
        newest_age_seconds=30,
        confidence=EtaConfidence.HIGH,
    )


def _assert_equal(actual: object, expected: object) -> None:
    if actual != expected:
        raise AssertionError(f"expected {expected!r}, got {actual!r}")


def _assert_invalid_window(factory: Callable[[], object], expected: str) -> None:
    try:
        factory()
    except ValueError as error:
        _assert_equal(expected in str(error), True)
    else:
        raise AssertionError(f"expected report window validation error containing {expected!r}")


if __name__ == "__main__":
    main()
