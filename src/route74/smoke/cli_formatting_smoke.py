from __future__ import annotations

from datetime import datetime
from pathlib import Path

from route74.cli.forecast_formatting import (
    format_forecast_readiness_summary,
    format_forecast_window_coverage_summary,
)
from route74.cli.formatting import (
    format_line_topology,
    format_report_window_summary,
    format_yandex_collect_result,
    format_yandex_telemetry_summary,
)
from route74.models import NOVOSIBIRSK_TZ
from route74.services.yandex_telemetry import YandexTelemetryResult
from route74.sources.yandex.line import YandexLineStop, YandexLineThread, YandexLineTopology
from route74.storage.models import (
    CollectorHeartbeat,
    CollectorRunSummary,
    CountByKey,
    ForecastCoverageBucket,
    ForecastReadinessSummary,
    ForecastWindowCoverageSummary,
    ReportWindowSummary,
    YandexTelemetrySummary,
)


def main() -> None:
    _assert_yandex_collect_result_is_single_line()
    _assert_yandex_summary_dynamic_values_are_single_line()
    _assert_report_window_dynamic_values_are_single_line()
    _assert_line_topology_dynamic_values_are_single_line()
    _assert_forecast_dynamic_values_are_single_line()
    _assert_diagnostic_values_drop_control_characters()
    print("OK | CLI formatting smoke passed")


def _assert_yandex_collect_result_is_single_line() -> None:
    long_reason = "x" * 140
    text = format_yandex_collect_result(
        YandexTelemetryResult(
            profile_key="morning\nextra",
            source_method="vehicle_prediction\nfallback",
            source_status="ok\nblocked",
            available=True,
            vehicle_count=1,
            arrival_minutes=(8,),
            traffic_provider="collector\nerror",
            traffic_status="not\ncollected",
            traffic_reason="traffic\nblocked",
            route_geometry_status="saved\nstale",
            route_geometry_reason="expected=216\nselected=999",
            fallback_reason=f"{long_reason}\nsecond line",
            total_snapshots=2,
            total_observations=3,
            prediction_events_created=4,
            arrival_events_created=1,
            evaluations_created=2,
        ),
        Path("data/route74.sqlite"),
    )
    _assert_not_contains(text, "\n")
    _assert_contains(text, "profile=morning extra")
    _assert_contains(text, "method=vehicle_prediction fallback")
    _assert_contains(text, "traffic_reason=traffic blocked")
    _assert_contains(text, "geometry_reason=expected=216 selected=999")
    _assert_contains(text, "prediction_lab=p4/a1/e2")
    _assert_not_contains(text, "x" * 121)


def _assert_yandex_summary_dynamic_values_are_single_line() -> None:
    text = format_yandex_telemetry_summary(
        YandexTelemetrySummary(
            profile_key="morning\nextra",
            hours=24,
            total_snapshots=0,
            eta_snapshots=0,
            vehicle_snapshots=0,
            total_observations=0,
            eta_observations=0,
            latest_sampled_at=None,
            heartbeat=CollectorHeartbeat(
                name="yandex-collect",
                updated_at=datetime(2026, 6, 4, 7, 0, tzinfo=NOVOSIBIRSK_TZ),
                pid=1234,
                profile_filter="morning\nall",
                last_status="ok\npartial",
                last_message="collector_error\nblocked",
            ),
            collector_runs=CollectorRunSummary(
                name="yandex-collect",
                hours=24,
                total_runs=0,
                result_runs=0,
                eta_runs=0,
                traffic_ok_runs=0,
                skipped_runs=0,
                latest_started_at=None,
                statuses=(CountByKey("ok\npartial", 1),),
            ),
            statuses=(),
            methods=(),
        ),
        Path("data/route74.sqlite"),
    )
    _assert_equal(len(text.splitlines()), 7)
    _assert_contains(text, "profile=morning extra")
    _assert_contains(text, "profiles=morning all message=collector_error blocked")
    _assert_not_contains(text, "\nblocked")


def _assert_report_window_dynamic_values_are_single_line() -> None:
    text = format_report_window_summary(
        ReportWindowSummary(
            days=30,
            report_window_key="weekday\nmorning",
            profile_key="morning\nextra",
            total_samples=0,
            eta_samples=0,
            traffic_samples=0,
            latest_sampled_at=None,
            statuses=(CountByKey("no_eta\nblocked", 1),),
        ),
        Path("data/route74.sqlite"),
    )
    _assert_equal(len(text.splitlines()), 3)
    _assert_contains(text, "window=weekday morning profile=morning extra")
    _assert_contains(text, "statuses=no_eta blocked:1")


def _assert_line_topology_dynamic_values_are_single_line() -> None:
    text = format_line_topology(
        YandexLineTopology(
            line_id="74\nbad",
            active_thread_id="thread\n1",
            threads=(
                YandexLineThread(
                    thread_id="thread\n1",
                    line_id="74",
                    name="74",
                    vehicle_type="bus",
                    start_stop_id="start",
                    start_stop_name="Start\nstop",
                    end_stop_id="end",
                    end_stop_name="End\nstop",
                    stops=(YandexLineStop("740\nbad", "Medical\ncenter"),),
                    points=(),
                ),
            ),
        )
    )
    _assert_equal(len(text.splitlines()), 3)
    _assert_contains(text, "line_id=74 bad")
    _assert_contains(text, "Start stop -> End stop")
    _assert_contains(text, "Medical center(740 bad)")


def _assert_forecast_dynamic_values_are_single_line() -> None:
    current_time = datetime(2026, 6, 4, 9, 15, tzinfo=NOVOSIBIRSK_TZ)
    db_path = Path("data/\x1b[31mforecast.sqlite")
    readiness = format_forecast_readiness_summary(
        ForecastReadinessSummary(
            profile_key="morning\nextra",
            report_window_key="weekday\nmorning",
            current_time=current_time,
            days=30,
            min_samples=1,
            min_distinct_days=1,
            primary_bucket_minutes=10,
            fallback_bucket_minutes=20,
            max_age_seconds=None,
            total_samples=1,
            eta_samples=1,
            fresh_eta_samples=1,
            traffic_samples=0,
            primary_samples=1,
            fallback_samples=0,
            primary_distinct_days=1,
            fallback_distinct_days=0,
            latest_sampled_at=None,
        ),
        db_path,
    )
    coverage = format_forecast_window_coverage_summary(
        ForecastWindowCoverageSummary(
            window_key="weekday\nmorning",
            profile_key="morning\nextra",
            days=30,
            min_samples=1,
            min_distinct_days=1,
            total_samples=1,
            eta_samples=1,
            fresh_eta_samples=1,
            traffic_samples=0,
            latest_sampled_at=None,
            buckets=(ForecastCoverageBucket("09:00\nspoof", True, 1, 1, 10, 1, 0, 1, 0),),
        ),
        db_path,
    )
    _assert_equal(len(readiness.splitlines()), 4)
    _assert_equal(len(coverage.splitlines()), 4)
    _assert_no_control_characters(readiness)
    _assert_no_control_characters(coverage)
    _assert_contains(readiness, "profile=morning extra window=weekday morning")
    _assert_contains(coverage, "buckets=09:00 spoof:ok")


def _assert_diagnostic_values_drop_control_characters() -> None:
    text = format_yandex_collect_result(
        YandexTelemetryResult(
            profile_key="morning\x1b[31m\nextra",
            source_method="vehicle_prediction\x1b[0m",
            source_status="ok\x1b[7m",
            available=True,
            vehicle_count=1,
            arrival_minutes=(8,),
            traffic_provider="collector\x1b[33m",
            traffic_status="not\x1b[0m collected",
            traffic_reason="traffic\x1b[31m blocked",
            route_geometry_status="saved\x1b[2m",
            route_geometry_reason="expected=216\x1b[0m\nselected=999",
            fallback_reason="fallback\x1b[0m reason",
            total_snapshots=2,
            total_observations=3,
        ),
        Path("data/\x1b[31mroute74.sqlite"),
    )
    _assert_not_contains(text, "\n")
    _assert_no_control_characters(text)
    _assert_contains(text, "profile=morning [31m extra")
    _assert_contains(text, "db=data/ [31mroute74.sqlite")


def _assert_contains(text: str, expected: str) -> None:
    if expected not in text:
        raise AssertionError(f"expected {expected!r} in {text!r}")


def _assert_not_contains(text: str, unexpected: str) -> None:
    if unexpected in text:
        raise AssertionError(f"did not expect {unexpected!r} in {text!r}")


def _assert_no_control_characters(text: str) -> None:
    for character in text:
        if character != "\n" and not character.isprintable():
            raise AssertionError(f"unexpected control character {character!r} in {text!r}")


def _assert_equal(actual: object, expected: object) -> None:
    if actual != expected:
        raise AssertionError(f"expected {expected!r}, got {actual!r}")


if __name__ == "__main__":
    main()
