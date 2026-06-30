from __future__ import annotations

import argparse

from route74.cli.common import positive_int
from route74.cli.forecast_readiness import WINDOWS_BY_KEY
from route74.services.yandex_history import (
    DEFAULT_HISTORY_DAYS,
    DEFAULT_HISTORY_MAX_AGE_SECONDS,
    DEFAULT_MIN_HISTORY_DAYS,
    DEFAULT_MIN_OBSERVATIONS,
    DEFAULT_PRIMARY_BUCKET_MINUTES,
)
from route74.storage import connect, init_db
from route74.storage.forecast_backtest import (
    DEFAULT_FORECAST_BACKTEST_PERCENTILES,
    ForecastBacktestResult,
    ForecastBacktestSummary,
    summarize_yandex_forecast_backtest,
    validate_forecast_backtest_percentiles,
)


def register_forecast_backtest_command(subparsers: argparse._SubParsersAction) -> None:
    backtest = subparsers.add_parser("forecast-backtest", help="Compare Yandex history forecast percentiles.")
    backtest.add_argument("--window", choices=tuple(WINDOWS_BY_KEY), required=True)
    backtest.add_argument("--days", type=positive_int, default=DEFAULT_HISTORY_DAYS)
    backtest.add_argument("--bucket", type=positive_int, default=DEFAULT_PRIMARY_BUCKET_MINUTES)
    backtest.add_argument("--min-samples", type=positive_int, default=DEFAULT_MIN_OBSERVATIONS)
    backtest.add_argument("--min-days", type=positive_int, default=DEFAULT_MIN_HISTORY_DAYS)
    backtest.add_argument("--max-age-seconds", type=positive_int, default=DEFAULT_HISTORY_MAX_AGE_SECONDS)
    backtest.add_argument("--percentiles", type=_percentiles, default=DEFAULT_FORECAST_BACKTEST_PERCENTILES)
    backtest.set_defaults(func=cmd_forecast_backtest)


def cmd_forecast_backtest(args: argparse.Namespace) -> None:
    window = WINDOWS_BY_KEY[args.window]
    with connect(args.db) as connection:
        init_db(connection)
        summary = summarize_yandex_forecast_backtest(
            connection,
            profile_key=window.profile_key,
            report_window_key=window.key,
            history_days=args.days,
            bucket_minutes=args.bucket,
            min_samples=args.min_samples,
            min_distinct_days=args.min_days,
            percentiles=args.percentiles,
            max_age_seconds=args.max_age_seconds,
        )
    print(format_forecast_backtest_summary(summary, args.db))


def format_forecast_backtest_summary(summary: ForecastBacktestSummary, db_path: object) -> str:
    lines = [
        (
            f"forecast backtest window={summary.report_window_key} profile={summary.profile_key} "
            f"cases={summary.target_cases} days={summary.history_days} bucket=±{summary.bucket_minutes}m db={db_path}"
        )
    ]
    if not summary.results or all(result.evaluated_cases == 0 for result in summary.results):
        lines.append("results=none reason=insufficient_history")
        return "\n".join(lines)
    selected = summary.selected_result
    if selected is not None:
        lines.append(_selected_line(summary, selected))
    recommendation = _recommendation_line(summary)
    if recommendation:
        lines.append(recommendation)
    lines.extend(_result_line(summary, result) for result in summary.results)
    return "\n".join(lines)


def _selected_line(summary: ForecastBacktestSummary, result: ForecastBacktestResult) -> str:
    return (
        f"selected=p{result.percentile} evaluated={result.evaluated_cases}/{summary.target_cases} "
        f"miss={result.miss_cases}({result.miss_rate_percent}%) "
        f"bucket_accuracy={result.bucket_accurate_cases}({result.bucket_accuracy_percent}%) "
        f"mae={result.mean_absolute_error:.1f}"
    )


def _recommendation_line(summary: ForecastBacktestSummary) -> str:
    if len(summary.results) < 2:
        return ""
    selected = summary.selected_result
    best = summary.best_result
    if selected is None or best is None or best.evaluated_cases == 0:
        return ""
    return (
        f"recommendation selected=p{selected.percentile} best=p{best.percentile} "
        f"miss={best.miss_cases}/{best.evaluated_cases}({best.miss_rate_percent}%) "
        f"bucket_accuracy={best.bucket_accurate_cases}/{best.evaluated_cases}({best.bucket_accuracy_percent}%) "
        f"mae={best.mean_absolute_error:.1f} extra_wait={best.extra_wait_minutes}"
    )


def _result_line(summary: ForecastBacktestSummary, result: ForecastBacktestResult) -> str:
    return (
        f"p{result.percentile} evaluated={result.evaluated_cases}/{summary.target_cases} "
        f"skipped={result.skipped_cases} miss={result.miss_cases}({result.miss_rate_percent}%) "
        f"bucket_accuracy={result.bucket_accurate_cases}({result.bucket_accuracy_percent}%) "
        f"miss_minutes={result.miss_minutes} extra_wait={result.extra_wait_minutes} "
        f"mae={result.mean_absolute_error:.1f}"
    )


def _percentiles(value: str) -> tuple[int, ...]:
    parts = tuple(item.strip() for item in value.split(","))
    if not parts or any(not item for item in parts):
        raise argparse.ArgumentTypeError("expected comma-separated integers")
    try:
        items = tuple(int(item) for item in parts)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected comma-separated integers") from exc
    try:
        return validate_forecast_backtest_percentiles(items)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc
