from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from route74.cli.common import percent_int, positive_int
from route74.domain.profiles import PROFILE_KEYS
from route74.domain.runtime_sources import BOT_EVENT_USER_REPLY
from route74.models import now_local
from route74.storage import connect, init_db
from route74.storage.forecast_backtest import best_forecast_backtest_result, selected_forecast_backtest_result
from route74.storage.forecast_health import ForecastHealthSummary, ForecastWindowHealth
from route74.storage.monitoring import (
    MONITOR_CRITICAL,
    MONITOR_OK,
    MONITOR_WARNING,
    MonitorIssue,
    MonitorSummary,
    summarize_monitor,
)
from route74.support_actions import (
    bot_latency_command,
    bot_runtime_command,
    forecast_backtest_command_for_profile,
    forecast_coverage_command_for_profile,
    forecast_readiness_command_for_profile,
    prediction_calibration_command_for_profile,
    prediction_evaluate_command_for_profile,
    support_report_command_for_profile,
    watch_state_command_for_path,
)
from route74.support_triage import TRIAGE_KEY_PRIORITY
from route74.watch_state import DEFAULT_WATCH_STATE_PATH, WatchStateSummary, summarize_watch_state


FAIL_CHOICES = ("never", MONITOR_WARNING, MONITOR_CRITICAL)
MONITOR_ACTIONS = {
    "db_integrity": "route74 db-health",
    "collector": "route74 forecast-health",
    "yandex_canary": "./bin/smoke-yandex",
    "bot_latency_errors": bot_latency_command(event_kind=BOT_EVENT_USER_REPLY),
    "bot_latency_malformed": bot_latency_command(event_kind=BOT_EVENT_USER_REPLY),
    "bot_latency_p95": bot_latency_command(event_kind=BOT_EVENT_USER_REPLY),
    "bot_latency_stale": bot_latency_command(event_kind=BOT_EVENT_USER_REPLY),
    "bot_no_eta_replies": bot_latency_command(event_kind=BOT_EVENT_USER_REPLY),
    "bot_runtime_misses": bot_runtime_command(event_kind=BOT_EVENT_USER_REPLY),
    "bot_runtime_late_risk": bot_runtime_command(event_kind=BOT_EVENT_USER_REPLY),
    "bot_runtime_source_late_risk": bot_runtime_command(event_kind=BOT_EVENT_USER_REPLY),
    "bot_runtime_pending": bot_runtime_command(event_kind=BOT_EVENT_USER_REPLY),
    "bot_runtime_p50_error": bot_runtime_command(event_kind=BOT_EVENT_USER_REPLY),
    "bot_runtime_guardrail_unavailable": bot_runtime_command(event_kind=BOT_EVENT_USER_REPLY),
}


def register_monitor_command(subparsers: argparse._SubParsersAction) -> None:
    monitor = subparsers.add_parser("monitor-tick", help="Print one operational verdict for cron/systemd checks.")
    monitor.add_argument("--latency-hours", type=positive_int, default=24)
    monitor.add_argument("--warn-latency-ms", type=positive_int, default=5_000)
    monitor.add_argument("--critical-latency-ms", type=positive_int, default=15_000)
    monitor.add_argument("--warn-error-rate", type=percent_int, default=10)
    monitor.add_argument("--critical-error-rate", type=percent_int, default=30)
    monitor.add_argument(
        "--min-no-eta-events",
        type=positive_int,
        default=3,
        help="Minimum no-ETA commute replies before no-ETA rate alerts.",
    )
    monitor.add_argument(
        "--warn-no-eta-rate",
        type=percent_int,
        default=50,
        help="Warn when no-ETA commute replies reach this percent of bot responses.",
    )
    monitor.add_argument(
        "--critical-no-eta-rate",
        type=percent_int,
        default=80,
        help="Critical when no-ETA commute replies reach this percent of bot responses.",
    )
    monitor.add_argument(
        "--runtime-hours",
        type=positive_int,
        default=24,
        help="Bot runtime prediction window in hours.",
    )
    monitor.add_argument(
        "--min-bot-evaluated",
        type=positive_int,
        default=3,
        help="Minimum evaluated bot runtime predictions before quality alerts.",
    )
    monitor.add_argument(
        "--warn-bot-miss-rate",
        type=percent_int,
        default=50,
        help="Warn when evaluated bot runtime miss rate reaches this percent.",
    )
    monitor.add_argument(
        "--critical-bot-miss-rate",
        type=percent_int,
        default=80,
        help="Critical when evaluated bot runtime miss rate reaches this percent.",
    )
    monitor.add_argument(
        "--warn-bot-p50-error-minutes",
        type=positive_int,
        default=4,
        help="Warn when bot runtime p50 absolute error reaches this many minutes.",
    )
    monitor.add_argument(
        "--critical-bot-p50-error-minutes",
        type=positive_int,
        default=8,
        help="Critical when bot runtime p50 absolute error reaches this many minutes.",
    )
    monitor.add_argument(
        "--warn-bot-pending-age-minutes",
        type=positive_int,
        default=120,
        help="Warn when the oldest unevaluated bot runtime prediction is this old.",
    )
    monitor.add_argument(
        "--critical-bot-pending-age-minutes",
        type=positive_int,
        default=360,
        help="Critical when the oldest unevaluated bot runtime prediction is this old.",
    )
    monitor.add_argument(
        "--warn-bot-guardrail-unavailable",
        type=positive_int,
        default=1,
        help="Warn when this many bot replies ran without stored prediction guardrails.",
    )
    monitor.add_argument(
        "--critical-bot-guardrail-unavailable",
        type=positive_int,
        default=3,
        help="Critical when this many bot replies ran without stored prediction guardrails.",
    )
    monitor.add_argument(
        "--watch-state-path",
        type=Path,
        default=DEFAULT_WATCH_STATE_PATH,
        help="Persisted watch state JSON path.",
    )
    monitor.add_argument(
        "--profile",
        choices=PROFILE_KEYS,
        default=None,
        help="Show and fail on issues for one commute profile, keeping global infra issues.",
    )
    monitor.add_argument("--fail-on", choices=FAIL_CHOICES, default=MONITOR_CRITICAL)
    monitor.set_defaults(func=cmd_monitor_tick)


def cmd_monitor_tick(args: argparse.Namespace) -> None:
    current_time = now_local()
    with connect(args.db) as connection:
        init_db(connection)
        try:
            summary = summarize_monitor(
                connection,
                db_path=args.db,
                latency_hours=args.latency_hours,
                warn_latency_ms=args.warn_latency_ms,
                critical_latency_ms=args.critical_latency_ms,
                warn_error_rate_percent=args.warn_error_rate,
                critical_error_rate_percent=args.critical_error_rate,
                min_no_eta_events=args.min_no_eta_events,
                warn_no_eta_rate_percent=args.warn_no_eta_rate,
                critical_no_eta_rate_percent=args.critical_no_eta_rate,
                runtime_hours=args.runtime_hours,
                min_bot_runtime_evaluated=args.min_bot_evaluated,
                warn_bot_miss_rate_percent=args.warn_bot_miss_rate,
                critical_bot_miss_rate_percent=args.critical_bot_miss_rate,
                warn_bot_p50_abs_error_minutes=args.warn_bot_p50_error_minutes,
                critical_bot_p50_abs_error_minutes=args.critical_bot_p50_error_minutes,
                warn_bot_pending_age_minutes=args.warn_bot_pending_age_minutes,
                critical_bot_pending_age_minutes=args.critical_bot_pending_age_minutes,
                warn_bot_guardrail_unavailable=args.warn_bot_guardrail_unavailable,
                critical_bot_guardrail_unavailable=args.critical_bot_guardrail_unavailable,
                profile_key=args.profile,
                current_time=current_time,
            )
        except ValueError as exc:
            raise SystemExit(str(exc)) from None
    watch_state = summarize_watch_state(args.watch_state_path, current_time)
    print(
        format_monitor_summary(
            summary,
            args.db,
            watch_state=watch_state,
            watch_state_path=args.watch_state_path,
            profile_key=args.profile,
        )
    )
    code = _exit_code(summary, fail_on=args.fail_on, watch_state=watch_state, profile_key=args.profile)
    if code:
        raise SystemExit(code)


def format_monitor_summary(
    summary: MonitorSummary,
    db_path: object,
    *,
    watch_state: WatchStateSummary | None = None,
    watch_state_path: Path = DEFAULT_WATCH_STATE_PATH,
    profile_key: str | None = None,
) -> str:
    issues = _scoped_issues(_combined_issues(summary, watch_state), profile_key)
    next_action = next_monitor_action(
        summary,
        watch_state=watch_state,
        watch_state_path=watch_state_path,
        profile_key=profile_key,
    )
    lines = [
        (
            f"monitor{_scope_text(profile_key)} status={_combined_status(issues)} "
            f"warnings={_issue_count(issues, MONITOR_WARNING)} "
            f"criticals={_issue_count(issues, MONITOR_CRITICAL)} "
            f"next=\"{next_action}\" "
            f"db={db_path}"
        ),
        (
            f"signals=db:{'ok' if summary.db.healthy else 'bad'} "
            f"forecast:{'ready' if summary.forecast.ready else 'not_ready'} "
            f"canary:{summary.forecast.canary.status} "
            f"{history_signal(summary)} "
            f"{history_backtest_signal(summary)} "
            f"bot_events:{summary.latency.total_events} "
            f"bot_errors:{summary.latency.error_events}({summary.latency.error_rate_percent}%)"
            f" bot_no_eta:{summary.latency.no_eta_events}({summary.latency.no_eta_rate_percent}%)"
            f" {runtime_signal(summary)}"
            f" {watch_signal(watch_state)}"
        ),
    ]
    lines.extend(
        f"- {issue.severity} {issue.key}{_issue_profile_text(issue.profile_key)}: {issue.message}"
        for issue in issues
    )
    return "\n".join(lines)


def runtime_signal(summary: MonitorSummary) -> str:
    runtime = summary.runtime
    if runtime is None:
        return "bot_predictions:-"
    return (
        f"bot_predictions:{runtime.total} "
        f"bot_evaluated:{runtime.evaluated} "
        f"bot_pending:{runtime.pending} "
        f"bot_miss:{runtime.misses}({runtime.miss_rate_percent}%) "
        f"bot_guardrail_unavailable:{runtime.guardrail_unavailable}({runtime.guardrail_unavailable_percent}%) "
        f"bot_p50_abs:{_minutes(runtime.p50_abs_error_minutes)}"
        f" {runtime_calibration_signal(summary)}"
    )


def runtime_calibration_signal(summary: MonitorSummary) -> str:
    calibration = summary.calibration
    if calibration is None:
        return "bot_calibration:-"
    return (
        f"bot_calibration:{calibration.status} "
        f"bot_suggested_buffer:+{calibration.suggested_buffer_minutes}m"
    )


def history_signal(summary: MonitorSummary) -> str:
    readiness = summary.readiness
    if readiness is None:
        return "history:-"
    status = "ready" if readiness.ready else "not_ready"
    window = readiness.report_window_key or "-"
    return (
        f"history:{status} "
        f"history_window:{window} "
        f"history_bucket:+/-{readiness.selected_bucket_minutes}m "
        f"history_samples:{readiness.selected_sample_count}/{readiness.min_samples} "
        f"history_days:{readiness.selected_distinct_days}/{readiness.min_distinct_days}"
    )


def history_backtest_signal(summary: MonitorSummary) -> str:
    backtest = summary.backtest
    if backtest is None:
        return "history_backtest:-"
    result = selected_forecast_backtest_result(backtest)
    if result is None:
        return f"history_backtest:empty history_backtest_cases:{backtest.target_cases}"
    best = best_forecast_backtest_result(backtest)
    best_signal = ""
    if best is not None and best.percentile != result.percentile and best.evaluated_cases > 0:
        best_signal = (
            f" history_backtest_best:p{best.percentile}"
            f"({best.miss_cases}/{best.evaluated_cases},{best.miss_rate_percent}%)"
        )
    return (
        f"history_backtest:p{result.percentile} "
        f"history_backtest_eval:{result.evaluated_cases}/{backtest.target_cases} "
        f"history_backtest_miss:{result.miss_cases}({result.miss_rate_percent}%) "
        f"history_backtest_accuracy:{result.bucket_accuracy_percent}%"
        f"{best_signal}"
    )


def watch_signal(watch_state: WatchStateSummary | None) -> str:
    if watch_state is None:
        return "watch:-"
    error_detail = ""
    if watch_state.runtime_error_count:
        error_detail = (
            f" watch_latest_error_age:{_watch_error_age_text(watch_state)}"
            f" watch_error_types:{_runtime_error_types_signal_text(watch_state.runtime_error_types)}"
        )
    return (
        f"watch:{watch_state.status} "
        f"watch_active:{watch_state.active_count} "
        f"watch_due:{watch_state.due_count} "
        f"watch_overdue:{watch_state.overdue_count} "
        f"watch_errors:{watch_state.runtime_error_count}{error_detail} "
        f"watch_file:{watch_state.file_status}"
    )


def next_monitor_action(
    summary: MonitorSummary,
    *,
    watch_state: WatchStateSummary | None = None,
    watch_state_path: Path = DEFAULT_WATCH_STATE_PATH,
    profile_key: str | None = None,
) -> str:
    issues = _scoped_issues(_combined_issues(summary, watch_state), profile_key)
    if not issues:
        return "route74 monitor-tick --fail-on critical"
    issue = max(issues, key=lambda item: _scoped_issue_priority(item, profile_key))
    return _issue_action(
        issue,
        forecast=summary.forecast,
        watch_state_path=watch_state_path,
        profile_key=profile_key,
        hours=getattr(summary.latency, "hours", 24),
    )


def _issue_action(
    issue: MonitorIssue,
    *,
    forecast: ForecastHealthSummary,
    watch_state_path: Path = DEFAULT_WATCH_STATE_PATH,
    profile_key: str | None = None,
    hours: int = 24,
) -> str:
    fallback = _issue_fallback_action(issue, watch_state_path=watch_state_path, hours=hours)
    if issue.key.startswith("bot_latency_") or issue.key == "bot_no_eta_replies":
        return _profile_action(
            profile_key or issue.profile_key,
            lambda profile_key: bot_latency_command(
                hours=hours,
                profile_key=profile_key,
                event_kind=BOT_EVENT_USER_REPLY,
            ),
            fallback=fallback,
        )
    if issue.key == "bot_runtime_pending":
        return _profile_action(
            issue.profile_key,
            prediction_evaluate_command_for_profile,
            fallback=fallback,
        )
    if issue.key == "bot_runtime_guardrail_unavailable":
        return _profile_action(
            profile_key or issue.profile_key,
            support_report_command_for_profile,
            fallback=bot_runtime_command(hours=hours, limit=8, event_kind=BOT_EVENT_USER_REPLY),
        )
    if issue.key in {
        "bot_runtime_misses",
    }:
        return _profile_action(
            profile_key or issue.profile_key,
            support_report_command_for_profile,
            fallback=bot_runtime_command(hours=hours, limit=8, event_kind=BOT_EVENT_USER_REPLY),
        )
    if issue.key == "bot_runtime_late_risk":
        return _profile_action(
            profile_key or issue.profile_key,
            support_report_command_for_profile,
            fallback=bot_runtime_command(hours=hours, limit=8, event_kind=BOT_EVENT_USER_REPLY),
        )
    if issue.key == "bot_runtime_p50_error":
        return _profile_action(
            profile_key or issue.profile_key,
            support_report_command_for_profile,
            fallback=bot_runtime_command(hours=hours, limit=8, event_kind=BOT_EVENT_USER_REPLY),
        )
    if issue.key == "bot_runtime_source_late_risk":
        return _profile_action(
            profile_key or issue.profile_key,
            prediction_calibration_command_for_profile,
            fallback=bot_runtime_command(hours=hours, limit=8, event_kind=BOT_EVENT_USER_REPLY),
        )
    if issue.key == "history_readiness":
        return _profile_action(
            profile_key or issue.profile_key,
            lambda profile_key: _history_readiness_action(forecast, profile_key),
            fallback=fallback,
        )
    if issue.key == "history_backtest":
        return _profile_action(
            profile_key or issue.profile_key,
            forecast_backtest_command_for_profile,
            fallback=fallback,
        )
    if issue.profile_key and (
        issue.key.startswith("forecast_")
        or issue.key.startswith("truth_")
        or issue.key.startswith("bot_runtime_")
    ):
        return _profile_action(
            issue.profile_key,
            support_report_command_for_profile,
            fallback=fallback,
        )
    return fallback


def _history_readiness_action(forecast: ForecastHealthSummary, profile_key: str) -> str:
    window = _forecast_window_for_profile(getattr(forecast, "windows", ()), profile_key)
    if window is not None and getattr(window, "status", "") == "insufficient_bucket_coverage":
        return forecast_coverage_command_for_profile(profile_key)
    return forecast_readiness_command_for_profile(profile_key)


def _issue_fallback_action(
    issue: MonitorIssue,
    *,
    watch_state_path: Path = DEFAULT_WATCH_STATE_PATH,
    hours: int = 24,
) -> str:
    if issue.key.startswith("watch_state_"):
        return watch_state_command_for_path(watch_state_path)
    if issue.key.startswith("forecast_"):
        return "route74 forecast-health"
    if issue.key.startswith("truth_"):
        return "route74 forecast-health"
    if issue.key.startswith("bot_latency_") or issue.key == "bot_no_eta_replies":
        return bot_latency_command(hours=hours, event_kind=BOT_EVENT_USER_REPLY)
    return MONITOR_ACTIONS.get(issue.key, "route74 forecast-health")


def _issue_priority(severity: str, key: str) -> tuple[int, int]:
    severity_rank = {MONITOR_CRITICAL: 2, MONITOR_WARNING: 1}.get(severity, 0)
    key_rank = TRIAGE_KEY_PRIORITY.get(key, 10)
    return severity_rank, key_rank


def _scoped_issue_priority(issue: MonitorIssue, profile_key: str | None) -> tuple[int, int, int]:
    severity_rank, key_rank = _issue_priority(issue.severity, issue.key)
    profile_rank = 1 if profile_key is not None and issue.profile_key == profile_key else 0
    return severity_rank, profile_rank, key_rank


def _forecast_window_for_profile(
    windows: tuple[ForecastWindowHealth, ...],
    profile_key: str,
) -> ForecastWindowHealth | None:
    return next((window for window in windows if window.profile_key == profile_key), None)


def _combined_issues(
    summary: MonitorSummary,
    watch_state: WatchStateSummary | None,
) -> tuple[MonitorIssue, ...]:
    return (*summary.issues, *_watch_state_issues(watch_state))


def _scoped_issues(issues: tuple[MonitorIssue, ...], profile_key: str | None) -> tuple[MonitorIssue, ...]:
    if profile_key is None:
        return issues
    return tuple(issue for issue in issues if not issue.profile_key or issue.profile_key == profile_key)


def _watch_state_issues(watch_state: WatchStateSummary | None) -> tuple[MonitorIssue, ...]:
    if watch_state is None:
        return ()
    issues: list[MonitorIssue] = []
    if watch_state.status == MONITOR_CRITICAL:
        issues.append(
            MonitorIssue(
                MONITOR_CRITICAL,
                "watch_state_file",
                f"file={watch_state.file_status} type={watch_state.error_type or '-'}",
            )
        )
        return tuple(issues)
    if watch_state.overdue_count:
        issues.append(
            MonitorIssue(
                MONITOR_CRITICAL,
                "watch_state_overdue",
                (
                    f"active={watch_state.active_count} due={watch_state.due_count} "
                    f"overdue={watch_state.overdue_count} max_overdue={_seconds(watch_state.max_overdue_seconds)}"
                ),
            )
        )
    if watch_state.runtime_error_count:
        issues.append(
            MonitorIssue(
                MONITOR_WARNING,
                "watch_state_runtime_error",
                _watch_state_runtime_error_message(watch_state),
            )
        )
    if watch_state.invalid_records:
        issues.append(
            MonitorIssue(
                MONITOR_WARNING,
                "watch_state_invalid",
                f"invalid={watch_state.invalid_records} total={watch_state.total_records}",
            )
        )
    return tuple(issues)


def _combined_status(issues: tuple[MonitorIssue, ...]) -> str:
    if not issues:
        return MONITOR_OK
    return max(issues, key=lambda issue: _severity_rank(issue.severity)).severity


def _issue_count(issues: tuple[MonitorIssue, ...], severity: str) -> int:
    return sum(1 for issue in issues if issue.severity == severity)


def _severity_rank(severity: str) -> int:
    return {MONITOR_CRITICAL: 2, MONITOR_WARNING: 1}.get(severity, 0)


def _profile_action(profile_key: str, factory: Callable[[str], str], *, fallback: str) -> str:
    try:
        return factory(profile_key)
    except (TypeError, ValueError):
        return fallback


def _issue_profile_text(profile_key: str) -> str:
    return "" if not profile_key else f" profile={profile_key}"


def _scope_text(profile_key: str | None) -> str:
    return "" if not profile_key else f" profile={profile_key}"


def _minutes(value: int | None) -> str:
    return "-" if value is None else f"{value}m"


def _seconds(value: int | None) -> str:
    return "-" if value is None else f"{value}s"


def _watch_state_runtime_error_message(watch_state: WatchStateSummary) -> str:
    return (
        f"errors={watch_state.runtime_error_count} watches={watch_state.runtime_error_records} "
        f"latest={_datetime_text(watch_state.latest_error_at)} types={_runtime_error_types_text(watch_state.runtime_error_types)}"
    )


def _datetime_text(value: object) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    return "n/a"


def _runtime_error_types_text(values: tuple[str, ...]) -> str:
    return "n/a" if not values else ", ".join(values)


def _runtime_error_types_signal_text(values: tuple[str, ...]) -> str:
    if not values:
        return "n/a"
    return ",".join(_signal_value(value) for value in values[:3] if value) or "n/a"


def _signal_value(value: str) -> str:
    return "_".join(str(value).split())[:80] or "-"


def _watch_error_age_text(watch_state: WatchStateSummary) -> str:
    latest = watch_state.latest_error_at
    if latest is None:
        return "n/a"
    try:
        seconds = max(0, round((watch_state.current_time - latest).total_seconds()))
    except TypeError:
        return "n/a"
    if seconds < 60:
        return f"{seconds}s"
    minutes = round(seconds / 60)
    if minutes < 60:
        return f"{minutes}m"
    return f"{round(minutes / 60)}h"


def _exit_code(
    summary: MonitorSummary,
    *,
    fail_on: str,
    watch_state: WatchStateSummary | None = None,
    profile_key: str | None = None,
) -> int:
    status = _combined_status(_scoped_issues(_combined_issues(summary, watch_state), profile_key))
    if fail_on == "never":
        return 0
    if fail_on == MONITOR_WARNING and status in {MONITOR_WARNING, MONITOR_CRITICAL}:
        return 1
    if fail_on == MONITOR_CRITICAL and status == MONITOR_CRITICAL:
        return 2
    return 0
