from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

from route74.models import NOVOSIBIRSK_TZ
from route74.storage.bot_latency import BotLatencySummary
from route74.storage.db_admin import DbHealthSummary
from route74.storage.forecast_health import ForecastCollectorHealth, ForecastHealthSummary, ForecastWindowHealth
from route74.storage.monitoring import MONITOR_CRITICAL, MONITOR_WARNING, MonitorIssue, MonitorSummary
from route74.storage.runtime_quality import (
    BotRuntimeCalibration,
    BotRuntimeCalibrationGroup,
    BotRuntimePredictionQuality,
    BotRuntimePredictionQualityGroup,
)
from route74.storage.yandex_canary import YandexCanaryHealth
from route74.support_actions import watch_state_command_for_path
from route74.support_triage import (
    TRIAGE_CRITICAL,
    TRIAGE_WARNING,
    SupportTriageItem,
    build_support_triage,
    operator_triage_item_for_items,
    primary_triage_item_for_items,
    _primary_action,
)
from route74.watch_state import WatchStateSummary


CURRENT_TIME = datetime(2026, 6, 8, 9, 0, tzinfo=NOVOSIBIRSK_TZ)
WATCH_STATE_PATH = Path("data/web_watches.json")
WATCH_STATE_ACTION = watch_state_command_for_path(WATCH_STATE_PATH)


def main() -> None:
    _assert_pending_evaluation_backlog_beats_truth_window()
    _assert_pending_backlog_beats_forecast_window()
    _assert_runtime_backlog_beats_forecast_window()
    _assert_cross_profile_runtime_monitor_issue_is_ignored()
    _assert_unscoped_monitor_issue_still_applies_to_profile_triage()
    _assert_no_eta_monitor_issue_points_to_bot_latency()
    _assert_history_readiness_points_to_forecast_readiness()
    _assert_integrity_gap_points_to_forecast_health()
    _assert_history_backtest_points_to_forecast_backtest()
    _assert_warning_latency_beats_warning_collector()
    _assert_collector_still_beats_pending_evaluation_backlog()
    _assert_guardrail_unavailable_beats_pending_evaluation_backlog()
    _assert_profile_guardrail_unavailable_points_to_support_report()
    _assert_operator_triage_promotes_runtime_over_forecast()
    _assert_operator_triage_promotes_watch_state_over_runtime()
    _assert_operator_triage_promotes_watch_state_over_collector()
    _assert_watch_state_file_beats_collector()
    _assert_watch_state_file_feeds_support_triage()
    _assert_overdue_watch_state_becomes_critical()
    _assert_watch_state_runtime_errors_warn()
    _assert_runtime_source_calibration_late_risk_feeds_support_triage()
    _assert_runtime_calibration_late_risk_feeds_support_triage_once()
    _assert_operator_triage_prefers_watch_and_runtime_actions()
    _assert_history_readiness_uses_forecast_readiness_but_loses_to_coverage()
    print("OK | support triage smoke passed")


def _assert_pending_evaluation_backlog_beats_truth_window() -> None:
    items = (
        SupportTriageItem(
            TRIAGE_WARNING,
            "truth_window",
            "window=weekday_morning_09_12 truth=insufficient",
            "route74 prediction-calibration --window weekday_morning_09_12",
        ),
        SupportTriageItem(
            TRIAGE_WARNING,
            "bot_runtime_pending",
            "pending=3/3(100%) oldest_pending=180m",
            "route74 prediction-evaluate --window weekday_morning_09_12",
        ),
    )
    _assert_equal(_primary_action(items), "route74 prediction-evaluate --window weekday_morning_09_12")
    item = primary_triage_item_for_items(items)
    if item is None:
        raise AssertionError("expected primary triage item")
    _assert_equal(item.key, "bot_runtime_pending")


def _assert_operator_triage_promotes_runtime_over_forecast() -> None:
    items = (
        SupportTriageItem(
            TRIAGE_WARNING,
            "forecast_window",
            "window=weekday_morning_09_12 status=no_collector_runs",
            "route74 forecast-health",
        ),
        SupportTriageItem(
            TRIAGE_WARNING,
            "bot_runtime_misses",
            "misses=2/3(67%)",
            "route74 prediction-calibration --window weekday_morning_09_12",
        ),
    )
    item = operator_triage_item_for_items(items)
    if item is None:
        raise AssertionError("expected operator triage item")
    _assert_equal(item.key, "bot_runtime_misses")
    _assert_equal(item.action, "route74 prediction-calibration --window weekday_morning_09_12")


def _assert_operator_triage_promotes_watch_state_over_runtime() -> None:
    items = (
        SupportTriageItem(
            TRIAGE_WARNING,
            "bot_runtime_misses",
            "misses=2/3(67%)",
            "route74 prediction-calibration --window weekday_morning_09_12",
        ),
        SupportTriageItem(
            TRIAGE_WARNING,
            "watch_state_runtime_error",
            "errors=2 watches=1",
            WATCH_STATE_ACTION,
        ),
    )
    item = operator_triage_item_for_items(items)
    if item is None:
        raise AssertionError("expected operator triage item")
    _assert_equal(item.key, "watch_state_runtime_error")
    _assert_equal(item.action, WATCH_STATE_ACTION)


def _assert_operator_triage_promotes_watch_state_over_collector() -> None:
    items = (
        SupportTriageItem(
            TRIAGE_CRITICAL,
            "collector",
            "yandex-collect status=stale",
            "route74 forecast-health",
        ),
        SupportTriageItem(
            TRIAGE_WARNING,
            "watch_state_runtime_error",
            "errors=2 watches=1",
            WATCH_STATE_ACTION,
        ),
    )
    _assert_equal(_primary_action(items), "route74 forecast-health")
    item = operator_triage_item_for_items(items)
    if item is None:
        raise AssertionError("expected operator triage item")
    _assert_equal(item.key, "watch_state_runtime_error")
    _assert_equal(item.action, WATCH_STATE_ACTION)


def _assert_pending_backlog_beats_forecast_window() -> None:
    forecast_window = replace(
        _ready_window(),
        status="no_collector_runs",
        reason="collector has not produced report-window snapshots yet",
        ready_buckets=0,
        collector_runs=0,
        collector_eta_runs=0,
        collector_traffic_ok_runs=0,
        missing_bucket_labels=("09:00", "09:30"),
    )
    triage = build_support_triage(
        window_key="weekday_morning_09_12",
        profile_key="morning",
        hours=24,
        monitor=_monitor(
            MonitorIssue(
                MONITOR_WARNING,
                "bot_runtime_pending",
                "pending=3/3(100%) oldest_pending=180m",
                profile_key="morning",
            )
        ),
        forecast=_forecast(forecast_window),
        runtime_quality=_runtime_quality(
            BotRuntimePredictionQualityGroup(
                key="morning",
                total=3,
                evaluated=0,
                pending=3,
                misses=0,
                guardrail_unavailable=0,
                average_error_minutes=None,
                p50_abs_error_minutes=None,
                latest_sampled_at=CURRENT_TIME,
                latest_evaluated_at=None,
                oldest_pending_sampled_at=CURRENT_TIME - timedelta(hours=3),
            )
        ),
        runtime_calibration=_runtime_calibration(),
    )
    item = primary_triage_item_for_items(triage.items)
    if item is None:
        raise AssertionError("expected primary triage item")
    _assert_equal(triage.primary_action, "route74 prediction-evaluate --window weekday_morning_09_12")
    _assert_equal(item.key, "bot_runtime_pending")


def _assert_runtime_backlog_beats_forecast_window() -> None:
    cases = (
        (
            SupportTriageItem(
                TRIAGE_WARNING,
                "bot_runtime_guardrail_unavailable",
                "guardrail_unavailable=2/2(100%)",
                "route74 support-report --profile morning",
            ),
            "route74 support-report --profile morning",
        ),
        (
            SupportTriageItem(
                TRIAGE_WARNING,
                "bot_runtime_misses",
                "misses=2/3(67%)",
                "route74 runtime-events --hours 24 --limit 8 --profile morning --event-kind user_reply",
            ),
            "route74 runtime-events --hours 24 --limit 8 --profile morning --event-kind user_reply",
        ),
        (
            SupportTriageItem(
                TRIAGE_WARNING,
                "bot_runtime_p50_error",
                "p50_abs_error=4m",
                "route74 runtime-events --hours 24 --limit 8 --profile morning --event-kind user_reply",
            ),
            "route74 runtime-events --hours 24 --limit 8 --profile morning --event-kind user_reply",
        ),
        (
            SupportTriageItem(
                TRIAGE_WARNING,
                "bot_runtime_late_risk",
                "profile=morning eval=3/3 miss=1(33%) suggested=+1m",
                "route74 runtime-events --hours 24 --limit 8 --profile morning --event-kind user_reply",
            ),
            "route74 runtime-events --hours 24 --limit 8 --profile morning --event-kind user_reply",
        ),
        (
            SupportTriageItem(
                TRIAGE_WARNING,
                "bot_runtime_source_late_risk",
                "profile=morning source=target_stop_live eval=3/3 miss=1(33%) suggested=+1m",
                "route74 prediction-calibration --window weekday_morning_09_12",
            ),
            "route74 prediction-calibration --window weekday_morning_09_12",
        ),
    )
    for runtime_item, expected_action in cases:
        items = (
            SupportTriageItem(
                TRIAGE_WARNING,
                "forecast_window",
                "window=weekday_morning_09_12 status=no_collector_runs reason=collector has not produced report-window snapshots yet",
                "route74 forecast-health",
            ),
            runtime_item,
        )
        item = primary_triage_item_for_items(items)
        if item is None:
            raise AssertionError("expected primary triage item")
        _assert_equal(item.key, runtime_item.key)
        _assert_equal(_primary_action(items), expected_action)


def _assert_operator_triage_prefers_watch_and_runtime_actions() -> None:
    watch_item = SupportTriageItem(
        TRIAGE_WARNING,
        "watch_state_runtime_error",
        "watch errors=2",
        WATCH_STATE_ACTION,
    )
    forecast_item = SupportTriageItem(
        TRIAGE_CRITICAL,
        "forecast_window",
        "window=weekday_morning_09_12 status=no_collector_runs",
        "route74 forecast-health",
    )
    runtime_item = SupportTriageItem(
        TRIAGE_WARNING,
        "bot_runtime_pending",
        "pending=3/3(100%) oldest_pending=180m",
        "route74 prediction-evaluate --window weekday_morning_09_12",
    )
    _assert_equal(operator_triage_item_for_items((forecast_item, watch_item)), watch_item)
    _assert_equal(operator_triage_item_for_items((forecast_item, runtime_item)), runtime_item)


def _assert_cross_profile_runtime_monitor_issue_is_ignored() -> None:
    triage = build_support_triage(
        window_key="weekday_evening_19_22",
        profile_key="evening",
        hours=24,
        monitor=_monitor(
            MonitorIssue(
                MONITOR_WARNING,
                "bot_runtime_pending",
                "pending=3/3(100%) oldest_pending=180m",
                profile_key="morning",
            )
        ),
        forecast=_forecast(_ready_window(window_key="weekday_evening_19_22", profile_key="evening")),
        runtime_quality=_runtime_quality(),
        runtime_calibration=_runtime_calibration(),
    )
    _assert_equal(tuple(item.key for item in triage.items), ("bot_runtime_profile",))
    _assert_equal(triage.status, "ok")
    _assert_equal(triage.primary_action, "route74 monitor-tick --fail-on critical")


def _assert_unscoped_monitor_issue_still_applies_to_profile_triage() -> None:
    triage = build_support_triage(
        window_key="weekday_evening_19_22",
        profile_key="evening",
        hours=24,
        monitor=_monitor(MonitorIssue(MONITOR_WARNING, "bot_latency_errors", "errors=1/2(50%)")),
        forecast=_forecast(_ready_window(window_key="weekday_evening_19_22", profile_key="evening")),
        runtime_quality=_runtime_quality(),
        runtime_calibration=_runtime_calibration(),
    )
    item = _item_by_key(triage.items, "bot_latency_errors")
    _assert_equal(triage.status, "warning")
    _assert_equal(item.action, "route74 runtime-latency --hours 24 --profile evening --event-kind user_reply")


def _assert_no_eta_monitor_issue_points_to_bot_latency() -> None:
    triage = build_support_triage(
        window_key="weekday_evening_19_22",
        profile_key="evening",
        hours=24,
        monitor=_monitor(MonitorIssue(MONITOR_WARNING, "bot_no_eta_replies", "no_eta=3/4(75%)")),
        forecast=_forecast(_ready_window(window_key="weekday_evening_19_22", profile_key="evening")),
        runtime_quality=_runtime_quality(),
        runtime_calibration=_runtime_calibration(),
    )
    item = _item_by_key(triage.items, "bot_no_eta_replies")
    _assert_equal(triage.status, "warning")
    _assert_equal(item.action, "route74 runtime-latency --hours 24 --profile evening --event-kind user_reply")
    _assert_equal(triage.primary_action, "route74 runtime-latency --hours 24 --profile evening --event-kind user_reply")


def _assert_history_readiness_points_to_forecast_readiness() -> None:
    triage = build_support_triage(
        window_key="weekday_morning_09_12",
        profile_key="morning",
        hours=24,
        monitor=_monitor(MonitorIssue(MONITOR_WARNING, "history_readiness", "samples=2/20", profile_key="morning")),
        forecast=_forecast(_ready_window()),
        runtime_quality=_runtime_quality(),
        runtime_calibration=_runtime_calibration(),
    )
    item = _item_by_key(triage.items, "history_readiness")
    _assert_equal(triage.status, "warning")
    _assert_equal(item.action, "route74 forecast-readiness --window weekday_morning_09_12")
    _assert_equal(triage.primary_action, "route74 forecast-readiness --window weekday_morning_09_12")


def _assert_integrity_gap_points_to_forecast_health() -> None:
    triage = build_support_triage(
        window_key="weekday_morning_09_12",
        profile_key="morning",
        hours=24,
        monitor=_monitor(),
        forecast=_forecast(
            replace(
                _ready_window(),
                status="integrity_gap",
                reason="forecast/report-window tables disagree: forecast_only=3, report_only=1",
                forecast_without_report_samples=3,
                report_without_forecast_samples=1,
            )
        ),
        runtime_quality=_runtime_quality(),
        runtime_calibration=_runtime_calibration(),
    )
    item = _item_by_key(triage.items, "integrity_gap")
    _assert_equal(triage.items[0].key, "integrity_gap")
    _assert_equal(triage.status, "warning")
    _assert_equal(item.action, "route74 forecast-health")
    _assert_equal(triage.primary_action, "route74 forecast-health")
    _assert_equal(item.message, "forecast_only=3 report_only=1")


def _assert_history_backtest_points_to_forecast_backtest() -> None:
    triage = build_support_triage(
        window_key="weekday_evening_19_22",
        profile_key="evening",
        hours=24,
        monitor=_monitor(
            MonitorIssue(
                MONITOR_WARNING,
                "history_backtest",
                "window=weekday_evening_19_22 miss=4/8(50%)",
                profile_key="evening",
            )
        ),
        forecast=_forecast(_ready_window(window_key="weekday_morning_09_12", profile_key="morning")),
        runtime_quality=_runtime_quality(),
        runtime_calibration=_runtime_calibration(),
    )
    item = _item_by_key(triage.items, "history_backtest")
    _assert_equal(triage.status, "warning")
    _assert_equal(_item_by_key(triage.items, "forecast_window_missing").action, "route74 forecast-health")
    _assert_equal(item.action, "route74 forecast-backtest --window weekday_evening_19_22")
    _assert_equal(triage.primary_action, "route74 forecast-backtest --window weekday_evening_19_22")


def _assert_warning_latency_beats_warning_collector() -> None:
    triage = build_support_triage(
        window_key="weekday_evening_19_22",
        profile_key="evening",
        hours=24,
        monitor=_monitor(
            MonitorIssue(MONITOR_WARNING, "collector", "yandex-collect status=stale"),
            MonitorIssue(
                MONITOR_WARNING,
                "bot_latency_errors",
                "errors=1/4(25%)",
                profile_key="evening",
            ),
        ),
        forecast=_forecast(_ready_window(window_key="weekday_evening_19_22", profile_key="evening")),
        runtime_quality=_runtime_quality(),
        runtime_calibration=_runtime_calibration(),
    )
    _assert_equal(triage.status, "warning")
    _assert_equal(triage.primary_action, "route74 forecast-health")


def _assert_collector_still_beats_pending_evaluation_backlog() -> None:
    items = (
        SupportTriageItem(
            TRIAGE_WARNING,
            "bot_runtime_pending",
            "pending=3/3(100%) oldest_pending=180m",
            "route74 prediction-evaluate --window weekday_morning_09_12",
        ),
        SupportTriageItem(
            TRIAGE_CRITICAL,
            "collector",
            "yandex-collect status=stale",
            "route74 forecast-health",
        ),
    )
    _assert_equal(_primary_action(items), "route74 forecast-health")


def _assert_guardrail_unavailable_beats_pending_evaluation_backlog() -> None:
    items = (
        SupportTriageItem(
            TRIAGE_WARNING,
            "bot_runtime_pending",
            "pending=3/3(100%) oldest_pending=180m",
            "route74 prediction-evaluate --window weekday_morning_09_12",
        ),
        SupportTriageItem(
            TRIAGE_WARNING,
            "bot_runtime_guardrail_unavailable",
            "guardrail_unavailable=2/2(100%)",
            "route74 support-report --profile morning",
        ),
    )
    _assert_equal(_primary_action(items), "route74 support-report --profile morning")


def _assert_profile_guardrail_unavailable_points_to_support_report() -> None:
    triage = build_support_triage(
        window_key="weekday_morning_09_12",
        profile_key="morning",
        hours=24,
        monitor=_monitor(
            MonitorIssue(
                MONITOR_WARNING,
                "bot_runtime_guardrail_unavailable",
                "guardrail_unavailable=2/2(100%)",
                profile_key="morning",
            )
        ),
        forecast=_forecast(),
        runtime_quality=_runtime_quality(),
        runtime_calibration=_runtime_calibration(),
    )
    item = _item_by_key(triage.items, "bot_runtime_guardrail_unavailable")
    _assert_equal(triage.status, "warning")
    _assert_equal(triage.primary_action, "route74 support-report --profile morning")
    _assert_equal(item.action, "route74 support-report --profile morning")


def _assert_watch_state_file_beats_collector() -> None:
    items = (
        SupportTriageItem(
            TRIAGE_CRITICAL,
            "collector",
            "yandex-collect status=stale",
            "route74 forecast-health",
        ),
        SupportTriageItem(
            TRIAGE_CRITICAL,
            "watch_state_file",
            "file=unreadable type=JSONDecodeError",
            WATCH_STATE_ACTION,
        ),
    )
    _assert_equal(_primary_action(items), WATCH_STATE_ACTION)


def _assert_watch_state_file_feeds_support_triage() -> None:
    triage = build_support_triage(
        window_key="weekday_morning_09_12",
        profile_key="morning",
        hours=24,
        monitor=_monitor(
            MonitorIssue(
                MONITOR_CRITICAL,
                "collector",
                "yandex-collect status=stale",
            )
        ),
        forecast=_forecast(),
        runtime_quality=_runtime_quality(),
        runtime_calibration=_runtime_calibration(),
        watch_state=_watch_summary(
            status="critical",
            file_status="unreadable",
            error_type="JSONDecodeError",
        ),
    )
    _assert_equal(triage.status, "critical")
    item = _item_by_key(triage.items, "watch_state_file")
    _assert_equal(triage.primary_action, WATCH_STATE_ACTION)
    _assert_equal(item.action, WATCH_STATE_ACTION)
    _assert_equal(item.message, "file=unreadable type=JSONDecodeError")


def _assert_overdue_watch_state_becomes_critical() -> None:
    triage = build_support_triage(
        window_key="weekday_morning_09_12",
        profile_key="morning",
        hours=24,
        monitor=_monitor(),
        forecast=_forecast(),
        runtime_quality=_runtime_quality(),
        runtime_calibration=_runtime_calibration(),
        watch_state=_watch_summary(
            status="warning",
            overdue_count=1,
            max_overdue_seconds=180,
        ),
    )
    _assert_equal(triage.status, "critical")
    item = _item_by_key(triage.items, "watch_state_overdue")
    _assert_equal(triage.primary_action, WATCH_STATE_ACTION)
    _assert_equal(item.action, WATCH_STATE_ACTION)
    _assert_equal(item.message, "active=1 due=1 overdue=1 max_overdue=180s")


def _assert_watch_state_runtime_errors_warn() -> None:
    triage = build_support_triage(
        window_key="weekday_morning_09_12",
        profile_key="morning",
        hours=24,
        monitor=_monitor(
            MonitorIssue(
                MONITOR_WARNING,
                "watch_state_runtime_error",
                "errors=2 watches=1 latest=2026-06-08T09:00:00+07:00 types=RuntimeError",
            )
        ),
        forecast=_forecast(),
        runtime_quality=_runtime_quality(),
        runtime_calibration=_runtime_calibration(),
        watch_state=_watch_summary(
            status="warning",
            runtime_error_count=2,
            runtime_error_records=1,
            latest_error_at=CURRENT_TIME,
            runtime_error_types=("RuntimeError",),
        ),
    )
    items = tuple(item for item in triage.items if item.key == "watch_state_runtime_error")
    _assert_equal(triage.status, "warning")
    _assert_equal(triage.primary_action, WATCH_STATE_ACTION)
    _assert_equal(len(items), 1)
    _assert_equal(items[0].action, WATCH_STATE_ACTION)
    _assert_equal(
        items[0].message,
        "errors=2 watches=1 latest=2026-06-08T09:00:00+07:00 types=RuntimeError",
    )


def _assert_runtime_source_calibration_late_risk_feeds_support_triage() -> None:
    triage = build_support_triage(
        window_key="weekday_morning_09_12",
        profile_key="morning",
        hours=24,
        monitor=_monitor(
            MonitorIssue(
                MONITOR_WARNING,
                "bot_runtime_source_late_risk",
                "profile=morning source=target_stop_live eval=3/3 miss=1(33%) p80_early=1m suggested=+1m",
                profile_key="morning",
            )
        ),
        forecast=_forecast(_ready_window()),
        runtime_quality=_runtime_quality(
            BotRuntimePredictionQualityGroup(
                key="morning",
                total=10,
                evaluated=10,
                pending=0,
                misses=1,
                guardrail_unavailable=0,
                average_error_minutes=1,
                p50_abs_error_minutes=1,
                latest_sampled_at=CURRENT_TIME,
                latest_evaluated_at=CURRENT_TIME,
                oldest_pending_sampled_at=None,
            )
        ),
        runtime_calibration=_runtime_calibration(
            BotRuntimeCalibrationGroup(
                "morning",
                10,
                10,
                1,
                1,
                1,
                0,
                "balanced",
                "keep current buffers",
            ),
            by_profile_source=(
                BotRuntimeCalibrationGroup(
                    "morning/target_stop_live",
                    3,
                    3,
                    1,
                    1,
                    1,
                    1,
                    "late_risk",
                    "review +1m buffer for affected profile",
                ),
            ),
        ),
    )
    item = _item_by_key(triage.items, "bot_runtime_source_late_risk")
    _assert_equal(triage.status, "warning")
    _assert_equal(triage.primary_action, "route74 prediction-calibration --window weekday_morning_09_12")
    _assert_equal(item.action, "route74 prediction-calibration --window weekday_morning_09_12")
    _assert_equal(
        item.message,
        "profile=morning source=target_stop_live eval=3/3 miss=1(33%) p80_early=1m suggested=+1m",
    )


def _assert_runtime_calibration_late_risk_feeds_support_triage_once() -> None:
    triage = build_support_triage(
        window_key="weekday_morning_09_12",
        profile_key="morning",
        hours=24,
        monitor=_monitor(
            MonitorIssue(
                MONITOR_WARNING,
                "bot_runtime_late_risk",
                "profile=morning eval=3/3 miss=1(33%) suggested=+1m",
                profile_key="morning",
            )
        ),
        forecast=_forecast(),
        runtime_quality=_runtime_quality(
            BotRuntimePredictionQualityGroup(
                key="morning",
                total=3,
                evaluated=3,
                pending=0,
                misses=1,
                guardrail_unavailable=0,
                average_error_minutes=1,
                p50_abs_error_minutes=1,
                latest_sampled_at=CURRENT_TIME,
                latest_evaluated_at=CURRENT_TIME,
                oldest_pending_sampled_at=None,
            )
        ),
        runtime_calibration=_runtime_calibration(
            BotRuntimeCalibrationGroup(
                key="morning",
                total=3,
                evaluated=3,
                misses=1,
                p80_early_minutes=1,
                p50_extra_wait_minutes=None,
                suggested_buffer_minutes=1,
                status="late_risk",
                action="review +1m buffer for affected profile",
            ),
            by_profile_source=(
                BotRuntimeCalibrationGroup(
                    key="morning/target_stop_live",
                    total=3,
                    evaluated=3,
                    misses=1,
                    p80_early_minutes=1,
                    p50_extra_wait_minutes=None,
                    suggested_buffer_minutes=1,
                    status="late_risk",
                    action="review +1m buffer for target stop live",
                ),
            ),
        ),
    )
    items = tuple(item for item in triage.items if item.key == "bot_runtime_late_risk")
    _assert_equal(triage.status, "warning")
    _assert_equal(triage.primary_action, "route74 prediction-calibration --window weekday_morning_09_12")
    _assert_equal(len(items), 1)
    _assert_equal(items[0].action, "route74 prediction-calibration --window weekday_morning_09_12")
    _assert_equal(
        items[0].message,
        (
            "profile=morning eval=3/3 miss=1(33%) p80_early=1m suggested=+1m "
            "source=target_stop_live source_eval=3/3 source_miss=1(33%) "
            "source_p80_early=1m source_suggested=+1m"
        ),
    )


def _assert_history_readiness_uses_forecast_readiness_but_loses_to_coverage() -> None:
    history_issue = MonitorIssue(
        MONITOR_WARNING,
        "history_readiness",
        "window=weekday_morning_09_12 bucket=+/-30m samples=12/20 days=2/3 fresh_eta=12 latest=2026-06-08 09:00",
        profile_key="morning",
    )
    coverage_window = replace(
        _ready_window(),
        status="insufficient_bucket_coverage",
        reason="missing report-window buckets",
        ready_buckets=1,
        total_buckets=2,
        missing_bucket_labels=("09:30",),
    )
    triage = build_support_triage(
        window_key="weekday_morning_09_12",
        profile_key="morning",
        hours=24,
        monitor=_monitor(history_issue),
        forecast=_forecast(coverage_window),
        runtime_quality=_runtime_quality(),
        runtime_calibration=_runtime_calibration(),
    )
    history_item = _item_by_key(triage.items, "history_readiness")
    _assert_equal(history_item.action, "route74 forecast-readiness --window weekday_morning_09_12")
    _assert_equal(triage.primary_action, "route74 forecast-coverage --window weekday_morning_09_12")


def _monitor(*issues: MonitorIssue) -> MonitorSummary:
    return MonitorSummary(
        db=DbHealthSummary(
            db_path=Path("data/route74.sqlite"),
            db_size_bytes=0,
            wal_size_bytes=0,
            shm_size_bytes=0,
            sqlite_version="3",
            journal_mode="wal",
            busy_timeout_ms=5000,
            foreign_keys=True,
            integrity_check="ok",
            quick_check="ok",
            table_counts=(),
            latest_timestamps=(),
        ),
        forecast=_forecast(),
        latency=BotLatencySummary(
            hours=24,
            latest_received_at=None,
            total_events=0,
            invalid_duration_events=0,
            error_events=0,
            no_eta_events=0,
            p50_total_ms=None,
            p95_total_ms=None,
            p95_forecast_ms=None,
            p95_send_ms=None,
            statuses=(),
            source_methods=(),
            update_types=(),
            event_kinds=(),
            reply_sources=(),
            error_reasons=(),
        ),
        issues=tuple(issues),
        runtime=_runtime_quality(),
    )


def _forecast(*windows: ForecastWindowHealth) -> ForecastHealthSummary:
    return ForecastHealthSummary(
        days=14,
        min_samples=20,
        min_distinct_days=3,
        collector=ForecastCollectorHealth(
            name="yandex-collect",
            status="ok",
            message="ok",
            updated_at=CURRENT_TIME,
            age_seconds=0,
            max_age_seconds=120,
        ),
        canary=YandexCanaryHealth("ok", CURRENT_TIME, "latest canary runs are ok", 0),
        windows=windows or (_ready_window(),),
    )


def _ready_window(
    *,
    window_key: str = "weekday_morning_09_12",
    profile_key: str = "morning",
) -> ForecastWindowHealth:
    return ForecastWindowHealth(
        window_key=window_key,
        profile_key=profile_key,
        status="ready",
        reason="all report-window buckets have enough fresh ETA samples",
        total_samples=20,
        eta_samples=20,
        fresh_eta_samples=20,
        traffic_samples=20,
        ready_buckets=2,
        total_buckets=2,
        forecast_without_report_samples=0,
        report_without_forecast_samples=0,
        collector_runs=1,
        collector_eta_runs=1,
        collector_traffic_ok_runs=1,
        collector_run_statuses=(),
        api_risk_samples=0,
        api_risk_reasons=(),
        coordinate_fallback_samples=0,
        coordinate_fallback_reasons=(),
        arrival_events=5,
        prediction_events=10,
        prediction_evaluations=10,
        prediction_miss_cases=0,
        bot_prediction_events=10,
        bot_prediction_evaluations=10,
        bot_prediction_miss_cases=0,
        truth_status="ready",
        truth_reason="enough truth events",
        latest_arrival_at=CURRENT_TIME,
        collector_latest_started_at=CURRENT_TIME,
        missing_bucket_labels=(),
        bucket_gaps=(),
        latest_sampled_at=CURRENT_TIME,
    )


def _runtime_quality(group: BotRuntimePredictionQualityGroup | None = None) -> BotRuntimePredictionQuality:
    return BotRuntimePredictionQuality(
        hours=24,
        total=group.total if group is not None else 0,
        evaluated=group.evaluated if group is not None else 0,
        pending=group.pending if group is not None else 0,
        misses=group.misses if group is not None else 0,
        guardrail_unavailable=0,
        average_error_minutes=group.average_error_minutes if group is not None else None,
        p50_abs_error_minutes=group.p50_abs_error_minutes if group is not None else None,
        latest_sampled_at=group.latest_sampled_at if group is not None else None,
        latest_evaluated_at=group.latest_evaluated_at if group is not None else None,
        oldest_pending_sampled_at=group.oldest_pending_sampled_at if group is not None else None,
        by_profile=(group,) if group is not None else (),
        by_source=(),
        by_profile_source=(),
        by_event_kind=(),
    )


def _runtime_calibration(
    profile_group: BotRuntimeCalibrationGroup | None = None,
    *,
    by_profile_source: tuple[BotRuntimeCalibrationGroup, ...] = (),
) -> BotRuntimeCalibration:
    return BotRuntimeCalibration(
        hours=24,
        total=profile_group.total if profile_group is not None else 0,
        evaluated=profile_group.evaluated if profile_group is not None else 0,
        misses=profile_group.misses if profile_group is not None else 0,
        p80_early_minutes=profile_group.p80_early_minutes if profile_group is not None else None,
        p50_extra_wait_minutes=profile_group.p50_extra_wait_minutes if profile_group is not None else None,
        suggested_buffer_minutes=profile_group.suggested_buffer_minutes if profile_group is not None else 0,
        status=profile_group.status if profile_group is not None else "insufficient",
        action=profile_group.action if profile_group is not None else "collect more evaluated bot replies",
        by_profile=(profile_group,) if profile_group is not None else (),
        by_source=(),
        by_profile_source=by_profile_source,
    )


def _watch_summary(
    *,
    status: str,
    overdue_count: int = 0,
    max_overdue_seconds: int | None = None,
    file_status: str = "ok",
    error_type: str = "",
    runtime_error_count: int = 0,
    runtime_error_records: int = 0,
    latest_error_at: datetime | None = None,
    runtime_error_types: tuple[str, ...] = (),
) -> WatchStateSummary:
    active_count = 1 if overdue_count or runtime_error_records else 0
    return WatchStateSummary(
        path=WATCH_STATE_PATH,
        current_time=CURRENT_TIME,
        status=status,
        active_count=active_count,
        due_count=overdue_count,
        overdue_count=overdue_count,
        expired_records=0,
        invalid_records=0,
        total_records=active_count,
        early_sent_count=0,
        oldest_age_minutes=10 if active_count else None,
        next_poll_at=None,
        max_overdue_seconds=max_overdue_seconds,
        file_status=file_status,
        error_type=error_type,
        profiles=(),
        runtime_error_count=runtime_error_count,
        runtime_error_records=runtime_error_records,
        latest_error_at=latest_error_at,
        runtime_error_types=runtime_error_types,
    )


def _item_by_key(items: tuple[SupportTriageItem, ...], key: str) -> SupportTriageItem:
    for item in items:
        if item.key == key:
            return item
    raise AssertionError(f"missing triage item {key!r}")


def _assert_equal(actual: object, expected: object) -> None:
    if actual != expected:
        raise AssertionError(f"expected {expected!r}, got {actual!r}")


if __name__ == "__main__":
    main()
