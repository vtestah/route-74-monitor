from __future__ import annotations

import argparse

from route74.cli.bot_latency import register_bot_latency_command
from route74.cli.bot_runtime import register_bot_runtime_command
from route74.cli.common import positive_int
from route74.cli.forecast_backtest import register_forecast_backtest_command
from route74.cli.forecast_coverage import register_forecast_coverage_command
from route74.cli.forecast_health import register_forecast_health_command
from route74.cli.forecast_readiness import register_forecast_readiness_command
from route74.cli.formatting import (
    format_report_window_summary,
    format_yandex_telemetry_summary,
)
from route74.cli.monitor import register_monitor_command
from route74.cli.prediction_lab import register_prediction_lab_commands
from route74.cli.support_report import register_support_report_command
from route74.cli.support_snapshot import register_support_snapshot_command
from route74.domain.profiles import ALL_PROFILES_KEY, PROFILE_SELECTORS
from route74.domain.reporting import ALL_REPORT_WINDOWS_KEY, REPORT_WINDOW_SELECTORS
from route74.storage import (
    connect,
    init_db,
    summarize_report_windows,
    summarize_yandex_telemetry,
)


def register_stats_commands(subparsers: argparse._SubParsersAction) -> None:
    yandex_stats = subparsers.add_parser("yandex-stats", help="Summarize collected Yandex telemetry quality.")
    yandex_stats.add_argument("--profile", choices=PROFILE_SELECTORS, default=ALL_PROFILES_KEY)
    yandex_stats.add_argument("--hours", type=positive_int, default=24, help="Summary window in hours.")
    yandex_stats.add_argument("--heartbeat-name", default="yandex-collect", help="SQLite heartbeat name.")
    yandex_stats.set_defaults(func=cmd_yandex_stats)

    report_stats = subparsers.add_parser("report-stats", help="Summarize weekday report-window samples.")
    report_stats.add_argument("--profile", choices=PROFILE_SELECTORS, default=ALL_PROFILES_KEY)
    report_stats.add_argument("--window", choices=REPORT_WINDOW_SELECTORS, default=ALL_REPORT_WINDOWS_KEY)
    report_stats.add_argument("--days", type=positive_int, default=30, help="Summary window in days.")
    report_stats.set_defaults(func=cmd_report_stats)

    register_forecast_readiness_command(subparsers)
    register_forecast_coverage_command(subparsers)
    register_forecast_health_command(subparsers)
    register_forecast_backtest_command(subparsers)
    register_prediction_lab_commands(subparsers)
    register_bot_latency_command(subparsers)
    register_bot_runtime_command(subparsers)
    register_monitor_command(subparsers)
    register_support_snapshot_command(subparsers)
    register_support_report_command(subparsers)


def cmd_yandex_stats(args: argparse.Namespace) -> None:
    profile_key = None if args.profile == ALL_PROFILES_KEY else args.profile
    with connect(args.db) as connection:
        init_db(connection)
        summary = summarize_yandex_telemetry(
            connection,
            hours=args.hours,
            profile_key=profile_key,
            heartbeat_name=args.heartbeat_name,
        )
    print(format_yandex_telemetry_summary(summary, args.db))


def cmd_report_stats(args: argparse.Namespace) -> None:
    profile_key = None if args.profile == ALL_PROFILES_KEY else args.profile
    report_window_key = None if args.window == ALL_REPORT_WINDOWS_KEY else args.window
    with connect(args.db) as connection:
        init_db(connection)
        summary = summarize_report_windows(
            connection,
            days=args.days,
            report_window_key=report_window_key,
            profile_key=profile_key,
        )
    print(format_report_window_summary(summary, args.db))
