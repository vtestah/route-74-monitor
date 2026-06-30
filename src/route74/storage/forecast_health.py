from __future__ import annotations

import json
import sqlite3
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from route74.domain.reporting import REPORT_WINDOWS, report_profiles_for_time
from route74.domain.runtime_sources import RUNTIME_SOURCE_WEB_APP
from route74.models import now_local
from route74.storage.collector_runs import (
    collector_profile_filter_includes,
    summarize_collector_runs_for_report_window,
)
from route74.storage.forecast_coverage import summarize_yandex_forecast_window_coverage
from route74.storage.forecast_validation import validate_forecast_window_coverage_inputs
from route74.storage.heartbeat import load_collector_heartbeat
from route74.storage.helpers import WEEKDAYS, count_rows, optional_int_value
from route74.storage.history import FORECAST_HISTORY_SLOT_MINUTES
from route74.storage.models import (
    CollectorWindowRunSummary,
    CountByKey,
    ForecastWindowCoverageSummary,
    percent,
)
from route74.storage.yandex_canary import (
    YandexCanaryHealth,
    summarize_yandex_canary_health,
)

HEALTHY_COLLECTOR_STATUSES = {"ok", "skipped"}
API_RISK_ALERT_PERCENT = 10
API_RISK_STATUSES = {"parse_error", "needs_signature", "blocked"}
API_RISK_REASON_PREFIXES = (
    "direction_thread_missing",
    "direction_thread_not_found",
    "vehicles_not_found",
)
ROUTE_GEOMETRY_OK_STATUSES = {"saved", "cached", "touched"}
ROUTE_GEOMETRY_RISK_STATUSES = {"thread_drift", "no_target_stop"}
TRUTH_MIN_ARRIVALS = 5
TRUTH_MIN_EVALUATIONS = 10
TRUTH_MAX_AGE_DAYS = 7
REQUIRED_CANARY_PROFILE_KEYS = tuple(dict.fromkeys(window.profile_key for window in REPORT_WINDOWS))


@dataclass(frozen=True)
class ForecastCollectorHealth:
    name: str
    status: str
    message: str
    updated_at: datetime | None
    age_seconds: int | None
    max_age_seconds: int

    @property
    def healthy(self) -> bool:
        return self.status in HEALTHY_COLLECTOR_STATUSES


@dataclass(frozen=True)
class ForecastBucketGap:
    label: str
    selected_sample_count: int
    min_samples: int
    selected_distinct_days: int
    min_distinct_days: int
    selected_bucket_minutes: int
    primary_samples: int
    fallback_samples: int
    primary_distinct_days: int
    fallback_distinct_days: int

    @property
    def sample_gap(self) -> int:
        return max(0, self.min_samples - self.selected_sample_count)

    @property
    def day_gap(self) -> int:
        return max(0, self.min_distinct_days - self.selected_distinct_days)


@dataclass(frozen=True)
class PredictionLabHealthCounts:
    arrivals: int
    predictions: int
    evaluations: int
    misses: int
    bot_predictions: int
    bot_evaluations: int
    bot_misses: int
    latest_arrival_at: datetime | None


@dataclass(frozen=True)
class ForecastWindowHealth:
    window_key: str
    profile_key: str
    status: str
    reason: str
    total_samples: int
    eta_samples: int
    fresh_eta_samples: int
    traffic_samples: int
    ready_buckets: int
    total_buckets: int
    forecast_without_report_samples: int
    report_without_forecast_samples: int
    collector_runs: int
    collector_eta_runs: int
    collector_traffic_ok_runs: int
    collector_run_statuses: tuple[CountByKey, ...]
    api_risk_samples: int
    api_risk_reasons: tuple[CountByKey, ...]
    coordinate_fallback_samples: int
    coordinate_fallback_reasons: tuple[CountByKey, ...]
    arrival_events: int
    prediction_events: int
    prediction_evaluations: int
    prediction_miss_cases: int
    bot_prediction_events: int
    bot_prediction_evaluations: int
    bot_prediction_miss_cases: int
    truth_status: str
    truth_reason: str
    latest_arrival_at: datetime | None
    collector_latest_started_at: datetime | None
    missing_bucket_labels: tuple[str, ...]
    bucket_gaps: tuple[ForecastBucketGap, ...]
    latest_sampled_at: datetime | None

    @property
    def ready(self) -> bool:
        return self.status == "ready"

    @property
    def eta_coverage_percent(self) -> int:
        return percent(self.eta_samples, self.total_samples)

    @property
    def fresh_eta_coverage_percent(self) -> int:
        return percent(self.fresh_eta_samples, self.total_samples)

    @property
    def traffic_coverage_percent(self) -> int:
        return percent(self.traffic_samples, self.total_samples)

    @property
    def readiness_percent(self) -> int:
        return percent(self.ready_buckets, self.total_buckets)

    @property
    def integrity_gap_samples(self) -> int:
        return self.forecast_without_report_samples + self.report_without_forecast_samples

    @property
    def collector_eta_run_percent(self) -> int:
        return percent(self.collector_eta_runs, self.collector_runs)

    @property
    def collector_traffic_ok_run_percent(self) -> int:
        return percent(self.collector_traffic_ok_runs, self.collector_runs)

    @property
    def api_risk_percent(self) -> int:
        return percent(self.api_risk_samples, self.total_samples)

    @property
    def coordinate_fallback_percent(self) -> int:
        return percent(self.coordinate_fallback_samples, self.total_samples)

    @property
    def prediction_miss_rate_percent(self) -> int:
        return percent(self.prediction_miss_cases, self.prediction_evaluations)

    @property
    def bot_prediction_miss_rate_percent(self) -> int:
        return percent(self.bot_prediction_miss_cases, self.bot_prediction_evaluations)


@dataclass(frozen=True)
class ForecastHealthSummary:
    days: int
    min_samples: int
    min_distinct_days: int
    collector: ForecastCollectorHealth
    canary: YandexCanaryHealth
    windows: tuple[ForecastWindowHealth, ...]

    @property
    def ready_windows(self) -> int:
        return sum(1 for window in self.windows if window.ready)

    @property
    def total_windows(self) -> int:
        return len(self.windows)

    @property
    def ready(self) -> bool:
        return self.collector.healthy and self.canary.healthy and self.ready_windows == self.total_windows


def summarize_forecast_health(
    connection: sqlite3.Connection,
    *,
    current_date: datetime | None = None,
    days: int,
    min_samples: int,
    min_distinct_days: int,
    primary_bucket_minutes: int,
    fallback_bucket_minutes: int,
    max_age_seconds: int | None,
    step_minutes: int,
    heartbeat_name: str = "yandex-collect",
    max_heartbeat_age_seconds: int = 120,
) -> ForecastHealthSummary:
    current_date = current_date or now_local()
    validate_forecast_window_coverage_inputs(
        current_date=current_date,
        days=days,
        min_samples=min_samples,
        min_distinct_days=min_distinct_days,
        primary_bucket_minutes=primary_bucket_minutes,
        fallback_bucket_minutes=fallback_bucket_minutes,
        max_age_seconds=max_age_seconds,
        step_minutes=step_minutes,
    )
    _positive_int("max_heartbeat_age_seconds", max_heartbeat_age_seconds)
    collector = _collector_health(
        connection,
        current_date=current_date,
        name=heartbeat_name,
        max_age_seconds=max_heartbeat_age_seconds,
    )
    canary = summarize_yandex_canary_health(
        connection,
        current_time=current_date,
        required_profile_keys=REQUIRED_CANARY_PROFILE_KEYS,
    )
    windows = tuple(
        _window_health(
            connection,
            summarize_yandex_forecast_window_coverage(
                connection,
                report_window=window,
                current_date=current_date,
                days=days,
                min_samples=min_samples,
                min_distinct_days=min_distinct_days,
                primary_bucket_minutes=primary_bucket_minutes,
                fallback_bucket_minutes=fallback_bucket_minutes,
                max_age_seconds=max_age_seconds,
                step_minutes=step_minutes,
            ),
            collector_runs=summarize_collector_runs_for_report_window(
                connection,
                report_window=window,
                current_date=current_date,
                days=days,
                name=heartbeat_name,
            ),
            current_date=current_date,
            days=days,
        )
        for window in REPORT_WINDOWS
    )
    return ForecastHealthSummary(
        days=days,
        min_samples=min_samples,
        min_distinct_days=min_distinct_days,
        collector=collector,
        canary=canary,
        windows=windows,
    )


def _window_health(
    connection: sqlite3.Connection,
    coverage: ForecastWindowCoverageSummary,
    *,
    collector_runs: CollectorWindowRunSummary,
    current_date: datetime,
    days: int,
) -> ForecastWindowHealth:
    forecast_gap, report_gap = _integrity_gaps(connection, coverage, current_date=current_date, days=days)
    api_risk_samples, api_risk_reasons = _api_risk(connection, coverage, current_date=current_date, days=days)
    coordinate_fallback_samples, coordinate_fallback_reasons = _coordinate_fallbacks(
        connection,
        coverage,
        current_date=current_date,
        days=days,
    )
    lab_counts = _prediction_lab_counts(connection, coverage, current_date=current_date, days=days)
    truth_status, truth_reason = _truth_status_reason(lab_counts, current_date=current_date)
    bucket_gaps = _bucket_gaps(coverage)
    missing_buckets = tuple(gap.label for gap in bucket_gaps)
    status, reason = _status_reason(
        coverage,
        forecast_gap,
        report_gap,
        collector_runs,
        bucket_gaps,
        api_risk_samples,
        api_risk_reasons,
    )
    return ForecastWindowHealth(
        window_key=coverage.window_key,
        profile_key=coverage.profile_key,
        status=status,
        reason=reason,
        total_samples=coverage.total_samples,
        eta_samples=coverage.eta_samples,
        fresh_eta_samples=coverage.fresh_eta_samples,
        traffic_samples=coverage.traffic_samples,
        ready_buckets=coverage.ready_buckets,
        total_buckets=coverage.total_buckets,
        forecast_without_report_samples=forecast_gap,
        report_without_forecast_samples=report_gap,
        collector_runs=collector_runs.total_runs,
        collector_eta_runs=collector_runs.eta_runs,
        collector_traffic_ok_runs=collector_runs.traffic_ok_runs,
        collector_run_statuses=collector_runs.statuses,
        api_risk_samples=api_risk_samples,
        api_risk_reasons=api_risk_reasons,
        coordinate_fallback_samples=coordinate_fallback_samples,
        coordinate_fallback_reasons=coordinate_fallback_reasons,
        arrival_events=lab_counts.arrivals,
        prediction_events=lab_counts.predictions,
        prediction_evaluations=lab_counts.evaluations,
        prediction_miss_cases=lab_counts.misses,
        bot_prediction_events=lab_counts.bot_predictions,
        bot_prediction_evaluations=lab_counts.bot_evaluations,
        bot_prediction_miss_cases=lab_counts.bot_misses,
        truth_status=truth_status,
        truth_reason=truth_reason,
        latest_arrival_at=lab_counts.latest_arrival_at,
        collector_latest_started_at=collector_runs.latest_started_at,
        missing_bucket_labels=missing_buckets,
        bucket_gaps=bucket_gaps,
        latest_sampled_at=coverage.latest_sampled_at,
    )


def _status_reason(
    coverage: ForecastWindowCoverageSummary,
    forecast_gap: int,
    report_gap: int,
    collector_runs: CollectorWindowRunSummary,
    bucket_gaps: tuple[ForecastBucketGap, ...],
    api_risk_samples: int,
    api_risk_reasons: tuple[CountByKey, ...],
) -> tuple[str, str]:
    if forecast_gap or report_gap:
        return (
            "integrity_gap",
            f"forecast/report-window tables disagree: forecast_only={forecast_gap}, report_only={report_gap}",
        )
    if coverage.total_samples == 0 and collector_runs.total_runs == 0:
        return (
            "no_collector_runs",
            "collector has no recorded runs in this report window",
        )
    if coverage.total_samples == 0:
        return (
            "no_samples",
            f"collector ran {collector_runs.total_runs} times, but no report-window forecast samples were stored",
        )
    if api_risk_samples > 0 and percent(api_risk_samples, coverage.total_samples) >= API_RISK_ALERT_PERCENT:
        return (
            "api_contract_risk",
            f"Yandex API/route contract risk: {_count_reason(api_risk_reasons)}",
        )
    if coverage.eta_samples == 0:
        return "no_eta", "samples exist, but Yandex did not provide ETA"
    if coverage.fresh_eta_samples == 0:
        return (
            "stale_eta",
            "ETA samples exist, but vehicle freshness cutoff rejects them",
        )
    if coverage.ready_buckets < coverage.total_buckets:
        return (
            "insufficient_bucket_coverage",
            f"missing ready buckets: {_bucket_gap_reason(bucket_gaps)}",
        )
    return "ready", "all report-window buckets have enough fresh ETA samples"


def _api_risk(
    connection: sqlite3.Connection,
    coverage: ForecastWindowCoverageSummary,
    *,
    current_date: datetime,
    days: int,
) -> tuple[int, tuple[CountByKey, ...]]:
    since = (current_date - timedelta(days=days)).isoformat()
    rows = connection.execute(
        """
        SELECT source_status, fallback_reason, raw_json, vehicle_count
             , service_date, minute_of_day
        FROM yandex_forecast_samples
        WHERE report_window_key = ?
          AND profile_key = ?
          AND sampled_at >= ?
          AND weekday IN (?,?,?,?,?)
        ORDER BY sampled_at DESC, id DESC
        """,
        (coverage.window_key, coverage.profile_key, since, *WEEKDAYS),
    ).fetchall()
    reasons_by_slot: dict[tuple[str, int], str] = {}
    for row in rows:
        reason = _api_risk_reason(
            str(row["source_status"]),
            str(row["fallback_reason"]),
            _route_geometry_status(str(row["raw_json"])),
            _vehicle_count(row),
        )
        slot_key = _sample_slot_key(row)
        if reason and slot_key is not None:
            reasons_by_slot.setdefault(slot_key, reason)
    reasons: Counter[str] = Counter(reasons_by_slot.values())
    return sum(reasons.values()), count_rows(reasons)


def _coordinate_fallbacks(
    connection: sqlite3.Connection,
    coverage: ForecastWindowCoverageSummary,
    *,
    current_date: datetime,
    days: int,
) -> tuple[int, tuple[CountByKey, ...]]:
    since = (current_date - timedelta(days=days)).isoformat()
    rows = connection.execute(
        """
        SELECT source_status, fallback_reason, raw_json, vehicle_count, arrival_minutes
             , service_date, minute_of_day
        FROM yandex_forecast_samples
        WHERE report_window_key = ?
          AND profile_key = ?
          AND sampled_at >= ?
          AND weekday IN (?,?,?,?,?)
        ORDER BY sampled_at DESC, id DESC
        """,
        (coverage.window_key, coverage.profile_key, since, *WEEKDAYS),
    ).fetchall()
    reasons_by_slot: dict[tuple[str, int], str] = {}
    for row in rows:
        if row["arrival_minutes"] is not None:
            continue
        status = str(row["source_status"])
        fallback_reason = str(row["fallback_reason"])
        route_geometry_status = _route_geometry_status(str(row["raw_json"]))
        if not _has_degraded_route_signal(status, route_geometry_status, _vehicle_count(row)):
            continue
        slot_key = _sample_slot_key(row)
        if slot_key is None:
            continue
        reasons_by_slot.setdefault(
            slot_key,
            _coordinate_fallback_reason(fallback_reason, route_geometry_status),
        )
    reasons: Counter[str] = Counter(reasons_by_slot.values())
    return sum(reasons.values()), count_rows(reasons)


def _vehicle_count(row: sqlite3.Row) -> int:
    return max(0, optional_int_value(row["vehicle_count"]) or 0)


def _sample_slot_key(row: sqlite3.Row) -> tuple[str, int] | None:
    minute_of_day = optional_int_value(row["minute_of_day"])
    if minute_of_day is None or not 0 <= minute_of_day < 24 * 60:
        return None
    service_date = str(row["service_date"]).strip()
    if not service_date:
        return None
    return service_date, minute_of_day // FORECAST_HISTORY_SLOT_MINUTES


def _prediction_lab_counts(
    connection: sqlite3.Connection,
    coverage: ForecastWindowCoverageSummary,
    *,
    current_date: datetime,
    days: int,
) -> PredictionLabHealthCounts:
    since = (current_date - timedelta(days=days)).isoformat()
    arrivals = connection.execute(
        """
        SELECT COUNT(*) AS count, MAX(arrival_events.arrived_at) AS latest_arrival_at
        FROM arrival_events
        JOIN yandex_forecast_samples ON yandex_forecast_samples.yandex_snapshot_id = arrival_events.yandex_snapshot_id
        WHERE arrival_events.profile_key = ?
          AND yandex_forecast_samples.report_window_key = ?
          AND arrival_events.arrived_at >= ?
        """,
        (coverage.profile_key, coverage.window_key, since),
    ).fetchone()
    predictions = connection.execute(
        """
        SELECT
            COUNT(*) AS count,
            SUM(CASE WHEN runtime_source = ? THEN 1 ELSE 0 END) AS bot_count
        FROM prediction_events
        WHERE profile_key = ? AND report_window_key = ? AND sampled_at >= ?
        """,
        (RUNTIME_SOURCE_WEB_APP, coverage.profile_key, coverage.window_key, since),
    ).fetchone()
    evaluations = connection.execute(
        """
        SELECT
            COUNT(*) AS count,
            SUM(CASE WHEN prediction_evaluations.error_minutes < 0 THEN 1 ELSE 0 END) AS misses,
            SUM(CASE WHEN prediction_events.runtime_source = ? THEN 1 ELSE 0 END) AS bot_count,
            SUM(
                CASE
                    WHEN prediction_events.runtime_source = ?
                     AND prediction_evaluations.error_minutes < 0
                    THEN 1 ELSE 0
                END
            ) AS bot_misses
        FROM prediction_evaluations
        JOIN prediction_events ON prediction_events.id = prediction_evaluations.prediction_event_id
        WHERE prediction_evaluations.profile_key = ?
          AND prediction_events.report_window_key = ?
          AND prediction_events.sampled_at >= ?
        """,
        (
            RUNTIME_SOURCE_WEB_APP,
            RUNTIME_SOURCE_WEB_APP,
            coverage.profile_key,
            coverage.window_key,
            since,
        ),
    ).fetchone()
    return PredictionLabHealthCounts(
        arrivals=int(arrivals["count"]),
        predictions=int(predictions["count"]),
        evaluations=int(evaluations["count"]),
        misses=int(evaluations["misses"] or 0),
        bot_predictions=int(predictions["bot_count"] or 0),
        bot_evaluations=int(evaluations["bot_count"] or 0),
        bot_misses=int(evaluations["bot_misses"] or 0),
        latest_arrival_at=_optional_datetime(arrivals["latest_arrival_at"]),
    )


def _truth_status_reason(counts: PredictionLabHealthCounts, *, current_date: datetime) -> tuple[str, str]:
    if counts.arrivals < TRUTH_MIN_ARRIVALS:
        return "insufficient", f"arrival facts {counts.arrivals}/{TRUTH_MIN_ARRIVALS}"
    if counts.evaluations < TRUTH_MIN_EVALUATIONS:
        return (
            "warming_up",
            f"evaluated predictions {counts.evaluations}/{TRUTH_MIN_EVALUATIONS}",
        )
    if counts.latest_arrival_at is None:
        return "insufficient", "latest arrival is missing"
    age_days = _age_seconds(current_date, counts.latest_arrival_at) // 86_400
    if age_days > TRUTH_MAX_AGE_DAYS:
        return "stale", f"latest arrival is {age_days}d old"
    return "ready", "arrival facts and evaluated predictions are sufficient"


def _optional_datetime(value: object) -> datetime | None:
    if value is None:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def _api_risk_reason(
    status: str,
    fallback_reason: str,
    route_geometry_status: str = "",
    vehicle_count: int = 0,
) -> str:
    if status in API_RISK_STATUSES:
        return status
    if route_geometry_status in ROUTE_GEOMETRY_RISK_STATUSES:
        return f"route_geometry_{route_geometry_status}"
    for part in fallback_reason.split(";"):
        normalized = part.strip()
        for prefix in API_RISK_REASON_PREFIXES:
            if normalized.startswith(prefix) or normalized.endswith(f":{prefix}"):
                if _has_degraded_route_signal(status, route_geometry_status, vehicle_count):
                    continue
                return prefix
    return ""


def _has_degraded_route_signal(status: str, route_geometry_status: str, vehicle_count: int) -> bool:
    if route_geometry_status in ROUTE_GEOMETRY_RISK_STATUSES:
        return False
    return route_geometry_status in ROUTE_GEOMETRY_OK_STATUSES or (status == "coordinates_only" and vehicle_count > 0)


def _coordinate_fallback_reason(fallback_reason: str, route_geometry_status: str) -> str:
    parts = tuple(part.strip() for part in fallback_reason.split(";") if part.strip())
    for normalized in parts:
        for prefix in API_RISK_REASON_PREFIXES:
            if normalized.startswith(prefix) or normalized.endswith(f":{prefix}"):
                return prefix
    for normalized in parts:
        return normalized.split(":", 1)[0]
    return f"route_geometry_{route_geometry_status}" if route_geometry_status else "coordinates_only"


def _route_geometry_status(raw_json: str) -> str:
    try:
        raw = json.loads(raw_json)
    except json.JSONDecodeError:
        return ""
    if not isinstance(raw, dict):
        return ""
    status = raw.get("route_geometry_status")
    return status if isinstance(status, str) else ""


def _count_reason(counts: tuple[CountByKey, ...]) -> str:
    return ", ".join(f"{item.key}={item.count}" for item in counts) or "-"


def _bucket_gaps(
    coverage: ForecastWindowCoverageSummary,
) -> tuple[ForecastBucketGap, ...]:
    return tuple(
        ForecastBucketGap(
            label=bucket.label,
            selected_sample_count=bucket.selected_sample_count,
            min_samples=coverage.min_samples,
            selected_distinct_days=bucket.selected_distinct_days,
            min_distinct_days=coverage.min_distinct_days,
            selected_bucket_minutes=bucket.selected_bucket_minutes,
            primary_samples=bucket.primary_samples,
            fallback_samples=bucket.fallback_samples,
            primary_distinct_days=bucket.primary_distinct_days,
            fallback_distinct_days=bucket.fallback_distinct_days,
        )
        for bucket in coverage.buckets
        if not bucket.ready
    )


def _bucket_gap_reason(bucket_gaps: tuple[ForecastBucketGap, ...]) -> str:
    return ", ".join(
        (
            f"{gap.label}({gap.selected_sample_count}/{gap.min_samples} samples, "
            f"{gap.selected_distinct_days}/{gap.min_distinct_days} days, ±{gap.selected_bucket_minutes}m)"
        )
        for gap in bucket_gaps
    )


def _integrity_gaps(
    connection: sqlite3.Connection,
    coverage: ForecastWindowCoverageSummary,
    *,
    current_date: datetime,
    days: int,
) -> tuple[int, int]:
    since = (current_date - timedelta(days=days)).isoformat()
    return (
        _forecast_without_report_count(connection, coverage, since),
        _report_without_forecast_count(connection, coverage, since),
    )


def _forecast_without_report_count(
    connection: sqlite3.Connection,
    coverage: ForecastWindowCoverageSummary,
    since: str,
) -> int:
    row = connection.execute(
        """
        SELECT COUNT(*) AS count
        FROM yandex_forecast_samples AS forecast
        LEFT JOIN report_window_snapshots AS report
          ON report.yandex_snapshot_id = forecast.yandex_snapshot_id
         AND report.report_window_key = forecast.report_window_key
         AND report.profile_key = forecast.profile_key
        WHERE forecast.report_window_key = ?
          AND forecast.profile_key = ?
          AND forecast.sampled_at >= ?
          AND report.id IS NULL
        """,
        (coverage.window_key, coverage.profile_key, since),
    ).fetchone()
    return int(row["count"])


def _report_without_forecast_count(
    connection: sqlite3.Connection,
    coverage: ForecastWindowCoverageSummary,
    since: str,
) -> int:
    row = connection.execute(
        """
        SELECT COUNT(*) AS count
        FROM report_window_snapshots AS report
        LEFT JOIN yandex_forecast_samples AS forecast
          ON forecast.yandex_snapshot_id = report.yandex_snapshot_id
         AND forecast.report_window_key = report.report_window_key
         AND forecast.profile_key = report.profile_key
        WHERE report.report_window_key = ?
          AND report.profile_key = ?
          AND report.sampled_at >= ?
          AND forecast.id IS NULL
        """,
        (coverage.window_key, coverage.profile_key, since),
    ).fetchone()
    return int(row["count"])


def _collector_health(
    connection: sqlite3.Connection,
    *,
    current_date: datetime,
    name: str,
    max_age_seconds: int,
) -> ForecastCollectorHealth:
    heartbeat = load_collector_heartbeat(connection, name)
    if heartbeat is None:
        return ForecastCollectorHealth(
            name,
            "missing",
            "collector heartbeat is absent",
            None,
            None,
            max_age_seconds,
        )
    age_seconds = _age_seconds(current_date, heartbeat.updated_at)
    if age_seconds > max_age_seconds:
        return ForecastCollectorHealth(
            name,
            "stale",
            heartbeat.last_message,
            heartbeat.updated_at,
            age_seconds,
            max_age_seconds,
        )
    active_profiles = report_profiles_for_time(current_date)
    if heartbeat.last_status == "skipped" and _profile_filter_matches(heartbeat.profile_filter, active_profiles):
        return ForecastCollectorHealth(
            name,
            "unexpected_skipped",
            f"{heartbeat.last_message}; skipped during active report window",
            heartbeat.updated_at,
            age_seconds,
            max_age_seconds,
        )
    return ForecastCollectorHealth(
        name,
        heartbeat.last_status,
        heartbeat.last_message,
        heartbeat.updated_at,
        age_seconds,
        max_age_seconds,
    )


def _age_seconds(current_date: datetime, updated_at: datetime) -> int:
    current = current_date if current_date.tzinfo is not None else current_date.replace(tzinfo=UTC)
    updated = updated_at if updated_at.tzinfo is not None else updated_at.replace(tzinfo=UTC)
    return max(0, round((current - updated).total_seconds()))


def _profile_filter_matches(profile_filter: str, profile_keys: tuple[str, ...]) -> bool:
    if not profile_keys:
        return False
    if profile_filter == "all":
        return True
    return any(collector_profile_filter_includes(profile_filter, profile_key) for profile_key in profile_keys)


def _positive_int(name: str, value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
