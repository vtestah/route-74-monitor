from __future__ import annotations

import argparse
import importlib
import json
import tempfile
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from route74.cli.bot_latency import format_bot_latency_summary
from route74.cli.monitor import _exit_code, cmd_monitor_tick, format_monitor_summary, next_monitor_action
from route74.domain.prediction_sources import SOURCE_HISTORY_HEADWAY, SOURCE_TARGET_STOP_LIVE
from route74.domain.reporting import REPORT_WINDOWS
from route74.domain.runtime_sources import BOT_EVENT_USER_REPLY, BOT_EVENT_WATCH_EARLY, RUNTIME_SOURCE_WEB_APP
from route74.domain.yandex_history import DEFAULT_HISTORY_PERCENTILE
from route74.models import NOVOSIBIRSK_TZ, now_local
from route74.storage import (
    BotInteractionEvent,
    connect,
    init_db,
    insert_bot_interaction_event,
    update_collector_heartbeat,
)
from route74.storage.bot_latency import BotLatencySummary, summarize_bot_latency
from route74.storage.collector_runs import summarize_collector_runs, summarize_collector_runs_for_report_window
from route74.storage.forecast_backtest import ForecastBacktestResult, ForecastBacktestSummary
from route74.storage.forecast_health import summarize_forecast_health
from route74.storage.monitoring import (
    MONITOR_CRITICAL,
    MONITOR_OK,
    MONITOR_WARNING,
    MonitorIssue,
    MonitorSummary,
    summarize_monitor,
)
from route74.storage.models import ForecastReadinessSummary
from route74.watch_state import WatchStateSummary
from route74.cli import monitor as monitor_cli


_exit_code = monitor_cli._exit_code
cmd_monitor_tick = monitor_cli.cmd_monitor_tick
format_monitor_summary = monitor_cli.format_monitor_summary
next_monitor_action = monitor_cli.next_monitor_action


def main() -> None:
    _reload_monitor_cli()
    _assert_monitor_payload_guards()
    with tempfile.TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "monitor.sqlite"
        _assert_empty_db_flags_missing_collector(db_path)
        _assert_bot_latency_can_be_critical(db_path)
        _assert_bot_latency_p95_waits_for_sample_size(Path(temp_dir) / "bot-latency-small-sample.sqlite")
        _assert_bot_no_eta_replies_warn(Path(temp_dir) / "bot-no-eta.sqlite")
        _assert_bot_no_eta_replies_beats_collector(Path(temp_dir) / "bot-no-eta-vs-collector.sqlite")
        _assert_bot_latency_profile_scope(Path(temp_dir) / "bot-latency-profile.sqlite")
        _assert_bot_latency_persists_sanitized_errors(Path(temp_dir) / "latency-sanitized.sqlite")
        _assert_stale_collector_is_critical(Path(temp_dir) / "stale-collector.sqlite")
        _assert_history_readiness_routes_to_forecast_readiness(Path(temp_dir) / "history-readiness.sqlite")
        _assert_history_backtest_routes_to_forecast_backtest(Path(temp_dir) / "history-backtest.sqlite")
        _assert_zero_error_threshold_allows_clean_events(Path(temp_dir) / "zero-error-threshold.sqlite")
        _assert_bot_runtime_misses_warn(Path(temp_dir) / "bot-runtime-misses.sqlite")
        _assert_bot_runtime_calibration_late_risk_warns(Path(temp_dir) / "bot-runtime-calibration.sqlite")
        _assert_bot_runtime_source_calibration_late_risk_warns(Path(temp_dir) / "bot-runtime-source-calibration.sqlite")
        _assert_bot_runtime_pending_warns(Path(temp_dir) / "bot-runtime-pending.sqlite")
        _assert_bot_runtime_pending_uses_oldest_profile(Path(temp_dir) / "bot-runtime-pending-profile.sqlite")
        _assert_bot_runtime_monitor_ignores_watch_events(Path(temp_dir) / "bot-runtime-watch-events.sqlite")
        _assert_bot_runtime_p50_error_warns(Path(temp_dir) / "bot-runtime-p50.sqlite")
        _assert_bot_runtime_misses_are_profile_scoped(Path(temp_dir) / "bot-runtime-profile-misses.sqlite")
        _assert_bot_runtime_guardrail_unavailable_warns(Path(temp_dir) / "bot-runtime-guardrail.sqlite")
        _assert_history_readiness_warns(Path(temp_dir) / "history-readiness.sqlite")
        _assert_history_readiness_monitor_action()
        _assert_history_readiness_routes_to_forecast_coverage()
        _assert_stale_bot_latency_warns(Path(temp_dir) / "stale-monitor.sqlite")
        _assert_bot_latency_rejects_invalid_hours(Path(temp_dir) / "invalid-latency-hours.sqlite")
        _assert_monitor_rejects_invalid_thresholds(Path(temp_dir) / "invalid-monitor-thresholds.sqlite")
        _assert_monitor_cli_rejects_invalid_threshold_order(Path(temp_dir) / "invalid-monitor-cli.sqlite")
        _assert_forecast_health_rejects_invalid_inputs(Path(temp_dir) / "invalid-forecast-health.sqlite")
        _assert_malformed_bot_latency_rows_are_ignored(Path(temp_dir) / "bot-latency.sqlite")
        _assert_malformed_bot_latency_warns(Path(temp_dir) / "malformed-latency-monitor.sqlite")
        _assert_malformed_collector_runs_are_ignored(Path(temp_dir) / "collector-runs.sqlite")
        _assert_collector_runs_reject_invalid_windows(Path(temp_dir) / "invalid-collector-windows.sqlite")
    print("OK | monitor smoke passed")


def _reload_monitor_cli() -> None:
    global _exit_code, cmd_monitor_tick, format_monitor_summary, next_monitor_action
    reloaded = importlib.reload(monitor_cli)
    _exit_code = reloaded._exit_code
    cmd_monitor_tick = reloaded.cmd_monitor_tick
    format_monitor_summary = reloaded.format_monitor_summary
    next_monitor_action = reloaded.next_monitor_action


def _assert_empty_db_flags_missing_collector(db_path: Path) -> None:
    with connect(db_path) as connection:
        init_db(connection)
        summary = summarize_monitor(connection, db_path=db_path)
    _assert_equal(summary.status, MONITOR_CRITICAL)
    _assert_contains(format_monitor_summary(summary, db_path), "monitor status=critical")
    _assert_contains(format_monitor_summary(summary, db_path), 'next="route74 forecast-health"')
    _assert_issue(summary, "collector", MONITOR_CRITICAL)
    _assert_issue(summary, "yandex_canary")


def _assert_monitor_payload_guards() -> None:
    issue = MonitorIssue(MONITOR_WARNING, "collector", "not fresh")
    _assert_equal(issue.severity, MONITOR_WARNING)

    summary = MonitorSummary(
        db=object(),  # type: ignore[arg-type]
        forecast=object(),  # type: ignore[arg-type]
        latency=object(),  # type: ignore[arg-type]
        issues=(),
    )
    _assert_equal(summary.status, MONITOR_OK)
    _assert_value_error(
        lambda: MonitorIssue("broken", "collector", "not fresh"),
        "severity",
    )
    _assert_value_error(
        lambda: MonitorIssue(MONITOR_WARNING, " collector ", "not fresh"),
        "key",
    )
    _assert_value_error(
        lambda: MonitorIssue(MONITOR_WARNING, "collector", ""),
        "message",
    )
    _assert_value_error(
        lambda: MonitorIssue(MONITOR_WARNING, "collector", "not fresh", profile_key="morning\nbad"),
        "profile key",
    )
    _assert_value_error(
        lambda: MonitorSummary(
            db=object(),  # type: ignore[arg-type]
            forecast=object(),  # type: ignore[arg-type]
            latency=object(),  # type: ignore[arg-type]
            issues=[issue],  # type: ignore[arg-type]
        ),
        "tuple",
    )
    _assert_value_error(
        lambda: MonitorSummary(
            db=object(),  # type: ignore[arg-type]
            forecast=object(),  # type: ignore[arg-type]
            latency=object(),  # type: ignore[arg-type]
            issues=(object(),),  # type: ignore[arg-type]
        ),
        "MonitorIssue",
    )
    _assert_value_error(
        lambda: MonitorSummary(
            db=object(),  # type: ignore[arg-type]
            forecast=object(),  # type: ignore[arg-type]
            latency=object(),  # type: ignore[arg-type]
            issues=(),
            readiness=object(),  # type: ignore[arg-type]
        ),
        "ForecastReadinessSummary",
    )
    _assert_equal(next_monitor_action(summary), "route74 monitor-tick --fail-on critical")
    _assert_equal(
        next_monitor_action(
            MonitorSummary(
                db=object(),  # type: ignore[arg-type]
                forecast=object(),  # type: ignore[arg-type]
                latency=object(),  # type: ignore[arg-type]
                issues=(MonitorIssue(MONITOR_WARNING, "bot_runtime_misses", "misses", profile_key="morning"),),
            )
        ),
        "route74 support-report --profile morning",
    )
    _assert_equal(
        next_monitor_action(
            MonitorSummary(
                db=object(),  # type: ignore[arg-type]
                forecast=object(),  # type: ignore[arg-type]
                latency=object(),  # type: ignore[arg-type]
                issues=(MonitorIssue(MONITOR_WARNING, "bot_latency_errors", "errors", profile_key="morning"),),
            ),
            profile_key="morning",
        ),
        "route74 runtime-latency --hours 24 --profile morning --event-kind user_reply",
    )
    _assert_equal(
        next_monitor_action(
            MonitorSummary(
                db=object(),  # type: ignore[arg-type]
                forecast=object(),  # type: ignore[arg-type]
                latency=object(),  # type: ignore[arg-type]
                issues=(MonitorIssue(MONITOR_WARNING, "bot_no_eta_replies", "no ETA", profile_key="evening"),),
            ),
            profile_key="evening",
        ),
        "route74 runtime-latency --hours 24 --profile evening --event-kind user_reply",
    )
    _assert_equal(
        next_monitor_action(
            MonitorSummary(
                db=object(),  # type: ignore[arg-type]
                forecast=object(),  # type: ignore[arg-type]
                latency=object(),  # type: ignore[arg-type]
                issues=(
                    MonitorIssue(
                        MONITOR_WARNING,
                        "bot_runtime_guardrail_unavailable",
                        "guardrail",
                        profile_key="morning",
                    ),
                ),
            )
        ),
        "route74 support-report --profile morning",
    )
    _assert_equal(
        next_monitor_action(
            MonitorSummary(
                db=object(),  # type: ignore[arg-type]
                forecast=object(),  # type: ignore[arg-type]
                latency=object(),  # type: ignore[arg-type]
                issues=(
                    MonitorIssue(MONITOR_WARNING, "bot_runtime_pending", "pending", profile_key="morning"),
                    MonitorIssue(MONITOR_WARNING, "bot_runtime_pending", "pending", profile_key="evening"),
                ),
            ),
            profile_key="morning",
        ),
        "route74 prediction-evaluate --window weekday_morning_09_12",
    )
    _assert_equal(
        next_monitor_action(
            MonitorSummary(
                db=object(),  # type: ignore[arg-type]
                forecast=object(),  # type: ignore[arg-type]
                latency=object(),  # type: ignore[arg-type]
                issues=(MonitorIssue(MONITOR_WARNING, "bot_runtime_pending", "pending", profile_key="unknown"),),
            )
        ),
        "route74 runtime-events --hours 24 --limit 8 --event-kind user_reply",
    )
    _assert_equal(
        next_monitor_action(
            MonitorSummary(
                db=object(),  # type: ignore[arg-type]
                forecast=object(),  # type: ignore[arg-type]
                latency=object(),  # type: ignore[arg-type]
                issues=(
                    MonitorIssue(MONITOR_CRITICAL, "db_integrity", "db failed"),
                    MonitorIssue(MONITOR_WARNING, "bot_runtime_pending", "pending", profile_key="morning"),
                ),
            )
        ),
        "route74 db-health",
    )
    _assert_equal(
        next_monitor_action(
            MonitorSummary(
                db=object(),  # type: ignore[arg-type]
                forecast=object(),  # type: ignore[arg-type]
                latency=object(),  # type: ignore[arg-type]
                issues=(MonitorIssue(MONITOR_WARNING, "bot_runtime_pending", "pending"),),
            )
        ),
        "route74 runtime-events --hours 24 --limit 8 --event-kind user_reply",
    )
    _assert_equal(
        next_monitor_action(
            MonitorSummary(
                db=object(),  # type: ignore[arg-type]
                forecast=object(),  # type: ignore[arg-type]
                latency=object(),  # type: ignore[arg-type]
                issues=(MonitorIssue(MONITOR_WARNING, "bot_runtime_pending", "pending", profile_key="morning"),),
            )
        ),
        "route74 prediction-evaluate --window weekday_morning_09_12",
    )
    readiness_issue_summary = MonitorSummary(
        db=SimpleNamespace(healthy=True),
        forecast=SimpleNamespace(
            ready=True,
            canary=SimpleNamespace(status="ok"),
            windows=(
                SimpleNamespace(
                    window_key="weekday_morning_09_12",
                    profile_key="morning",
                    status="insufficient_bucket_coverage",
                ),
            ),
        ),
        latency=SimpleNamespace(
            total_events=0,
            error_events=0,
            error_rate_percent=0,
            no_eta_events=0,
            no_eta_rate_percent=0,
        ),
        issues=(MonitorIssue(MONITOR_WARNING, "history_readiness", "history not ready", profile_key="morning"),),
        runtime=None,
        readiness=_history_readiness_summary(ready=False),
    )
    _assert_equal(
        next_monitor_action(readiness_issue_summary, profile_key="morning"),
        "route74 forecast-coverage --window weekday_morning_09_12",
    )
    readiness_formatted = format_monitor_summary(
        _monitor_summary(readiness=_history_readiness_summary(ready=False)),
        "db.sqlite",
        profile_key="morning",
    )
    _assert_contains(readiness_formatted, "history:not_ready")
    _assert_contains(readiness_formatted, "history_window:weekday_morning_09_12")
    _assert_contains(readiness_formatted, "history_bucket:+/-30m")
    _assert_contains(readiness_formatted, "history_samples:2/20")
    _assert_contains(readiness_formatted, "history_days:1/3")
    backtest_issue_summary = _monitor_summary(
        issues=(MonitorIssue(MONITOR_WARNING, "history_backtest", "miss=3/6", profile_key="morning"),)
    )
    _assert_equal(
        next_monitor_action(backtest_issue_summary, profile_key="morning"),
        "route74 forecast-backtest --window weekday_morning_09_12",
    )
    backtest_formatted = format_monitor_summary(
        _monitor_summary(backtest=_history_backtest_summary(miss_rate_percent=50)),
        "db.sqlite",
        profile_key="morning",
    )
    _assert_contains(backtest_formatted, "history_backtest:p80")
    _assert_contains(backtest_formatted, "history_backtest_eval:6/8")
    _assert_contains(backtest_formatted, "history_backtest_miss:3(50%)")
    scoped_summary = _monitor_summary(
        issues=(
            MonitorIssue(MONITOR_WARNING, "bot_runtime_pending", "morning pending", profile_key="morning"),
            MonitorIssue(MONITOR_WARNING, "bot_runtime_pending", "evening pending", profile_key="evening"),
            MonitorIssue(MONITOR_WARNING, "bot_latency_errors", "global errors"),
        )
    )
    _assert_equal(
        next_monitor_action(scoped_summary, profile_key="evening"),
        "route74 prediction-evaluate --window weekday_evening_19_22",
    )
    scoped_formatted = format_monitor_summary(scoped_summary, "db.sqlite", profile_key="evening")
    _assert_contains(scoped_formatted, "monitor profile=evening status=warning")
    _assert_contains(scoped_formatted, "bot_runtime_pending profile=evening")
    _assert_contains(scoped_formatted, "bot_latency_errors")
    _assert_not_contains(scoped_formatted, "morning pending")
    morning_only_summary = _monitor_summary(
        issues=(MonitorIssue(MONITOR_WARNING, "bot_runtime_pending", "morning pending", profile_key="morning"),)
    )
    _assert_equal(_exit_code(morning_only_summary, fail_on=MONITOR_WARNING, profile_key="evening"), 0)
    _assert_equal(_exit_code(morning_only_summary, fail_on=MONITOR_WARNING, profile_key="morning"), 1)
    global_critical_summary = _monitor_summary(
        issues=(
            MonitorIssue(MONITOR_WARNING, "bot_runtime_pending", "evening pending", profile_key="evening"),
            MonitorIssue(MONITOR_CRITICAL, "collector", "collector stale"),
        )
    )
    _assert_equal(_exit_code(global_critical_summary, fail_on=MONITOR_CRITICAL, profile_key="evening"), 2)
    _assert_equal(
        next_monitor_action(
            _monitor_summary(
                issues=(
                    MonitorIssue(MONITOR_WARNING, "bot_runtime_pending", "morning pending", profile_key="morning"),
                    MonitorIssue(MONITOR_WARNING, "bot_runtime_pending", "evening pending", profile_key="evening"),
                )
            ),
            profile_key="evening",
        ),
        "route74 prediction-evaluate --window weekday_evening_19_22",
    )
    _assert_equal(
        next_monitor_action(
            _monitor_summary(
                issues=(
                    MonitorIssue(MONITOR_WARNING, "bot_runtime_pending", "evening pending", profile_key="evening"),
                    MonitorIssue(MONITOR_CRITICAL, "collector", "collector stale"),
                )
            ),
            profile_key="evening",
        ),
        "route74 forecast-health",
    )
    _assert_equal(
        next_monitor_action(
            MonitorSummary(
                db=object(),  # type: ignore[arg-type]
                forecast=object(),  # type: ignore[arg-type]
                latency=object(),  # type: ignore[arg-type]
                issues=(MonitorIssue(MONITOR_WARNING, "bot_runtime_p50_error", "p50", profile_key="morning"),),
            )
        ),
        "route74 support-report --profile morning",
    )
    _assert_equal(
        next_monitor_action(
            MonitorSummary(
                db=object(),  # type: ignore[arg-type]
                forecast=object(),  # type: ignore[arg-type]
                latency=object(),  # type: ignore[arg-type]
                issues=(MonitorIssue(MONITOR_WARNING, "forecast_morning", "not ready", profile_key="morning"),),
            )
        ),
        "route74 support-report --profile morning",
    )
    _assert_equal(
        next_monitor_action(
            MonitorSummary(
                db=object(),  # type: ignore[arg-type]
                forecast=object(),  # type: ignore[arg-type]
                latency=object(),  # type: ignore[arg-type]
                issues=(
                    MonitorIssue(MONITOR_WARNING, "bot_runtime_misses", "misses"),
                    MonitorIssue(MONITOR_CRITICAL, "collector", "stale"),
                ),
            )
        ),
        "route74 forecast-health",
    )
    _assert_equal(
        next_monitor_action(
            MonitorSummary(
                db=object(),  # type: ignore[arg-type]
                forecast=object(),  # type: ignore[arg-type]
                latency=object(),  # type: ignore[arg-type]
                issues=(
                    MonitorIssue(MONITOR_WARNING, "bot_runtime_pending", "pending", profile_key="morning"),
                    MonitorIssue(MONITOR_CRITICAL, "collector", "stale"),
                ),
            )
        ),
        "route74 forecast-health",
    )
    _assert_equal(
        next_monitor_action(
            MonitorSummary(
                db=object(),  # type: ignore[arg-type]
                forecast=object(),  # type: ignore[arg-type]
                latency=object(),  # type: ignore[arg-type]
                issues=(MonitorIssue(MONITOR_WARNING, "yandex_canary", "warning"),),
            )
        ),
        "./bin/smoke-yandex",
    )
    summary = _monitor_summary()
    watch_state_path = Path("data/custom-watches.json")
    watch_state = _watch_state_summary(
        status=MONITOR_CRITICAL,
        file_status="unreadable",
        error_type="JSONDecodeError",
    )
    _assert_equal(
        next_monitor_action(summary, watch_state=watch_state, watch_state_path=watch_state_path),
        "route74 watch-state --path data/custom-watches.json",
    )
    with tempfile.TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "watch-monitor.sqlite"
        with connect(db_path) as connection:
            init_db(connection)
            real_summary = summarize_monitor(connection, db_path=db_path)
        formatted = format_monitor_summary(
            real_summary,
            "db.sqlite",
            watch_state=watch_state,
            watch_state_path=watch_state_path,
        )
    _assert_contains(formatted, "monitor status=critical")
    _assert_contains(formatted, "watch:critical")
    _assert_contains(formatted, "watch_state_file")
    _assert_contains(formatted, 'next="route74 watch-state --path data/custom-watches.json"')

    runtime_error_watch = _watch_state_summary(
        status=MONITOR_WARNING,
        runtime_error_count=2,
        runtime_error_records=1,
        latest_error_at=datetime(2026, 6, 4, 8, 58, tzinfo=NOVOSIBIRSK_TZ),
        runtime_error_types=("RuntimeError",),
    )
    runtime_error_formatted = format_monitor_summary(
        summary,
        "db.sqlite",
        watch_state=runtime_error_watch,
        watch_state_path=watch_state_path,
    )
    _assert_contains(runtime_error_formatted, "watch_errors:2")
    _assert_contains(runtime_error_formatted, "watch_latest_error_age:2m")
    _assert_contains(runtime_error_formatted, "watch_error_types:RuntimeError")
    _assert_contains(runtime_error_formatted, "watch_state_runtime_error")
    _assert_contains(runtime_error_formatted, 'next="route74 watch-state --path data/custom-watches.json"')


def _assert_bot_latency_can_be_critical(db_path: Path) -> None:
    current_time = now_local()
    with connect(db_path) as connection:
        init_db(connection)
        _mark_collector_ok(connection, current_time)
        insert_bot_interaction_event(
            connection,
            BotInteractionEvent(
                received_at=current_time - timedelta(minutes=1),
                chat_id=101,
                update_type="message",
                command="🎯 Поймать 74",
                event_kind=BOT_EVENT_USER_REPLY,
                reply_source="error",
                yandex_source_method="none",
                forecast_ms=20_000,
                render_ms=1,
                send_ms=1,
                total_ms=20_002,
                status="error",
                error="send_error: RuntimeError: local smoke failure",
            ),
        )
        for index in range(2):
            insert_bot_interaction_event(
                connection,
                BotInteractionEvent(
                    received_at=current_time - timedelta(minutes=2 + index),
                    chat_id=201 + index,
                    update_type="message",
                    command="🎯 Поймать 74",
                    event_kind=BOT_EVENT_USER_REPLY,
                    reply_source="yandex",
                    yandex_source_method="vehicle_prediction",
                    forecast_ms=20_000,
                    render_ms=1,
                    send_ms=1,
                    total_ms=20_002,
                    status="ok",
                ),
            )
        connection.commit()
        summary = summarize_monitor(
            connection,
            db_path=db_path,
            warn_latency_ms=1_000,
            critical_latency_ms=2_000,
            warn_error_rate_percent=1,
            critical_error_rate_percent=1,
        )
    _assert_equal(summary.status, MONITOR_CRITICAL)
    _assert_issue(summary, "bot_latency_errors")
    _assert_issue(summary, "bot_latency_p95")
    summary_text = format_monitor_summary(summary, db_path)
    _assert_contains(summary_text, "top_error=send_error")

    with connect(db_path) as connection:
        latency = summarize_bot_latency(connection, hours=24, current_time=current_time)
    formatted = format_bot_latency_summary(latency, db_path)
    _assert_contains(formatted, "updates=message:3")
    _assert_contains(formatted, "reply_sources=yandex:2, error:1")
    _assert_contains(formatted, "error_categories=send_error:1")
    _assert_contains(formatted, "error_reasons=send_error: RuntimeError: local smoke failure:1")


def _assert_bot_no_eta_replies_warn(db_path: Path) -> None:
    current_time = now_local()
    with connect(db_path) as connection:
        init_db(connection)
        _mark_collector_ok(connection, current_time)
        for index, reply_source in enumerate(("no_eta", "no_eta", "no_eta", "yandex")):
            insert_bot_interaction_event(
                connection,
                BotInteractionEvent(
                    received_at=current_time - timedelta(minutes=index),
                    chat_id=101 + index,
                    update_type="message",
                    command="🎯 Поймать 74",
                    event_kind=BOT_EVENT_USER_REPLY,
                    reply_source=reply_source,
                    yandex_source_method="none" if reply_source == "no_eta" else "vehicle_prediction",
                    forecast_ms=20,
                    render_ms=1,
                    send_ms=1,
                    total_ms=22,
                    status="ok",
                    no_eta_reason="yandex_no_target+history_insufficient" if reply_source == "no_eta" else "",
                ),
            )
        connection.commit()
        summary = summarize_monitor(connection, db_path=db_path)
        latency = summarize_bot_latency(connection, hours=24, current_time=current_time)
    _assert_equal(latency.no_eta_events, 3)
    _assert_equal(latency.no_eta_rate_percent, 75)
    _assert_equal(latency.no_eta_reasons[0].key, "yandex_no_target+history_insufficient")
    _assert_issue(summary, "bot_no_eta_replies", MONITOR_WARNING)
    formatted = format_monitor_summary(summary, db_path)
    _assert_contains(formatted, "bot_no_eta:3(75%)")
    _assert_contains(formatted, "top_reason=yandex_no_target+history_insufficient")
    _assert_contains(format_bot_latency_summary(latency, db_path), "no_eta=3(75%)")
    _assert_contains(
        format_bot_latency_summary(latency, db_path),
        "no_eta_reasons=yandex_no_target+history_insufficient:3",
    )


def _assert_bot_latency_p95_waits_for_sample_size(db_path: Path) -> None:
    current_time = now_local()
    with connect(db_path) as connection:
        init_db(connection)
        _mark_collector_ok(connection, current_time)
        insert_bot_interaction_event(
            connection,
            BotInteractionEvent(
                received_at=current_time - timedelta(minutes=1),
                chat_id=301,
                update_type="message",
                command="🎯 Поймать 74",
                event_kind=BOT_EVENT_USER_REPLY,
                reply_source="no_eta",
                yandex_source_method="none",
                forecast_ms=19_965,
                render_ms=0,
                send_ms=0,
                total_ms=19_965,
                status="ok",
                no_eta_reason="history_insufficient",
            ),
        )
        connection.commit()
        summary = summarize_monitor(
            connection,
            db_path=db_path,
            warn_latency_ms=1_000,
            critical_latency_ms=2_000,
        )
    _assert_no_issue(summary, "bot_latency_p95")


def _assert_bot_no_eta_replies_beats_collector(db_path: Path) -> None:
    summary = _monitor_summary(
        issues=(
            MonitorIssue(MONITOR_WARNING, "collector", "collector stale"),
            MonitorIssue(MONITOR_WARNING, "bot_no_eta_replies", "no_eta=3/4(75%)", profile_key="evening"),
        )
    )
    _assert_equal(
        next_monitor_action(summary, profile_key="evening"),
        "route74 runtime-latency --hours 24 --profile evening --event-kind user_reply",
    )
    formatted = format_monitor_summary(summary, db_path, profile_key="evening")
    _assert_contains(formatted, 'next="route74 runtime-latency --hours 24 --profile evening --event-kind user_reply"')


def _assert_bot_latency_profile_scope(db_path: Path) -> None:
    current_time = now_local()
    with connect(db_path) as connection:
        init_db(connection)
        _mark_collector_ok(connection, current_time)
        for index, reply_source in enumerate(("no_eta", "no_eta", "no_eta", "yandex")):
            insert_bot_interaction_event(
                connection,
                BotInteractionEvent(
                    received_at=current_time - timedelta(minutes=index),
                    chat_id=201 + index,
                    update_type="message",
                    command="🎯 Поймать 74",
                    event_kind=BOT_EVENT_USER_REPLY,
                    reply_source=reply_source,
                    yandex_source_method="none" if reply_source == "no_eta" else "vehicle_prediction",
                    forecast_ms=20,
                    render_ms=1,
                    send_ms=1,
                    total_ms=22,
                    status="ok",
                    profile_key="morning",
                    no_eta_reason="yandex_no_target+history_insufficient" if reply_source == "no_eta" else "",
                ),
            )
        for index in range(3):
            insert_bot_interaction_event(
                connection,
                BotInteractionEvent(
                    received_at=current_time - timedelta(minutes=10 + index),
                    chat_id=301 + index,
                    update_type="message",
                    command="Вечер",
                    event_kind=BOT_EVENT_USER_REPLY,
                    reply_source="yandex",
                    yandex_source_method="vehicle_prediction",
                    forecast_ms=20,
                    render_ms=1,
                    send_ms=1,
                    total_ms=22,
                    status="ok",
                    profile_key="evening",
                ),
            )
        connection.commit()
        morning_latency = summarize_bot_latency(
            connection,
            hours=24,
            current_time=current_time,
            profile_key="morning",
        )
        evening_latency = summarize_bot_latency(
            connection,
            hours=24,
            current_time=current_time,
            profile_key="evening",
        )
        morning_monitor = summarize_monitor(
            connection,
            db_path=db_path,
            current_time=current_time,
            profile_key="morning",
        )
        evening_monitor = summarize_monitor(
            connection,
            db_path=db_path,
            current_time=current_time,
            profile_key="evening",
        )
    _assert_equal(morning_latency.profile_key, "morning")
    _assert_equal(morning_latency.total_events, 4)
    _assert_equal(morning_latency.no_eta_events, 3)
    _assert_equal(morning_latency.no_eta_rate_percent, 75)
    _assert_equal(morning_latency.no_eta_reasons[0].key, "yandex_no_target+history_insufficient")
    _assert_equal(evening_latency.total_events, 3)
    _assert_equal(evening_latency.no_eta_events, 0)
    _assert_contains(format_bot_latency_summary(morning_latency, db_path), "runtime latency profile=morning")
    _assert_issue(morning_monitor, "bot_no_eta_replies", MONITOR_WARNING)
    _assert_no_issue(evening_monitor, "bot_no_eta_replies")
    morning_formatted = format_monitor_summary(morning_monitor, db_path, profile_key="morning")
    evening_formatted = format_monitor_summary(evening_monitor, db_path, profile_key="evening")
    _assert_contains(morning_formatted, "monitor profile=morning")
    _assert_contains(
        morning_formatted, 'next="route74 runtime-latency --hours 24 --profile morning --event-kind user_reply"'
    )
    _assert_contains(morning_formatted, "bot_events:4")
    _assert_contains(morning_formatted, "bot_no_eta:3(75%)")
    _assert_contains(morning_formatted, "bot_no_eta_replies profile=morning")
    _assert_contains(morning_formatted, "top_reason=yandex_no_target+history_insufficient")
    _assert_contains(evening_formatted, "bot_events:3")
    _assert_contains(evening_formatted, "bot_no_eta:0(0%)")
    _assert_not_contains(evening_formatted, "bot_no_eta_replies")


def _assert_bot_latency_persists_sanitized_errors(db_path: Path) -> None:
    current_time = now_local()
    raw_token = "123456:" + "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdef"
    with connect(db_path) as connection:
        init_db(connection)
        insert_bot_interaction_event(
            connection,
            BotInteractionEvent(
                received_at=current_time,
                chat_id=101,
                update_type="message",
                command="🎯 Поймать 74",
                event_kind=BOT_EVENT_USER_REPLY,
                reply_source="none",
                yandex_source_method="none",
                forecast_ms=1,
                render_ms=1,
                send_ms=1,
                total_ms=3,
                status="error",
                error=f"failed /home/vladimir/work-projects/74/.env token={raw_token}",
            ),
        )
        row = connection.execute("SELECT error FROM bot_interaction_events").fetchone()
        summary = summarize_bot_latency(connection, hours=1, current_time=current_time)
    formatted = format_bot_latency_summary(summary, db_path)
    if row is None:
        raise AssertionError("expected bot latency row")
    stored_error = str(row["error"])
    _assert_contains(stored_error, "token=<redacted>")
    _assert_contains(stored_error, "<path>")
    _assert_not_contains(stored_error, raw_token)
    _assert_not_contains(stored_error, "/home/vladimir")
    _assert_contains(formatted, "token=<redacted>")


def _assert_stale_collector_is_critical(db_path: Path) -> None:
    current_time = now_local()
    with connect(db_path) as connection:
        init_db(connection)
        update_collector_heartbeat(
            connection,
            name="yandex-collect",
            pid=123,
            profile_filter="all",
            last_status="ok",
            last_message="collector alive before smoke",
            updated_at=current_time - timedelta(hours=3),
        )
        summary = summarize_monitor(connection, db_path=db_path)
    _assert_equal(summary.status, MONITOR_CRITICAL)
    _assert_issue(summary, "collector", MONITOR_CRITICAL)


def _assert_history_readiness_routes_to_forecast_readiness(db_path: Path) -> None:
    current_time = datetime(2026, 6, 8, 9, 30, tzinfo=NOVOSIBIRSK_TZ)
    readiness = _history_readiness_summary(ready=False)
    with connect(db_path) as connection:
        init_db(connection)
        with (
            patch(
                "route74.storage.monitoring.summarize_db_health_readonly", return_value=SimpleNamespace(healthy=True)
            ),
            patch(
                "route74.storage.monitoring.summarize_forecast_health",
                return_value=SimpleNamespace(
                    ready=True,
                    collector=SimpleNamespace(healthy=True, status="ok", name="yandex-collect", message="ok"),
                    canary=SimpleNamespace(healthy=True, status="ok"),
                    windows=(),
                ),
            ),
            patch(
                "route74.storage.monitoring.summarize_bot_latency",
                return_value=BotLatencySummary(
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
                    profile_key=None,
                ),
            ),
            patch(
                "route74.storage.monitoring.summarize_yandex_forecast_readiness",
                return_value=readiness,
            ),
            patch(
                "route74.storage.monitoring.summarize_bot_runtime_predictions",
                return_value=SimpleNamespace(
                    total=0,
                    evaluated=0,
                    pending=0,
                    misses=0,
                    miss_rate_percent=0,
                    guardrail_unavailable=0,
                    guardrail_unavailable_percent=0,
                    p50_abs_error_minutes=None,
                    by_profile=(),
                    by_profile_source=(),
                ),
            ),
            patch(
                "route74.storage.monitoring.summarize_bot_runtime_calibration",
                return_value=SimpleNamespace(
                    status="insufficient",
                    suggested_buffer_minutes=0,
                    by_profile=(),
                    by_profile_source=(),
                ),
            ),
        ):
            summary = summarize_monitor(
                connection,
                db_path=db_path,
                profile_key="morning",
                current_time=current_time,
            )
    _assert_equal(summary.status, MONITOR_WARNING)
    _assert_issue(summary, "history_readiness", MONITOR_WARNING)
    _assert_equal(summary.readiness, readiness)
    formatted = format_monitor_summary(summary, db_path, profile_key="morning")
    _assert_contains(formatted, "monitor profile=morning status=warning")
    _assert_contains(formatted, "history:not_ready")
    _assert_contains(formatted, "history_readiness profile=morning")
    _assert_contains(formatted, 'next="route74 forecast-readiness --window weekday_morning_09_12"')
    _assert_equal(
        next_monitor_action(summary, profile_key="morning"),
        "route74 forecast-readiness --window weekday_morning_09_12",
    )


def _assert_history_backtest_routes_to_forecast_backtest(db_path: Path) -> None:
    current_time = datetime(2026, 6, 8, 9, 30, tzinfo=NOVOSIBIRSK_TZ)
    readiness = _history_readiness_summary(ready=True)
    backtest = _history_backtest_summary(miss_rate_percent=50)
    with connect(db_path) as connection:
        init_db(connection)
        with (
            patch(
                "route74.storage.monitoring.summarize_db_health_readonly", return_value=SimpleNamespace(healthy=True)
            ),
            patch(
                "route74.storage.monitoring.summarize_forecast_health",
                return_value=SimpleNamespace(
                    ready=True,
                    collector=SimpleNamespace(healthy=True, status="ok", name="yandex-collect", message="ok"),
                    canary=SimpleNamespace(healthy=True, status="ok"),
                    windows=(),
                ),
            ),
            patch(
                "route74.storage.monitoring.summarize_bot_latency",
                return_value=BotLatencySummary(
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
                    profile_key=None,
                ),
            ),
            patch("route74.storage.monitoring.summarize_yandex_forecast_readiness", return_value=readiness),
            patch("route74.storage.monitoring.summarize_yandex_forecast_backtest", return_value=backtest),
            patch(
                "route74.storage.monitoring.summarize_bot_runtime_predictions",
                return_value=SimpleNamespace(
                    total=0,
                    evaluated=0,
                    pending=0,
                    misses=0,
                    miss_rate_percent=0,
                    guardrail_unavailable=0,
                    guardrail_unavailable_percent=0,
                    p50_abs_error_minutes=None,
                    by_profile=(),
                    by_profile_source=(),
                ),
            ),
            patch(
                "route74.storage.monitoring.summarize_bot_runtime_calibration",
                return_value=SimpleNamespace(
                    status="insufficient",
                    suggested_buffer_minutes=0,
                    by_profile=(),
                    by_profile_source=(),
                ),
            ),
        ):
            summary = summarize_monitor(
                connection,
                db_path=db_path,
                profile_key="morning",
                current_time=current_time,
            )
    _assert_equal(summary.status, MONITOR_WARNING)
    _assert_no_issue(summary, "history_readiness")
    _assert_issue(summary, "history_backtest", MONITOR_WARNING)
    _assert_equal(summary.backtest, backtest)
    formatted = format_monitor_summary(summary, db_path, profile_key="morning")
    _assert_contains(formatted, "history:ready")
    _assert_contains(formatted, "history_backtest:p80")
    _assert_contains(formatted, "history_backtest_miss:3(50%)")
    _assert_contains(formatted, "history_backtest profile=morning")
    _assert_contains(formatted, 'next="route74 forecast-backtest --window weekday_morning_09_12"')
    _assert_equal(
        next_monitor_action(summary, profile_key="morning"),
        "route74 forecast-backtest --window weekday_morning_09_12",
    )


def _assert_zero_error_threshold_allows_clean_events(db_path: Path) -> None:
    current_time = now_local()
    with connect(db_path) as connection:
        init_db(connection)
        insert_bot_interaction_event(
            connection,
            BotInteractionEvent(
                received_at=current_time - timedelta(minutes=1),
                chat_id=101,
                update_type="message",
                command="/start",
                event_kind=BOT_EVENT_USER_REPLY,
                reply_source="none",
                yandex_source_method="none",
                forecast_ms=1,
                render_ms=1,
                send_ms=1,
                total_ms=3,
                status="ok",
            ),
        )
        connection.commit()
        summary = summarize_monitor(
            connection,
            db_path=db_path,
            warn_error_rate_percent=0,
            critical_error_rate_percent=0,
        )
    _assert_no_issue(summary, "bot_latency_errors")


def _assert_stale_bot_latency_warns(db_path: Path) -> None:
    current_time = now_local()
    with connect(db_path) as connection:
        init_db(connection)
        insert_bot_interaction_event(
            connection,
            BotInteractionEvent(
                received_at=current_time - timedelta(hours=25),
                chat_id=101,
                update_type="message",
                command="/start",
                event_kind=BOT_EVENT_USER_REPLY,
                reply_source="none",
                yandex_source_method="none",
                forecast_ms=1,
                render_ms=1,
                send_ms=1,
                total_ms=3,
                status="ok",
            ),
        )
        connection.commit()
        summary = summarize_monitor(connection, db_path=db_path, latency_hours=24)
        latency = summarize_bot_latency(connection, hours=24, current_time=current_time)
    _assert_issue(summary, "bot_latency_stale")
    formatted = format_bot_latency_summary(latency, db_path)
    _assert_contains(formatted, "events=0")
    _assert_contains(formatted, "latest=")


def _assert_malformed_bot_latency_rows_are_ignored(db_path: Path) -> None:
    current_time = now_local()
    with connect(db_path) as connection:
        init_db(connection)
        _insert_bot_latency_row(
            connection,
            received_at="not-a-date",
            forecast_ms="10",
            send_ms="10",
            total_ms="10",
            status="error",
            error="bad timestamp",
        )
        _insert_bot_latency_row(
            connection,
            received_at=current_time.isoformat(),
            forecast_ms="bad",
            send_ms="-10",
            total_ms="bad",
            status="error",
            error="",
        )
        _insert_bot_latency_row(
            connection,
            received_at=current_time.isoformat(),
            forecast_ms="100",
            send_ms="50",
            total_ms="200",
            status="ok",
            error="",
        )
        _insert_bot_latency_row(
            connection,
            received_at=(current_time + timedelta(days=1)).isoformat(),
            forecast_ms="9999",
            send_ms="9999",
            total_ms="9999",
            status="error",
            error="future clock skew",
        )
        connection.commit()

        summary = summarize_bot_latency(connection, hours=1, current_time=current_time)

    _assert_equal(summary.hours, 1)
    _assert_equal(summary.total_events, 2)
    _assert_equal(summary.invalid_duration_events, 1)
    _assert_equal(summary.error_events, 1)
    _assert_equal(summary.latest_received_at, current_time)
    _assert_equal(summary.p95_total_ms, 200)
    _assert_equal(summary.p95_forecast_ms, 100)
    _assert_equal(summary.p95_send_ms, 50)
    _assert_equal(summary.p95_render_ms, 0)
    _assert_equal(summary.error_reasons[0].key, "unknown_error")
    formatted = format_bot_latency_summary(summary, db_path)
    _assert_contains(formatted, "invalid_durations=1")
    _assert_contains(formatted, "p95_followup=0ms")


def _assert_malformed_bot_latency_warns(db_path: Path) -> None:
    current_time = now_local()
    with connect(db_path) as connection:
        init_db(connection)
        _insert_bot_latency_row(
            connection,
            received_at=current_time.isoformat(),
            forecast_ms="bad",
            send_ms="50",
            total_ms="200",
            status="ok",
            error="",
        )
        connection.commit()
        summary = summarize_monitor(connection, db_path=db_path)

    _assert_issue(summary, "bot_latency_malformed")


def _assert_bot_latency_rejects_invalid_hours(db_path: Path) -> None:
    with connect(db_path) as connection:
        init_db(connection)
        _assert_value_error(
            lambda: summarize_bot_latency(connection, hours=0),
            "hours must be a positive integer",
        )
        _assert_value_error(
            lambda: summarize_bot_latency(connection, hours=True),  # type: ignore[arg-type]
            "hours must be a positive integer",
        )
        _assert_value_error(
            lambda: summarize_bot_latency(connection, hours=1, profile_key="night"),
            "profile_key must be one of",
        )


def _assert_monitor_rejects_invalid_thresholds(db_path: Path) -> None:
    with connect(db_path) as connection:
        init_db(connection)
        _assert_value_error(
            lambda: summarize_monitor(connection, db_path=db_path, latency_hours=True),  # type: ignore[arg-type]
            "latency_hours must be a positive integer",
        )
        _assert_value_error(
            lambda: summarize_monitor(connection, db_path=db_path, warn_latency_ms=0),
            "warn_latency_ms must be a positive integer",
        )
        _assert_value_error(
            lambda: summarize_monitor(
                connection,
                db_path=db_path,
                warn_latency_ms=5_000,
                critical_latency_ms=1_000,
            ),
            "latency critical threshold",
        )
        _assert_value_error(
            lambda: summarize_monitor(
                connection,
                db_path=db_path,
                warn_error_rate_percent=-1,
            ),
            "warn_error_rate_percent must be an integer from 0 to 100",
        )
        _assert_value_error(
            lambda: summarize_monitor(
                connection,
                db_path=db_path,
                critical_error_rate_percent=101,
            ),
            "critical_error_rate_percent must be an integer from 0 to 100",
        )
        _assert_value_error(
            lambda: summarize_monitor(
                connection,
                db_path=db_path,
                warn_error_rate_percent=30,
                critical_error_rate_percent=10,
            ),
            "error rate critical threshold",
        )
        _assert_value_error(
            lambda: summarize_monitor(connection, db_path=db_path, min_no_eta_events=0),
            "min_no_eta_events must be a positive integer",
        )
        _assert_value_error(
            lambda: summarize_monitor(connection, db_path=db_path, warn_no_eta_rate_percent=-1),
            "warn_no_eta_rate_percent must be an integer from 0 to 100",
        )
        _assert_value_error(
            lambda: summarize_monitor(connection, db_path=db_path, critical_no_eta_rate_percent=101),
            "critical_no_eta_rate_percent must be an integer from 0 to 100",
        )
        _assert_value_error(
            lambda: summarize_monitor(
                connection,
                db_path=db_path,
                warn_no_eta_rate_percent=90,
                critical_no_eta_rate_percent=50,
            ),
            "no ETA rate critical threshold",
        )
        _assert_value_error(
            lambda: summarize_monitor(connection, db_path=db_path, runtime_hours=True),  # type: ignore[arg-type]
            "runtime_hours must be a positive integer",
        )
        _assert_value_error(
            lambda: summarize_monitor(connection, db_path=db_path, min_bot_runtime_evaluated=0),
            "min_bot_runtime_evaluated must be a positive integer",
        )
        _assert_value_error(
            lambda: summarize_monitor(connection, db_path=db_path, warn_bot_miss_rate_percent=-1),
            "warn_bot_miss_rate_percent must be an integer from 0 to 100",
        )
        _assert_value_error(
            lambda: summarize_monitor(connection, db_path=db_path, critical_bot_miss_rate_percent=101),
            "critical_bot_miss_rate_percent must be an integer from 0 to 100",
        )
        _assert_value_error(
            lambda: summarize_monitor(
                connection,
                db_path=db_path,
                warn_bot_miss_rate_percent=90,
                critical_bot_miss_rate_percent=50,
            ),
            "bot miss rate critical threshold",
        )
        _assert_value_error(
            lambda: summarize_monitor(connection, db_path=db_path, warn_bot_p50_abs_error_minutes=0),
            "warn_bot_p50_abs_error_minutes must be a positive integer",
        )
        _assert_value_error(
            lambda: summarize_monitor(
                connection,
                db_path=db_path,
                warn_bot_p50_abs_error_minutes=8,
                critical_bot_p50_abs_error_minutes=4,
            ),
            "bot p50 absolute error critical threshold",
        )
        _assert_value_error(
            lambda: summarize_monitor(connection, db_path=db_path, warn_bot_pending_age_minutes=0),
            "warn_bot_pending_age_minutes must be a positive integer",
        )
        _assert_value_error(
            lambda: summarize_monitor(
                connection,
                db_path=db_path,
                warn_bot_pending_age_minutes=360,
                critical_bot_pending_age_minutes=120,
            ),
            "bot pending age critical threshold",
        )
        _assert_value_error(
            lambda: summarize_monitor(connection, db_path=db_path, warn_bot_guardrail_unavailable=0),
            "warn_bot_guardrail_unavailable must be a positive integer",
        )
        _assert_value_error(
            lambda: summarize_monitor(connection, db_path=db_path, profile_key="night"),
            "profile_key must be one of",
        )
        _assert_value_error(
            lambda: summarize_monitor(
                connection,
                db_path=db_path,
                warn_bot_guardrail_unavailable=3,
                critical_bot_guardrail_unavailable=1,
            ),
            "bot guardrail unavailable critical threshold",
        )


def _assert_monitor_cli_rejects_invalid_threshold_order(db_path: Path) -> None:
    args = argparse.Namespace(
        db=db_path,
        latency_hours=24,
        warn_latency_ms=5_000,
        critical_latency_ms=15_000,
        warn_error_rate=30,
        critical_error_rate=10,
        min_no_eta_events=3,
        warn_no_eta_rate=50,
        critical_no_eta_rate=80,
        runtime_hours=24,
        min_bot_evaluated=3,
        warn_bot_miss_rate=50,
        critical_bot_miss_rate=80,
        warn_bot_p50_error_minutes=4,
        critical_bot_p50_error_minutes=8,
        warn_bot_pending_age_minutes=120,
        critical_bot_pending_age_minutes=360,
        warn_bot_guardrail_unavailable=1,
        critical_bot_guardrail_unavailable=3,
        watch_state_path=Path("data/web_watches.json"),
        profile=None,
        fail_on="critical",
    )
    try:
        cmd_monitor_tick(args)
    except SystemExit as error:
        _assert_contains(str(error), "error rate critical threshold")
        return
    raise AssertionError("expected monitor CLI to reject invalid threshold order")


def _assert_bot_runtime_misses_warn(db_path: Path) -> None:
    current_time = now_local()
    with connect(db_path) as connection:
        init_db(connection)
        _insert_runtime_prediction(connection, sampled_at=current_time - timedelta(minutes=3), error_minutes=-2)
        _insert_runtime_prediction(connection, sampled_at=current_time - timedelta(minutes=2), error_minutes=-1)
        _insert_runtime_prediction(connection, sampled_at=current_time - timedelta(minutes=1), error_minutes=2)
        connection.commit()
        summary = summarize_monitor(
            connection,
            db_path=db_path,
            min_bot_runtime_evaluated=3,
            warn_bot_miss_rate_percent=50,
            critical_bot_miss_rate_percent=90,
            current_time=current_time,
        )
    _assert_issue(summary, "bot_runtime_misses", MONITOR_WARNING)
    formatted = format_monitor_summary(summary, db_path)
    _assert_contains(formatted, "bot_predictions:3")
    _assert_contains(formatted, "bot_miss:2(67%)")
    _assert_contains(formatted, "bot_p50_abs:2m")
    _assert_contains(formatted, "top_source=target_stop_live:2")


def _assert_bot_runtime_calibration_late_risk_warns(db_path: Path) -> None:
    current_time = now_local()
    with connect(db_path) as connection:
        init_db(connection)
        update_collector_heartbeat(
            connection,
            name="yandex-collect",
            pid=123,
            profile_filter="all",
            last_status="ok",
            last_message="collector alive before calibration smoke",
            updated_at=current_time - timedelta(minutes=1),
        )
        _insert_ok_canary_runs(connection, checked_at=current_time - timedelta(minutes=1))
        _insert_runtime_prediction(connection, sampled_at=current_time - timedelta(minutes=3), error_minutes=-1)
        _insert_runtime_prediction(connection, sampled_at=current_time - timedelta(minutes=2), error_minutes=1)
        _insert_runtime_prediction(connection, sampled_at=current_time - timedelta(minutes=1), error_minutes=1)
        connection.commit()
        summary = summarize_monitor(
            connection,
            db_path=db_path,
            min_bot_runtime_evaluated=3,
            warn_bot_miss_rate_percent=50,
            warn_bot_p50_abs_error_minutes=4,
            current_time=current_time,
        )
    _assert_issue(summary, "bot_runtime_late_risk", MONITOR_WARNING)
    _assert_no_issue(summary, "bot_runtime_misses")
    _assert_no_issue(summary, "bot_runtime_p50_error")
    _assert_equal(summary.calibration.status if summary.calibration else "", "late_risk")
    formatted = format_monitor_summary(summary, db_path)
    _assert_contains(formatted, "bot_calibration:late_risk")
    _assert_contains(formatted, "bot_suggested_buffer:+1m")
    _assert_contains(formatted, "bot_runtime_late_risk profile=morning")
    _assert_contains(formatted, "miss=1(33%)")
    _assert_contains(formatted, "suggested=+1m")
    _assert_contains(formatted, "top_source=target_stop_live:1m/33%")
    _assert_contains(formatted, 'next="route74 support-report --profile morning"')


def _assert_bot_runtime_source_calibration_late_risk_warns(db_path: Path) -> None:
    current_time = now_local()
    with connect(db_path) as connection:
        init_db(connection)
        update_collector_heartbeat(
            connection,
            name="yandex-collect",
            pid=123,
            profile_filter="all",
            last_status="ok",
            last_message="collector alive before source calibration smoke",
            updated_at=current_time - timedelta(minutes=1),
        )
        _insert_ok_canary_runs(connection, checked_at=current_time - timedelta(minutes=1))
        for index, error_minutes in enumerate((-1, 1, 1)):
            _insert_runtime_prediction(
                connection,
                sampled_at=current_time - timedelta(minutes=12 - index),
                error_minutes=error_minutes,
                source=SOURCE_TARGET_STOP_LIVE,
            )
        for index in range(7):
            _insert_runtime_prediction(
                connection,
                sampled_at=current_time - timedelta(minutes=8 - index),
                error_minutes=1,
                source=SOURCE_HISTORY_HEADWAY,
            )
        connection.commit()
        summary = summarize_monitor(
            connection,
            db_path=db_path,
            min_bot_runtime_evaluated=3,
            warn_bot_miss_rate_percent=50,
            warn_bot_p50_abs_error_minutes=4,
            current_time=current_time,
        )
    _assert_issue(summary, "bot_runtime_source_late_risk", MONITOR_WARNING)
    _assert_no_issue(summary, "bot_runtime_late_risk")
    _assert_no_issue(summary, "bot_runtime_misses")
    _assert_equal(summary.calibration.status if summary.calibration else "", "balanced")
    formatted = format_monitor_summary(summary, db_path)
    _assert_contains(formatted, "bot_calibration:balanced")
    _assert_contains(formatted, "bot_runtime_source_late_risk profile=morning")
    _assert_contains(formatted, "source=target_stop_live")
    _assert_contains(formatted, "miss=1(33%)")
    _assert_contains(formatted, "suggested=+1m")
    _assert_contains(formatted, 'next="route74 prediction-calibration --window weekday_morning_09_12"')


def _assert_bot_runtime_pending_warns(db_path: Path) -> None:
    current_time = now_local()
    with connect(db_path) as connection:
        init_db(connection)
        update_collector_heartbeat(
            connection,
            name="yandex-collect",
            pid=123,
            profile_filter="all",
            last_status="ok",
            last_message="collector alive before pending smoke",
            updated_at=current_time - timedelta(minutes=1),
        )
        _insert_ok_canary_runs(connection, checked_at=current_time - timedelta(minutes=1))
        _insert_runtime_prediction(connection, sampled_at=current_time - timedelta(minutes=180), error_minutes=None)
        _insert_runtime_prediction(connection, sampled_at=current_time - timedelta(minutes=90), error_minutes=None)
        _insert_runtime_prediction(connection, sampled_at=current_time - timedelta(minutes=20), error_minutes=None)
        connection.commit()
        summary = summarize_monitor(
            connection,
            db_path=db_path,
            min_bot_runtime_evaluated=3,
            warn_bot_pending_age_minutes=120,
            critical_bot_pending_age_minutes=360,
            current_time=current_time,
        )
    _assert_issue(summary, "bot_runtime_pending", MONITOR_WARNING)
    _assert_no_issue(summary, "bot_runtime_misses")
    _assert_no_issue(summary, "bot_runtime_p50_error")
    formatted = format_monitor_summary(summary, db_path)
    _assert_contains(formatted, "bot_predictions:3")
    _assert_contains(formatted, "bot_evaluated:0")
    _assert_contains(formatted, "bot_pending:3")
    _assert_contains(formatted, 'next="route74 prediction-evaluate --window weekday_morning_09_12"')
    _assert_contains(formatted, "bot_runtime_pending profile=morning")
    _assert_contains(formatted, "pending=3/3(100%) oldest_pending=180m evaluated=0/3")


def _assert_bot_runtime_pending_uses_oldest_profile(db_path: Path) -> None:
    current_time = now_local()
    with connect(db_path) as connection:
        init_db(connection)
        update_collector_heartbeat(
            connection,
            name="yandex-collect",
            pid=123,
            profile_filter="all",
            last_status="ok",
            last_message="collector alive before pending profile smoke",
            updated_at=current_time - timedelta(minutes=1),
        )
        _insert_ok_canary_runs(connection, checked_at=current_time - timedelta(minutes=1))
        _insert_runtime_prediction(
            connection,
            sampled_at=current_time - timedelta(minutes=90),
            error_minutes=None,
            profile_key="morning",
        )
        _insert_runtime_prediction(
            connection,
            sampled_at=current_time - timedelta(minutes=180),
            error_minutes=None,
            profile_key="evening",
        )
        _insert_runtime_prediction(
            connection,
            sampled_at=current_time - timedelta(minutes=20),
            error_minutes=None,
            profile_key="evening",
        )
        connection.commit()
        summary = summarize_monitor(
            connection,
            db_path=db_path,
            min_bot_runtime_evaluated=3,
            warn_bot_pending_age_minutes=120,
            critical_bot_pending_age_minutes=360,
            current_time=current_time,
        )
    _assert_issue(summary, "bot_runtime_pending", MONITOR_WARNING)
    formatted = format_monitor_summary(summary, db_path)
    _assert_contains(formatted, 'next="route74 prediction-evaluate --window weekday_evening_19_22"')
    _assert_contains(formatted, "bot_runtime_pending profile=evening")
    _assert_contains(formatted, "pending=2/2(100%) oldest_pending=180m evaluated=0/2")
    _assert_not_contains(formatted, "bot_runtime_pending profile=morning")


def _assert_bot_runtime_monitor_ignores_watch_events(db_path: Path) -> None:
    current_time = now_local()
    with connect(db_path) as connection:
        init_db(connection)
        for index, error_minutes in enumerate((9, 8, 7)):
            _insert_runtime_prediction(
                connection,
                sampled_at=current_time - timedelta(minutes=10 - index),
                error_minutes=error_minutes,
                event_kind=BOT_EVENT_WATCH_EARLY,
            )
        _insert_runtime_prediction(
            connection,
            sampled_at=current_time - timedelta(minutes=1),
            error_minutes=None,
            event_kind=BOT_EVENT_USER_REPLY,
        )
        connection.commit()
        summary = summarize_monitor(
            connection,
            db_path=db_path,
            min_bot_runtime_evaluated=3,
            warn_bot_miss_rate_percent=50,
            warn_bot_p50_abs_error_minutes=4,
            warn_bot_pending_age_minutes=120,
            current_time=current_time,
        )
    runtime = summary.runtime
    if runtime is None:
        raise AssertionError("expected runtime summary")
    _assert_equal(runtime.total, 1)
    _assert_equal(runtime.evaluated, 0)
    _assert_equal(runtime.pending, 1)
    _assert_equal(tuple(group.key for group in runtime.by_event_kind), (BOT_EVENT_USER_REPLY,))
    _assert_no_issue(summary, "bot_runtime_misses")
    _assert_no_issue(summary, "bot_runtime_p50_error")
    _assert_no_issue(summary, "bot_runtime_pending")
    formatted = format_monitor_summary(summary, db_path)
    _assert_contains(formatted, "bot_predictions:1")
    _assert_contains(formatted, "bot_pending:1")
    _assert_not_contains(formatted, "bot_runtime_misses")


def _insert_ok_canary_runs(connection: object, *, checked_at: datetime) -> None:
    for profile_key in ("morning", "evening"):
        connection.execute(
            """
            INSERT INTO yandex_canary_runs(
                checked_at, status, source_method, profile_key, schema_hash,
                changed_keys_json, risk_reason, raw_summary_json
            )
            VALUES (?, 'ok', 'vehicle_prediction', ?, ?, ?, 'ok', ?)
            """,
            (
                checked_at.isoformat(),
                profile_key,
                "a" * 16,
                json.dumps({"changed": {}}, ensure_ascii=False),
                json.dumps({"source_method": "vehicle_prediction", "vehicle_count": 1}, ensure_ascii=False),
            ),
        )


def _mark_collector_ok(connection: object, current_time: datetime) -> None:
    update_collector_heartbeat(
        connection,
        name="yandex-collect",
        pid=123,
        profile_filter="all",
        last_status="ok",
        last_message="collector alive before latency smoke",
        updated_at=current_time - timedelta(minutes=1),
    )
    _insert_ok_canary_runs(connection, checked_at=current_time - timedelta(minutes=1))


def _assert_bot_runtime_p50_error_warns(db_path: Path) -> None:
    current_time = now_local()
    with connect(db_path) as connection:
        init_db(connection)
        _insert_runtime_prediction(connection, sampled_at=current_time - timedelta(minutes=3), error_minutes=5)
        _insert_runtime_prediction(connection, sampled_at=current_time - timedelta(minutes=2), error_minutes=6)
        _insert_runtime_prediction(connection, sampled_at=current_time - timedelta(minutes=1), error_minutes=7)
        connection.commit()
        summary = summarize_monitor(
            connection,
            db_path=db_path,
            min_bot_runtime_evaluated=3,
            warn_bot_p50_abs_error_minutes=4,
            critical_bot_p50_abs_error_minutes=9,
            current_time=current_time,
        )
    _assert_issue(summary, "bot_runtime_p50_error", MONITOR_WARNING)
    _assert_no_issue(summary, "bot_runtime_misses")
    formatted = format_monitor_summary(summary, db_path)
    _assert_contains(formatted, "bot_p50_abs:6m")
    _assert_contains(formatted, "profile=morning p50_abs=6m evaluated=3")


def _assert_bot_runtime_misses_are_profile_scoped(db_path: Path) -> None:
    current_time = now_local()
    with connect(db_path) as connection:
        init_db(connection)
        for offset, error_minutes in enumerate((1, 2, 3, 4)):
            _insert_runtime_prediction(
                connection,
                sampled_at=current_time - timedelta(minutes=10 - offset),
                error_minutes=error_minutes,
                profile_key="morning",
            )
        for offset, error_minutes in enumerate((-2, -1, 2)):
            _insert_runtime_prediction(
                connection,
                sampled_at=current_time - timedelta(minutes=5 - offset),
                error_minutes=error_minutes,
                profile_key="evening",
            )
        connection.commit()
        summary = summarize_monitor(
            connection,
            db_path=db_path,
            min_bot_runtime_evaluated=3,
            warn_bot_miss_rate_percent=50,
            critical_bot_miss_rate_percent=90,
            current_time=current_time,
        )

    miss_issues = _issues_by_key(summary, "bot_runtime_misses")
    _assert_equal(len(miss_issues), 1)
    _assert_equal(miss_issues[0].profile_key, "evening")
    formatted = format_monitor_summary(summary, db_path, profile_key="evening")
    morning_formatted = format_monitor_summary(summary, db_path, profile_key="morning")
    _assert_contains(formatted, "bot_runtime_misses profile=evening")
    _assert_contains(formatted, "profile=evening misses=2/3(67%)")
    _assert_contains(formatted, "top_source=target_stop_live:2")
    _assert_not_contains(morning_formatted, "bot_runtime_misses")


def _assert_bot_runtime_guardrail_unavailable_warns(db_path: Path) -> None:
    current_time = now_local()
    with connect(db_path) as connection:
        init_db(connection)
        update_collector_heartbeat(
            connection,
            name="yandex-collect",
            pid=123,
            profile_filter="all",
            last_status="ok",
            last_message="collector alive before guardrail smoke",
            updated_at=current_time - timedelta(minutes=1),
        )
        _insert_ok_canary_runs(connection, checked_at=current_time - timedelta(minutes=1))
        factor = ({"kind": "guardrail_unavailable"},)
        _insert_runtime_prediction(
            connection,
            sampled_at=current_time - timedelta(minutes=2),
            error_minutes=None,
            eta_factors=factor,
        )
        _insert_runtime_prediction(
            connection,
            sampled_at=current_time - timedelta(minutes=1),
            error_minutes=None,
            eta_factors=factor,
        )
        connection.commit()
        summary = summarize_monitor(
            connection,
            db_path=db_path,
            warn_bot_guardrail_unavailable=1,
            critical_bot_guardrail_unavailable=3,
            current_time=current_time,
        )
    _assert_issue(summary, "bot_runtime_guardrail_unavailable", MONITOR_WARNING)
    _assert_no_issue(summary, "bot_runtime_pending")
    formatted = format_monitor_summary(summary, db_path)
    _assert_contains(formatted, 'next="route74 support-report --profile morning"')
    _assert_contains(formatted, "bot_guardrail_unavailable:2(100%)")
    _assert_contains(formatted, "profile=morning guardrail_unavailable=2/2(100%)")
    _assert_contains(formatted, "top_source=target_stop_live:2")


def _assert_history_readiness_warns(db_path: Path) -> None:
    current_time = now_local()
    with connect(db_path) as connection:
        init_db(connection)
        summary = summarize_monitor(
            connection,
            db_path=db_path,
            profile_key="morning",
            current_time=current_time,
        )
    readiness = summary.readiness
    if readiness is None:
        raise AssertionError("expected history readiness summary")
    _assert_equal(readiness.ready, False)
    _assert_issue(summary, "history_readiness", MONITOR_WARNING)
    formatted = format_monitor_summary(summary, db_path)
    _assert_contains(formatted, "history:not_ready")
    _assert_contains(formatted, "history_window:weekday_morning_09_12")
    _assert_contains(formatted, "history_bucket:+/-30m")
    _assert_contains(formatted, "history_samples:0/20")


def _assert_forecast_health_rejects_invalid_inputs(db_path: Path) -> None:
    current_time = datetime(2026, 6, 4, 9, 15, tzinfo=NOVOSIBIRSK_TZ)
    with connect(db_path) as connection:
        init_db(connection)
        _assert_value_error(
            lambda: _forecast_health(connection, current_time, days=0),
            "days must be a positive integer",
        )
        _assert_value_error(
            lambda: _forecast_health(  # type: ignore[arg-type]
                connection,
                current_time,
                max_heartbeat_age_seconds=True,
            ),
            "max_heartbeat_age_seconds must be a positive integer",
        )


def _forecast_health(connection, current_time: datetime, **overrides: object) -> None:
    params = {
        "current_date": current_time,
        "days": 14,
        "min_samples": 20,
        "min_distinct_days": 3,
        "primary_bucket_minutes": 30,
        "fallback_bucket_minutes": 60,
        "max_age_seconds": 180,
        "step_minutes": 30,
        "max_heartbeat_age_seconds": 120,
    }
    params.update(overrides)
    summarize_forecast_health(connection, **params)


def _history_readiness_summary(*, ready: bool) -> ForecastReadinessSummary:
    current_time = now_local()
    if ready:
        primary_samples = fallback_samples = 20
        primary_distinct_days = fallback_distinct_days = 3
    else:
        primary_samples = 2
        fallback_samples = 1
        primary_distinct_days = 1
        fallback_distinct_days = 1
    return ForecastReadinessSummary(
        profile_key="morning",
        report_window_key="weekday_morning_09_12",
        current_time=current_time,
        days=30,
        min_samples=20,
        min_distinct_days=3,
        primary_bucket_minutes=30,
        fallback_bucket_minutes=60,
        max_age_seconds=86_400,
        total_samples=max(primary_samples, fallback_samples),
        eta_samples=max(primary_samples, fallback_samples),
        fresh_eta_samples=max(primary_samples, fallback_samples),
        traffic_samples=0,
        primary_samples=primary_samples,
        fallback_samples=fallback_samples,
        primary_distinct_days=primary_distinct_days,
        fallback_distinct_days=fallback_distinct_days,
        latest_sampled_at=current_time,
    )


def _history_backtest_summary(*, miss_rate_percent: int) -> ForecastBacktestSummary:
    evaluated_cases = 6
    miss_cases = round(evaluated_cases * miss_rate_percent / 100)
    return ForecastBacktestSummary(
        profile_key="morning",
        report_window_key="weekday_morning_09_12",
        history_days=14,
        bucket_minutes=30,
        min_samples=20,
        min_distinct_days=3,
        percentiles=(DEFAULT_HISTORY_PERCENTILE,),
        target_cases=8,
        results=(
            ForecastBacktestResult(
                percentile=DEFAULT_HISTORY_PERCENTILE,
                evaluated_cases=evaluated_cases,
                skipped_cases=2,
                miss_cases=miss_cases,
                bucket_accurate_cases=3,
                miss_minutes=9,
                extra_wait_minutes=4,
                mean_absolute_error=2.5,
            ),
        ),
    )


def _assert_history_readiness_monitor_action() -> None:
    current_time = now_local()
    readiness = ForecastReadinessSummary(
        profile_key="morning",
        report_window_key="weekday_morning_09_12",
        current_time=current_time,
        days=30,
        min_samples=20,
        min_distinct_days=3,
        primary_bucket_minutes=30,
        fallback_bucket_minutes=60,
        max_age_seconds=86_400,
        total_samples=12,
        eta_samples=12,
        fresh_eta_samples=12,
        traffic_samples=0,
        primary_samples=12,
        fallback_samples=8,
        primary_distinct_days=2,
        fallback_distinct_days=2,
        latest_sampled_at=current_time,
    )
    summary = MonitorSummary(
        db=SimpleNamespace(healthy=True),
        forecast=SimpleNamespace(ready=True, canary=SimpleNamespace(status="ok"), windows=()),
        latency=SimpleNamespace(
            total_events=0,
            error_events=0,
            error_rate_percent=0,
            no_eta_events=0,
            no_eta_rate_percent=0,
        ),
        issues=(
            MonitorIssue(
                MONITOR_WARNING,
                "history_readiness",
                "window=weekday_morning_09_12 bucket=+/-30m samples=12/20 days=2/3 fresh_eta=12 latest=2026-06-04 09:00",
                profile_key="morning",
            ),
        ),
        readiness=readiness,
    )
    formatted = format_monitor_summary(summary, Path("data/monitor.sqlite"))
    _assert_equal(
        next_monitor_action(summary),
        "route74 forecast-readiness --window weekday_morning_09_12",
    )
    _assert_contains(formatted, 'next="route74 forecast-readiness --window weekday_morning_09_12"')
    _assert_contains(formatted, "history:not_ready")
    _assert_contains(formatted, "history_window:weekday_morning_09_12")
    _assert_contains(formatted, "history_samples:12/20")


def _assert_history_readiness_routes_to_forecast_coverage() -> None:
    forecast = SimpleNamespace(
        ready=False,
        canary=SimpleNamespace(status="ok"),
        windows=(SimpleNamespace(profile_key="morning", status="insufficient_bucket_coverage"),),
    )
    summary = MonitorSummary(
        db=SimpleNamespace(healthy=True),
        forecast=forecast,
        latency=SimpleNamespace(
            total_events=0,
            error_events=0,
            error_rate_percent=0,
            no_eta_events=0,
            no_eta_rate_percent=0,
        ),
        issues=(MonitorIssue(MONITOR_WARNING, "history_readiness", "history not ready", profile_key="morning"),),
        runtime=None,
        readiness=_history_readiness_summary(ready=False),
    )
    formatted = format_monitor_summary(summary, Path("data/monitor.sqlite"), profile_key="morning")
    _assert_equal(
        next_monitor_action(summary, profile_key="morning"),
        "route74 forecast-coverage --window weekday_morning_09_12",
    )
    _assert_contains(formatted, 'next="route74 forecast-coverage --window weekday_morning_09_12"')
    _assert_contains(formatted, "history:not_ready")


def _assert_malformed_collector_runs_are_ignored(db_path: Path) -> None:
    current_time = datetime(2026, 6, 4, 9, 15, tzinfo=NOVOSIBIRSK_TZ)
    with connect(db_path) as connection:
        init_db(connection)
        _insert_collector_run_row(connection, started_at="not-a-date", result_count="bad")
        _insert_collector_run_row(connection, started_at=current_time.isoformat(), result_count=1)
        _insert_collector_run_row(
            connection,
            started_at=(current_time + timedelta(days=1)).isoformat(),
            result_count=3,
        )
        connection.commit()

        summary = summarize_collector_runs(connection, hours=24, current_time=current_time)
        window = summarize_collector_runs_for_report_window(
            connection,
            report_window=REPORT_WINDOWS[0],
            current_date=current_time,
            days=14,
        )
    _assert_equal(summary.total_runs, 1)
    _assert_equal(summary.result_runs, 1)
    _assert_equal(summary.latest_started_at, current_time)
    _assert_equal(window.total_runs, 1)
    _assert_equal(window.result_runs, 1)


def _assert_collector_runs_reject_invalid_windows(db_path: Path) -> None:
    current_time = datetime(2026, 6, 4, 9, 15, tzinfo=NOVOSIBIRSK_TZ)
    with connect(db_path) as connection:
        init_db(connection)
        _assert_value_error(
            lambda: summarize_collector_runs(connection, hours=0, current_time=current_time),
            "hours must be a positive integer",
        )
        _assert_value_error(
            lambda: summarize_collector_runs(connection, hours=True, current_time=current_time),  # type: ignore[arg-type]
            "hours must be a positive integer",
        )
        _assert_value_error(
            lambda: summarize_collector_runs_for_report_window(
                connection,
                report_window=REPORT_WINDOWS[0],
                current_date=current_time,
                days=0,
            ),
            "days must be a positive integer",
        )
        _assert_value_error(
            lambda: summarize_collector_runs_for_report_window(
                connection,
                report_window=REPORT_WINDOWS[0],
                current_date=current_time,
                days=True,  # type: ignore[arg-type]
            ),
            "days must be a positive integer",
        )


def _insert_bot_latency_row(
    connection,
    *,
    received_at: str,
    forecast_ms: object,
    send_ms: object,
    total_ms: object,
    status: str,
    error: object,
) -> None:
    connection.execute(
        """
        INSERT INTO bot_interaction_events(
            received_at, chat_id_hash, update_type, command, reply_source,
            yandex_source_method, forecast_ms, render_ms, send_ms, total_ms,
            status, error
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            received_at,
            "hash",
            "message",
            "/monitor",
            "none",
            "none",
            forecast_ms,
            0,
            send_ms,
            total_ms,
            status,
            error,
        ),
    )


def _insert_collector_run_row(connection, *, started_at: str, result_count: object) -> None:
    connection.execute(
        """
        INSERT INTO collector_runs(
            name, started_at, completed_at, pid, profile_filter,
            report_windows_only, active_profiles_json, status, message,
            result_count, eta_result_count, traffic_ok_count, raw_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "yandex-collect",
            started_at,
            started_at,
            1001,
            "all",
            1,
            '["morning"]',
            "ok",
            "smoke",
            result_count,
            1,
            1,
            "{}",
        ),
    )


def _insert_runtime_prediction(
    connection,
    *,
    sampled_at: datetime,
    error_minutes: int | None,
    eta_factors: tuple[dict[str, object], ...] = (),
    profile_key: str = "morning",
    source: str = SOURCE_TARGET_STOP_LIVE,
    event_kind: str = BOT_EVENT_USER_REPLY,
) -> None:
    predicted_minutes = 12
    report_window_key = "weekday_evening_19_22" if profile_key == "evening" else "weekday_morning_09_12"
    source_method = "history" if source == SOURCE_HISTORY_HEADWAY else "vehicle_prediction"
    raw_json = json.dumps(
        {
            "runtime_source": RUNTIME_SOURCE_WEB_APP,
            "event_kind": event_kind,
            "selected_departure_source": "yandex_history" if source == SOURCE_HISTORY_HEADWAY else "yandex",
            "urgency": "go_now",
            "target_wait_minutes": 3,
            "yandex_status": "ok",
            "eta_factors": list(eta_factors),
        },
        ensure_ascii=False,
    )
    cursor = connection.execute(
        """
        INSERT INTO prediction_events(
            yandex_snapshot_id, profile_key, sampled_at, report_window_key,
            source, source_method, predicted_minutes, predicted_arrival_at,
            confidence, vehicle_id, thread_id, traffic_provider, traffic_status,
            traffic_delay_seconds, runtime_source, raw_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            None,
            profile_key,
            sampled_at.isoformat(),
            report_window_key,
            source,
            source_method,
            predicted_minutes,
            (sampled_at + timedelta(minutes=predicted_minutes)).isoformat(),
            "low" if source == SOURCE_HISTORY_HEADWAY else "medium",
            "",
            "",
            "none",
            "not_collected",
            None,
            RUNTIME_SOURCE_WEB_APP,
            raw_json,
        ),
    )
    if error_minutes is None:
        return
    actual_minutes = predicted_minutes + error_minutes
    arrival = connection.execute(
        """
        INSERT INTO arrival_events(
            yandex_snapshot_id, profile_key, vehicle_id, thread_id, stop_id,
            arrived_at, source, confidence, lat, lng, raw_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            None,
            profile_key,
            "",
            "",
            "stop",
            (sampled_at + timedelta(minutes=actual_minutes)).isoformat(),
            "smoke",
            "high",
            None,
            None,
            "{}",
        ),
    )
    connection.execute(
        """
        INSERT INTO prediction_evaluations(
            prediction_event_id, arrival_event_id, profile_key, evaluated_at,
            actual_minutes, predicted_minutes, error_minutes, bucket, source, raw_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            int(cursor.lastrowid),
            int(arrival.lastrowid),
            profile_key,
            sampled_at.isoformat(),
            actual_minutes,
            predicted_minutes,
            error_minutes,
            "10_14",
            source,
            "{}",
        ),
    )


def _watch_state_summary(
    *,
    status: str,
    file_status: str = "ok",
    error_type: str = "",
    active_count: int = 0,
    due_count: int = 0,
    overdue_count: int = 0,
    invalid_records: int = 0,
    max_overdue_seconds: int | None = None,
    runtime_error_count: int = 0,
    runtime_error_records: int = 0,
    latest_error_at: datetime | None = None,
    runtime_error_types: tuple[str, ...] = (),
) -> WatchStateSummary:
    return WatchStateSummary(
        path=Path("data/custom-watches.json"),
        current_time=datetime(2026, 6, 4, 9, 0, tzinfo=NOVOSIBIRSK_TZ),
        status=status,
        active_count=active_count,
        due_count=due_count,
        overdue_count=overdue_count,
        expired_records=0,
        invalid_records=invalid_records,
        total_records=active_count + invalid_records,
        early_sent_count=0,
        oldest_age_minutes=None,
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


def _monitor_summary(
    *,
    issues: tuple[MonitorIssue, ...] = (),
    readiness: ForecastReadinessSummary | None = None,
    backtest: ForecastBacktestSummary | None = None,
    forecast: object | None = None,
    latency: object | None = None,
) -> MonitorSummary:
    return MonitorSummary(
        db=SimpleNamespace(healthy=True),
        forecast=forecast
        if forecast is not None
        else SimpleNamespace(ready=True, canary=SimpleNamespace(status="ok"), windows=()),
        latency=latency
        if latency is not None
        else SimpleNamespace(
            total_events=0,
            error_events=0,
            error_rate_percent=0,
            no_eta_events=0,
            no_eta_rate_percent=0,
        ),
        issues=issues,
        runtime=None,
        readiness=readiness,
        backtest=backtest,
    )


def _assert_issue(summary: object, key: str, severity: str | None = None) -> None:
    issues = getattr(summary, "issues")
    for issue in issues:
        if issue.key == key and (severity is None or issue.severity == severity):
            return
    suffix = f" with severity {severity}" if severity is not None else ""
    raise AssertionError(f"expected monitor issue {key}{suffix}")


def _issues_by_key(summary: object, key: str) -> tuple[MonitorIssue, ...]:
    return tuple(issue for issue in getattr(summary, "issues") if issue.key == key)


def _assert_no_issue(summary: object, key: str) -> None:
    issues = getattr(summary, "issues")
    if any(issue.key == key for issue in issues):
        raise AssertionError(f"did not expect monitor issue {key}")


def _assert_equal(actual: object, expected: object) -> None:
    if actual != expected:
        raise AssertionError(f"expected {expected!r}, got {actual!r}")


def _assert_contains(text: str, expected: str) -> None:
    if expected not in text:
        raise AssertionError(f"expected {expected!r} in {text!r}")


def _assert_not_contains(text: str, unexpected: str) -> None:
    if unexpected in text:
        raise AssertionError(f"did not expect {unexpected!r} in {text!r}")


def _assert_value_error(action: Callable[[], object], expected: str) -> None:
    try:
        action()
    except ValueError as exc:
        _assert_contains(str(exc), expected)
        return
    raise AssertionError(f"expected ValueError containing {expected!r}")


if __name__ == "__main__":
    main()
