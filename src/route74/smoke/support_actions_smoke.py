from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from route74.support_actions import (
    bot_latency_command,
    bot_runtime_command,
    forecast_backtest_command_for_profile,
    forecast_backtest_command_for_window,
    forecast_coverage_command_for_profile,
    forecast_coverage_command_for_window,
    prediction_calibration_command_for_profile,
    prediction_calibration_command_for_window,
    prediction_evaluate_command_for_profile,
    prediction_evaluate_command_for_window,
    support_report_command_for_profile,
    support_report_command_for_window,
    support_snapshot_command_for_profile,
    watch_state_command_for_path,
)


def main() -> None:
    _assert_equal(bot_latency_command(), "route74 runtime-latency --hours 24")
    _assert_equal(bot_latency_command(hours=6), "route74 runtime-latency --hours 6")
    _assert_equal(
        bot_latency_command(hours=12, profile_key="morning"),
        "route74 runtime-latency --hours 12 --profile morning",
    )
    _assert_equal(bot_runtime_command(), "route74 runtime-events --hours 24 --limit 8")
    _assert_equal(bot_runtime_command(hours=6, limit=3), "route74 runtime-events --hours 6 --limit 3")
    _assert_equal(
        bot_runtime_command(hours=12, limit=4, profile_key="morning"),
        "route74 runtime-events --hours 12 --limit 4 --profile morning",
    )
    _assert_equal(
        bot_runtime_command(hours=12, limit=4, profile_key="morning", event_kind="user_reply"),
        "route74 runtime-events --hours 12 --limit 4 --profile morning --event-kind user_reply",
    )
    _assert_equal(
        bot_latency_command(hours=12, profile_key="morning", event_kind="watch_early"),
        "route74 runtime-latency --hours 12 --profile morning --event-kind watch_early",
    )
    _assert_equal(
        bot_runtime_command(event_kind="watch_early"),
        "route74 runtime-events --hours 24 --limit 8 --event-kind watch_early",
    )
    _assert_equal(
        bot_runtime_command(event_kind="watch_final"),
        "route74 runtime-events --hours 24 --limit 8 --event-kind watch_final",
    )
    _assert_equal(
        prediction_evaluate_command_for_profile("morning"),
        "route74 prediction-evaluate --window weekday_morning_09_12",
    )
    _assert_equal(
        prediction_evaluate_command_for_window("weekday_evening_19_22"),
        "route74 prediction-evaluate --window weekday_evening_19_22",
    )
    _assert_equal(
        prediction_calibration_command_for_profile("morning"),
        "route74 prediction-calibration --window weekday_morning_09_12",
    )
    _assert_equal(
        prediction_calibration_command_for_window("weekday_evening_19_22"),
        "route74 prediction-calibration --window weekday_evening_19_22",
    )
    _assert_equal(
        forecast_coverage_command_for_profile("morning"),
        "route74 forecast-coverage --window weekday_morning_09_12",
    )
    _assert_equal(
        forecast_coverage_command_for_window("weekday_evening_19_22"),
        "route74 forecast-coverage --window weekday_evening_19_22",
    )
    _assert_equal(
        forecast_backtest_command_for_profile("morning"),
        "route74 forecast-backtest --window weekday_morning_09_12",
    )
    _assert_equal(
        forecast_backtest_command_for_window("weekday_evening_19_22"),
        "route74 forecast-backtest --window weekday_evening_19_22",
    )
    _assert_equal(support_report_command_for_profile("evening"), "route74 support-report --profile evening")
    _assert_equal(
        support_report_command_for_profile("evening", event_kind="user_reply"),
        "route74 support-report --profile evening",
    )
    _assert_equal(
        support_report_command_for_profile("evening", event_kind="watch_early"),
        "route74 support-report --profile evening --event-kind watch_early",
    )
    _assert_equal(support_snapshot_command_for_profile("evening"), "route74 support-snapshot --profile evening")
    _assert_equal(
        support_report_command_for_window("weekday_morning_09_12"),
        "route74 support-report --window weekday_morning_09_12",
    )
    _assert_equal(
        support_report_command_for_window("weekday_morning_09_12", event_kind="watch_final"),
        "route74 support-report --window weekday_morning_09_12 --event-kind watch_final",
    )
    _assert_equal(
        watch_state_command_for_path(Path("data/custom watches.json")),
        "route74 watch-state --path 'data/custom watches.json'",
    )
    _assert_raises(lambda: bot_latency_command(hours=0), "hours")
    _assert_raises(lambda: bot_latency_command(profile_key="night"), "profile")
    _assert_raises(lambda: bot_runtime_command(hours=0), "hours")
    _assert_raises(lambda: bot_latency_command(event_kind="night"), "event kind")
    _assert_raises(lambda: bot_runtime_command(profile_key="night"), "profile")
    _assert_raises(lambda: bot_runtime_command(event_kind="night"), "event kind")
    _assert_raises(lambda: support_report_command_for_profile("morning", event_kind="night"), "event kind")
    _assert_raises(lambda: prediction_evaluate_command_for_profile("night"), "profile")
    _assert_raises(lambda: prediction_calibration_command_for_window("night"), "unknown report window")
    _assert_raises(lambda: support_report_command_for_window("night"), "unknown report window")
    _assert_raises(lambda: support_snapshot_command_for_profile("night"), "profile")
    _assert_raises(lambda: forecast_backtest_command_for_profile("night"), "profile")
    print("OK | support actions smoke passed")


def _assert_equal(actual: object, expected: object) -> None:
    if actual != expected:
        raise AssertionError(f"expected {expected!r}, got {actual!r}")


def _assert_raises(factory: Callable[[], object], expected: str) -> None:
    try:
        factory()
    except ValueError as error:
        if expected not in str(error):
            raise AssertionError(f"expected {expected!r} in {str(error)!r}") from error
        return
    raise AssertionError(f"expected ValueError containing {expected!r}")


if __name__ == "__main__":
    main()
