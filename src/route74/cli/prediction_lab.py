from __future__ import annotations

import argparse

from route74.cli.common import positive_int
from route74.cli.forecast_readiness import WINDOWS_BY_KEY
from route74.domain.profiles import ALL_PROFILES_KEY, PROFILE_SELECTORS
from route74.storage import (
    PredictionLabBackfillResult,
    backfill_prediction_lab,
    connect,
    evaluate_pending_predictions,
    init_db,
    load_arrival_events,
    summarize_prediction_lab_calibration,
    summarize_prediction_lab_window,
)
from route74.storage.prediction_lab import (
    RUNTIME_RELIABILITY_MIN_SAMPLES,
    SOURCE_RELIABILITY_MIN_SAMPLES,
    PredictionLabCalibrationBucket,
    PredictionLabCalibrationSummary,
    PredictionLabSummary,
)


def register_prediction_lab_commands(subparsers: argparse._SubParsersAction) -> None:
    arrivals = subparsers.add_parser("arrival-events", help="List factual target-stop arrival events.")
    arrivals.add_argument("--window", choices=tuple(WINDOWS_BY_KEY), required=True)
    arrivals.add_argument("--limit", type=positive_int, default=20)
    arrivals.set_defaults(func=cmd_arrival_events)

    evaluate = subparsers.add_parser("prediction-evaluate", help="Evaluate pending prediction events.")
    evaluate.add_argument("--window", choices=tuple(WINDOWS_BY_KEY), required=True)
    evaluate.set_defaults(func=cmd_prediction_evaluate)

    lab = subparsers.add_parser("prediction-lab", help="Summarize prediction quality by source.")
    lab.add_argument("--window", choices=tuple(WINDOWS_BY_KEY), required=True)
    lab.set_defaults(func=cmd_prediction_lab)

    calibration = subparsers.add_parser(
        "prediction-calibration",
        help="Show source and bucket ETA calibration guardrails.",
    )
    calibration.add_argument("--window", choices=tuple(WINDOWS_BY_KEY), required=True)
    calibration.set_defaults(func=cmd_prediction_calibration)

    backfill = subparsers.add_parser("prediction-backfill", help="Rebuild prediction lab events from stored Yandex snapshots.")
    backfill.add_argument("--window", choices=(*WINDOWS_BY_KEY, "all"), default="all")
    backfill.add_argument("--profile", choices=PROFILE_SELECTORS, default=ALL_PROFILES_KEY)
    backfill.add_argument("--reset-existing", action="store_true", help="Delete and rebuild existing lab events for selected snapshots.")
    backfill.set_defaults(func=cmd_prediction_backfill)


def cmd_arrival_events(args: argparse.Namespace) -> None:
    window = WINDOWS_BY_KEY[args.window]
    with connect(args.db) as connection:
        init_db(connection)
        events = load_arrival_events(
            connection,
            profile_key=window.profile_key,
            report_window_key=window.key,
            limit=args.limit,
        )
    lines = [f"arrival events window={window.key} profile={window.profile_key} count={len(events)} db={args.db}"]
    for event in events:
        vehicle = event.vehicle_id or "-"
        lines.append(
            f"- {event.arrived_at:%Y-%m-%d %H:%M} vehicle={vehicle} "
            f"source={event.source} confidence={event.confidence} stop={event.stop_id}"
        )
    print("\n".join(lines))


def cmd_prediction_evaluate(args: argparse.Namespace) -> None:
    window = WINDOWS_BY_KEY[args.window]
    with connect(args.db) as connection:
        init_db(connection)
        inserted = evaluate_pending_predictions(
            connection,
            profile_key=window.profile_key,
            report_window_key=window.key,
        )
        summary = summarize_prediction_lab_window(
            connection,
            profile_key=window.profile_key,
            report_window_key=window.key,
        )
    print(format_prediction_lab_summary(summary, args.db, prefix=f"prediction evaluate inserted={inserted}"))


def cmd_prediction_lab(args: argparse.Namespace) -> None:
    window = WINDOWS_BY_KEY[args.window]
    with connect(args.db) as connection:
        init_db(connection)
        summary = summarize_prediction_lab_window(
            connection,
            profile_key=window.profile_key,
            report_window_key=window.key,
        )
    print(format_prediction_lab_summary(summary, args.db, prefix="prediction lab"))


def cmd_prediction_calibration(args: argparse.Namespace) -> None:
    window = WINDOWS_BY_KEY[args.window]
    with connect(args.db) as connection:
        init_db(connection)
        summary = summarize_prediction_lab_calibration(
            connection,
            profile_key=window.profile_key,
            report_window_key=window.key,
        )
    print(format_prediction_lab_calibration(summary, args.db))


def cmd_prediction_backfill(args: argparse.Namespace) -> None:
    if args.window != "all":
        window = WINDOWS_BY_KEY[args.window]
        profile_key = window.profile_key
        report_window_key = window.key
    else:
        profile_key = None if args.profile == ALL_PROFILES_KEY else args.profile
        report_window_key = None
    with connect(args.db) as connection:
        init_db(connection)
        result = backfill_prediction_lab(
            connection,
            profile_key=profile_key,
            report_window_key=report_window_key,
            reset_existing=args.reset_existing,
        )
    print(format_prediction_lab_backfill(result, args.db))


def format_prediction_lab_backfill(result: PredictionLabBackfillResult, db_path: object) -> str:
    return (
        f"prediction backfill scanned={result.snapshots_scanned} replayed={result.snapshots_replayed} "
        f"skipped_existing={result.snapshots_skipped_existing} "
        f"predictions_created={result.prediction_events_created} "
        f"arrivals_created={result.arrival_events_created} evaluations_created={result.evaluations_created} "
        f"db={db_path}"
    )


def format_prediction_lab_summary(summary: PredictionLabSummary, db_path: object, *, prefix: str) -> str:
    latest_arrival = summary.latest_arrival_at.strftime("%Y-%m-%d %H:%M") if summary.latest_arrival_at else "-"
    latest_prediction = summary.latest_prediction_at.strftime("%Y-%m-%d %H:%M") if summary.latest_prediction_at else "-"
    lines = [
        (
            f"{prefix} window={summary.window_key} profile={summary.profile_key} "
            f"arrivals={summary.arrival_events} predictions={summary.prediction_events} "
            f"evaluated={summary.evaluated_predictions} latest_arrival={latest_arrival} "
            f"latest_prediction={latest_prediction} db={db_path}"
        )
    ]
    if not summary.sources:
        lines.append("sources=none")
        return "\n".join(lines)
    for source in summary.sources:
        lines.append(
            f"source={source.source} evaluated={source.evaluated_predictions} "
            f"miss={source.miss_cases}({source.miss_rate_percent}%) "
            f"miss_minutes={source.miss_minutes} extra_wait={source.extra_wait_minutes} "
            f"mae={source.mean_absolute_error:.1f}"
        )
    return "\n".join(lines)


def format_prediction_lab_calibration(summary: PredictionLabCalibrationSummary, db_path: object) -> str:
    current_time = summary.current_time.strftime("%Y-%m-%d %H:%M")
    lines = [
        (
            f"prediction calibration window={summary.window_key} profile={summary.profile_key} "
            f"at={current_time} buckets={len(summary.buckets)} db={db_path}"
        )
    ]
    if not summary.buckets:
        lines.append("buckets=none")
        return "\n".join(lines)
    for bucket in summary.buckets:
        reliability = bucket.effective_reliability
        lines.append(
            f"source={bucket.source} bucket={bucket.bucket} samples={bucket.evaluated_predictions} "
            f"miss={bucket.miss_cases}({bucket.miss_rate_percent}%) p10={bucket.p10_error_minutes}m "
            f"reliability_samples={reliability.sample_count} reliability_scope={reliability.scope} "
            f"reliability_reason={bucket.effective_reliability_reason} "
            f"buffer={reliability.safety_buffer_minutes}m {_format_correction(bucket)} "
            f"{_format_runtime_reliability(bucket)} "
            f"action={_calibration_action(bucket)}"
        )
    return "\n".join(lines)


def _format_correction(bucket: PredictionLabCalibrationBucket) -> str:
    correction = bucket.residual_correction
    if correction is None:
        return "correction=na"
    return (
        f"correction={correction.correction_minutes}m "
        f"correction_samples={correction.sample_count} correction_scope={correction.scope}"
    )


def _format_runtime_reliability(bucket: PredictionLabCalibrationBucket) -> str:
    baseline = bucket.reliability
    runtime = bucket.runtime_reliability
    return (
        f"baseline_samples={baseline.sample_count} baseline_scope={baseline.scope} "
        f"baseline_buffer={baseline.safety_buffer_minutes}m "
        f"baseline_miss={baseline.miss_cases}({baseline.miss_rate_percent}%) "
        f"runtime_samples={runtime.sample_count} runtime_scope={runtime.scope} "
        f"runtime_miss={runtime.miss_cases}({runtime.miss_rate_percent}%) "
        f"runtime_buffer={runtime.safety_buffer_minutes}m"
    )


def _calibration_action(bucket: PredictionLabCalibrationBucket) -> str:
    reliability = bucket.effective_reliability
    correction = bucket.residual_correction
    if reliability is bucket.runtime_reliability and reliability.safety_buffer_minutes > 0:
        return "apply_runtime_buffer"
    if bucket.effective_reliability_reason == "runtime_miss_rate":
        return "review_runtime_miss_rate"
    if reliability.safety_buffer_minutes > 0:
        return "apply_buffer"
    if correction is not None and correction.correction_minutes < 0:
        return "bias_correction"
    if (
        bucket.reliability.sample_count < SOURCE_RELIABILITY_MIN_SAMPLES
        and bucket.runtime_reliability.sample_count < RUNTIME_RELIABILITY_MIN_SAMPLES
    ):
        return "collect_more"
    return "ok"
