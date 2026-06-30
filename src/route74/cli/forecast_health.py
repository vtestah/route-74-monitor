from __future__ import annotations

import argparse

from route74.cli.common import positive_int
from route74.cli.formatting import counts_text
from route74.services.yandex_history import (
    DEFAULT_FALLBACK_BUCKET_MINUTES,
    DEFAULT_HISTORY_DAYS,
    DEFAULT_HISTORY_MAX_AGE_SECONDS,
    DEFAULT_MIN_HISTORY_DAYS,
    DEFAULT_MIN_OBSERVATIONS,
    DEFAULT_PRIMARY_BUCKET_MINUTES,
)
from route74.storage import connect, init_db, summarize_forecast_health
from route74.storage.forecast_health import ForecastBucketGap, ForecastHealthSummary, ForecastWindowHealth
from route74.support_actions import forecast_coverage_command_for_window


def register_forecast_health_command(subparsers: argparse._SubParsersAction) -> None:
    health = subparsers.add_parser("forecast-health", help="Explain forecast DB readiness for all report windows.")
    health.add_argument("--days", type=positive_int, default=DEFAULT_HISTORY_DAYS)
    health.add_argument("--min-samples", type=positive_int, default=DEFAULT_MIN_OBSERVATIONS)
    health.add_argument("--min-days", type=positive_int, default=DEFAULT_MIN_HISTORY_DAYS)
    health.add_argument("--primary-bucket", type=positive_int, default=DEFAULT_PRIMARY_BUCKET_MINUTES)
    health.add_argument("--fallback-bucket", type=positive_int, default=DEFAULT_FALLBACK_BUCKET_MINUTES)
    health.add_argument("--max-age-seconds", type=positive_int, default=DEFAULT_HISTORY_MAX_AGE_SECONDS)
    health.add_argument("--step-minutes", type=positive_int, default=30)
    health.add_argument("--heartbeat-name", default="yandex-collect")
    health.add_argument("--max-heartbeat-age", type=positive_int, default=120)
    health.set_defaults(func=cmd_forecast_health)


def cmd_forecast_health(args: argparse.Namespace) -> None:
    try:
        with connect(args.db) as connection:
            init_db(connection)
            summary = summarize_forecast_health(
                connection,
                days=args.days,
                min_samples=args.min_samples,
                min_distinct_days=args.min_days,
                primary_bucket_minutes=args.primary_bucket,
                fallback_bucket_minutes=args.fallback_bucket,
                max_age_seconds=args.max_age_seconds,
                step_minutes=args.step_minutes,
                heartbeat_name=args.heartbeat_name,
                max_heartbeat_age_seconds=args.max_heartbeat_age,
            )
    except ValueError as exc:
        raise SystemExit(str(exc)) from None
    print(format_forecast_health_summary(summary, args.db))


def format_forecast_health_summary(summary: ForecastHealthSummary, db_path: object) -> str:
    status = "ready" if summary.ready else "not_ready"
    lines = [
        (
        f"forecast health status={status} days={summary.days} min_samples={summary.min_samples} "
        f"min_days={summary.min_distinct_days} db={db_path}"
    ),
        f"ready_windows={summary.ready_windows}/{summary.total_windows}",
        _format_collector(summary),
        _format_canary(summary),
    ]
    lines.extend(_format_window(window) for window in summary.windows)
    return "\n".join(lines)


def _format_collector(summary: ForecastHealthSummary) -> str:
    collector = summary.collector
    updated = collector.updated_at.strftime("%Y-%m-%d %H:%M") if collector.updated_at else "-"
    age = "-" if collector.age_seconds is None else f"{collector.age_seconds}s"
    return (
        f"collector={collector.name} status={collector.status} age={age}/{collector.max_age_seconds}s "
        f"updated={updated} message={collector.message}"
    )


def _format_canary(summary: ForecastHealthSummary) -> str:
    canary = summary.canary
    checked = canary.latest_checked_at.strftime("%Y-%m-%d %H:%M") if canary.latest_checked_at else "-"
    return (
        f"canary={canary.status} risky_runs={canary.risky_runs} "
        f"latest={checked} reason={canary.risk_reason}"
    )


def _format_window(window: ForecastWindowHealth) -> str:
    latest = window.latest_sampled_at.strftime("%Y-%m-%d %H:%M") if window.latest_sampled_at else "-"
    latest_arrival = window.latest_arrival_at.strftime("%Y-%m-%d %H:%M") if window.latest_arrival_at else "-"
    latest_run = window.collector_latest_started_at.strftime("%Y-%m-%d %H:%M") if window.collector_latest_started_at else "-"
    missing = ",".join(window.missing_bucket_labels) or "-"
    bucket_gaps = _format_bucket_gaps(window.bucket_gaps)
    parts = [
        f"window={window.window_key} profile={window.profile_key} status={window.status}",
        f"samples={window.total_samples} eta={window.eta_samples}({window.eta_coverage_percent}%)",
        f"fresh_eta={window.fresh_eta_samples}({window.fresh_eta_coverage_percent}%)",
        f"traffic={window.traffic_samples}({window.traffic_coverage_percent}%)",
        f"collector_runs={window.collector_runs} eta_runs={window.collector_eta_runs}"
        f"({window.collector_eta_run_percent}%) traffic_ok_runs={window.collector_traffic_ok_runs}"
        f"({window.collector_traffic_ok_run_percent}%) run_statuses={counts_text(window.collector_run_statuses)}",
        f"api_risk={window.api_risk_samples}({window.api_risk_percent}%)",
        f"api_risk_reasons={counts_text(window.api_risk_reasons)}",
        f"coordinate_fallback={window.coordinate_fallback_samples}({window.coordinate_fallback_percent}%)",
        f"coordinate_fallback_reasons={counts_text(window.coordinate_fallback_reasons)}",
        f"arrivals={window.arrival_events} predictions={window.prediction_events}",
        f"evaluated={window.prediction_evaluations} miss={window.prediction_miss_cases}"
        f"({window.prediction_miss_rate_percent}%)",
        f"bot_predictions={window.bot_prediction_events}",
        f"bot_evaluated={window.bot_prediction_evaluations}",
        f"bot_miss={window.bot_prediction_miss_cases}({window.bot_prediction_miss_rate_percent}%)",
        f"truth={window.truth_status} latest_arrival={latest_arrival} truth_reason={window.truth_reason}",
        f"buckets={window.ready_buckets}/{window.total_buckets}({window.readiness_percent}%)",
        f"missing_buckets={missing}",
        f"bucket_gaps={bucket_gaps}",
    ]
    coverage_action = _coverage_action(window)
    if coverage_action:
        parts.append(coverage_action)
    parts.extend(
        [
            f"integrity={window.forecast_without_report_samples}/{window.report_without_forecast_samples}",
            f"latest={latest} latest_run={latest_run}",
            f"reason={window.reason}",
        ]
    )
    return " ".join(parts)


def _coverage_action(window: ForecastWindowHealth) -> str:
    if window.status != "insufficient_bucket_coverage":
        return ""
    return f'coverage_action="{forecast_coverage_command_for_window(window.window_key)}"'


def _format_bucket_gaps(gaps: tuple[ForecastBucketGap, ...]) -> str:
    return ",".join(
        (
            f"{gap.label}:{gap.selected_sample_count}/{gap.min_samples}s,"
            f"{gap.selected_distinct_days}/{gap.min_distinct_days}d,±{gap.selected_bucket_minutes}m"
        )
        for gap in gaps
    ) or "-"
