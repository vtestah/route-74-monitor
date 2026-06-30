from __future__ import annotations

import argparse

from route74.cli.common import positive_int
from route74.cli.forecast_formatting import format_forecast_window_coverage_summary
from route74.cli.forecast_readiness import WINDOWS_BY_KEY
from route74.models import now_local
from route74.services.yandex_history import (
    DEFAULT_FALLBACK_BUCKET_MINUTES,
    DEFAULT_HISTORY_DAYS,
    DEFAULT_HISTORY_MAX_AGE_SECONDS,
    DEFAULT_MIN_HISTORY_DAYS,
    DEFAULT_MIN_OBSERVATIONS,
    DEFAULT_PRIMARY_BUCKET_MINUTES,
)
from route74.storage import connect, init_db, summarize_yandex_forecast_window_coverage


def register_forecast_coverage_command(subparsers: argparse._SubParsersAction) -> None:
    coverage = subparsers.add_parser(
        "forecast-coverage",
        help="Check forecast sample coverage across a report window.",
    )
    coverage.add_argument("--window", choices=tuple(WINDOWS_BY_KEY), required=True)
    coverage.add_argument("--days", type=positive_int, default=DEFAULT_HISTORY_DAYS)
    coverage.add_argument("--min-samples", type=positive_int, default=DEFAULT_MIN_OBSERVATIONS)
    coverage.add_argument("--min-days", type=positive_int, default=DEFAULT_MIN_HISTORY_DAYS)
    coverage.add_argument("--primary-bucket", type=positive_int, default=DEFAULT_PRIMARY_BUCKET_MINUTES)
    coverage.add_argument("--fallback-bucket", type=positive_int, default=DEFAULT_FALLBACK_BUCKET_MINUTES)
    coverage.add_argument("--max-age-seconds", type=positive_int, default=DEFAULT_HISTORY_MAX_AGE_SECONDS)
    coverage.add_argument("--step-minutes", type=positive_int, default=30)
    coverage.set_defaults(func=cmd_forecast_coverage)


def cmd_forecast_coverage(args: argparse.Namespace) -> None:
    try:
        with connect(args.db) as connection:
            init_db(connection)
            summary = summarize_yandex_forecast_window_coverage(
                connection,
                report_window=WINDOWS_BY_KEY[args.window],
                current_date=now_local(),
                days=args.days,
                min_samples=args.min_samples,
                min_distinct_days=args.min_days,
                primary_bucket_minutes=args.primary_bucket,
                fallback_bucket_minutes=args.fallback_bucket,
                max_age_seconds=args.max_age_seconds,
                step_minutes=args.step_minutes,
            )
    except ValueError as exc:
        raise SystemExit(str(exc)) from None
    print(format_forecast_window_coverage_summary(summary, args.db))
