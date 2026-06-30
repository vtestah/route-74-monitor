from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from math import isfinite

from route74.domain.traffic import RouteTrafficSnapshot


@dataclass(frozen=True)
class YandexObservation:
    profile_key: str
    source_method: str
    source_status: str
    vehicle_id: str
    thread_id: str
    lat: float | None
    lng: float | None
    arrival_minutes: int | None
    age_seconds: int | None
    sampled_at: datetime

    def __post_init__(self) -> None:
        _ensure_plain_key("Yandex observation profile key", self.profile_key)
        _ensure_plain_key("Yandex observation source method", self.source_method)
        _ensure_plain_key("Yandex observation source status", self.source_status)
        _ensure_single_line_text("Yandex observation vehicle id", self.vehicle_id)
        _ensure_single_line_text("Yandex observation thread id", self.thread_id, allow_empty=True)
        _ensure_coordinate_pair("Yandex observation coordinates", self.lat, self.lng)
        _ensure_optional_count("Yandex observation arrival minutes", self.arrival_minutes)
        _ensure_optional_count("Yandex observation age seconds", self.age_seconds)
        _ensure_aware_datetime("Yandex observation sampled_at", self.sampled_at)


@dataclass(frozen=True)
class CollectorHeartbeat:
    name: str
    updated_at: datetime
    pid: int
    profile_filter: str
    last_status: str
    last_message: str

    def __post_init__(self) -> None:
        _ensure_plain_key("collector heartbeat name", self.name)
        _ensure_aware_datetime("collector heartbeat updated_at", self.updated_at)
        _ensure_positive_count("collector heartbeat pid", self.pid)
        _ensure_text("collector heartbeat profile filter", self.profile_filter)
        _ensure_text("collector heartbeat status", self.last_status)
        _ensure_text("collector heartbeat message", self.last_message)


@dataclass(frozen=True)
class CountByKey:
    key: str
    count: int

    def __post_init__(self) -> None:
        if not isinstance(self.key, str):
            raise ValueError("count key needs text")
        object.__setattr__(self, "key", _normalize_count_key(self.key))
        _ensure_count("count", self.count)


@dataclass(frozen=True)
class CollectorRunSummary:
    name: str
    hours: int
    total_runs: int
    result_runs: int
    eta_runs: int
    traffic_ok_runs: int
    skipped_runs: int
    latest_started_at: datetime | None
    statuses: tuple[CountByKey, ...]

    def __post_init__(self) -> None:
        _ensure_plain_key("collector run summary name", self.name)
        _ensure_positive_count("collector run summary hours", self.hours)
        _ensure_collector_run_counts(
            "collector run summary",
            total_runs=self.total_runs,
            result_runs=self.result_runs,
            eta_runs=self.eta_runs,
            traffic_ok_runs=self.traffic_ok_runs,
            skipped_runs=self.skipped_runs,
        )
        _ensure_optional_aware_datetime(
            "collector run summary latest_started_at",
            self.latest_started_at,
        )
        _ensure_count_tuple("collector run summary statuses", self.statuses)

    @property
    def eta_run_percent(self) -> int:
        return percent(self.eta_runs, self.total_runs)

    @property
    def traffic_ok_run_percent(self) -> int:
        return percent(self.traffic_ok_runs, self.total_runs)


@dataclass(frozen=True)
class CollectorWindowRunSummary:
    window_key: str
    profile_key: str
    total_runs: int
    result_runs: int
    eta_runs: int
    traffic_ok_runs: int
    skipped_runs: int
    latest_started_at: datetime | None
    statuses: tuple[CountByKey, ...]

    def __post_init__(self) -> None:
        _ensure_plain_key("collector window run summary window key", self.window_key)
        _ensure_plain_key("collector window run summary profile key", self.profile_key)
        _ensure_collector_run_counts(
            "collector window run summary",
            total_runs=self.total_runs,
            result_runs=self.result_runs,
            eta_runs=self.eta_runs,
            traffic_ok_runs=self.traffic_ok_runs,
            skipped_runs=self.skipped_runs,
        )
        _ensure_optional_aware_datetime(
            "collector window run summary latest_started_at",
            self.latest_started_at,
        )
        _ensure_count_tuple("collector window run summary statuses", self.statuses)

    @property
    def eta_run_percent(self) -> int:
        return percent(self.eta_runs, self.total_runs)

    @property
    def traffic_ok_run_percent(self) -> int:
        return percent(self.traffic_ok_runs, self.total_runs)


@dataclass(frozen=True)
class YandexTelemetrySummary:
    profile_key: str | None
    hours: int
    total_snapshots: int
    eta_snapshots: int
    vehicle_snapshots: int
    total_observations: int
    eta_observations: int
    latest_sampled_at: datetime | None
    heartbeat: CollectorHeartbeat | None
    collector_runs: CollectorRunSummary
    statuses: tuple[CountByKey, ...]
    methods: tuple[CountByKey, ...]

    def __post_init__(self) -> None:
        if self.profile_key is not None:
            _ensure_text("Yandex telemetry profile key", self.profile_key)
        _ensure_positive_count("Yandex telemetry hours", self.hours)
        _ensure_count_not_above(
            "Yandex telemetry ETA snapshots",
            self.eta_snapshots,
            self.total_snapshots,
            "total snapshots",
        )
        _ensure_count_not_above(
            "Yandex telemetry vehicle snapshots",
            self.vehicle_snapshots,
            self.total_snapshots,
            "total snapshots",
        )
        _ensure_count_not_above(
            "Yandex telemetry ETA observations",
            self.eta_observations,
            self.total_observations,
            "total observations",
        )
        _ensure_optional_aware_datetime(
            "Yandex telemetry latest_sampled_at",
            self.latest_sampled_at,
        )
        if self.heartbeat is not None and not isinstance(self.heartbeat, CollectorHeartbeat):
            raise ValueError("Yandex telemetry heartbeat needs CollectorHeartbeat")
        if not isinstance(self.collector_runs, CollectorRunSummary):
            raise ValueError("Yandex telemetry collector runs need CollectorRunSummary")
        if self.collector_runs.hours != self.hours:
            raise ValueError("Yandex telemetry collector runs hours must match hours")
        _ensure_count_tuple("Yandex telemetry statuses", self.statuses)
        _ensure_count_tuple("Yandex telemetry methods", self.methods)

    @property
    def eta_coverage_percent(self) -> int:
        return percent(self.eta_snapshots, self.total_snapshots)

    @property
    def vehicle_coverage_percent(self) -> int:
        return percent(self.vehicle_snapshots, self.total_snapshots)


@dataclass(frozen=True)
class ReportWindowSummary:
    days: int
    report_window_key: str | None
    profile_key: str | None
    total_samples: int
    eta_samples: int
    traffic_samples: int
    latest_sampled_at: datetime | None
    statuses: tuple[CountByKey, ...]

    def __post_init__(self) -> None:
        _ensure_positive_count("report window days", self.days)
        _ensure_count_not_above(
            "report window ETA samples",
            self.eta_samples,
            self.total_samples,
            "total samples",
        )
        _ensure_count_not_above(
            "report window traffic samples",
            self.traffic_samples,
            self.total_samples,
            "total samples",
        )
        _ensure_optional_aware_datetime("report window latest_sampled_at", self.latest_sampled_at)
        _ensure_count_tuple("report window statuses", self.statuses)

    @property
    def eta_coverage_percent(self) -> int:
        return percent(self.eta_samples, self.total_samples)

    @property
    def traffic_coverage_percent(self) -> int:
        return percent(self.traffic_samples, self.total_samples)


@dataclass(frozen=True)
class ForecastReadinessSummary:
    profile_key: str
    report_window_key: str | None
    current_time: datetime
    days: int
    min_samples: int
    min_distinct_days: int
    primary_bucket_minutes: int
    fallback_bucket_minutes: int
    max_age_seconds: int | None
    total_samples: int
    eta_samples: int
    fresh_eta_samples: int
    traffic_samples: int
    primary_samples: int
    fallback_samples: int
    primary_distinct_days: int
    fallback_distinct_days: int
    latest_sampled_at: datetime | None

    def __post_init__(self) -> None:
        _ensure_aware_datetime("forecast readiness current_time", self.current_time)
        _ensure_positive_count("forecast readiness days", self.days)
        _ensure_positive_count("forecast readiness min samples", self.min_samples)
        _ensure_positive_count("forecast readiness min distinct days", self.min_distinct_days)
        _ensure_positive_count(
            "forecast readiness primary bucket minutes",
            self.primary_bucket_minutes,
        )
        _ensure_positive_count(
            "forecast readiness fallback bucket minutes",
            self.fallback_bucket_minutes,
        )
        _ensure_optional_count("forecast readiness max age seconds", self.max_age_seconds)
        if self.min_distinct_days > self.min_samples:
            raise ValueError("forecast readiness min distinct days must not exceed min samples")
        if self.fallback_bucket_minutes < self.primary_bucket_minutes:
            raise ValueError("forecast readiness fallback bucket must not be below primary bucket")
        _ensure_sample_counts(
            "forecast readiness",
            total_samples=self.total_samples,
            eta_samples=self.eta_samples,
            fresh_eta_samples=self.fresh_eta_samples,
            traffic_samples=self.traffic_samples,
        )
        _ensure_count_not_above(
            "forecast readiness primary samples",
            self.primary_samples,
            self.fresh_eta_samples,
            "fresh ETA samples",
        )
        _ensure_count_not_above(
            "forecast readiness fallback samples",
            self.fallback_samples,
            self.fresh_eta_samples,
            "fresh ETA samples",
        )
        _ensure_distinct_days_fit_samples(
            "primary forecast readiness",
            self.primary_distinct_days,
            self.primary_samples,
        )
        _ensure_distinct_days_fit_samples(
            "fallback forecast readiness",
            self.fallback_distinct_days,
            self.fallback_samples,
        )
        _ensure_optional_aware_datetime("forecast readiness latest_sampled_at", self.latest_sampled_at)

    @property
    def ready(self) -> bool:
        return self._primary_ready or self._fallback_ready

    @property
    def _primary_ready(self) -> bool:
        return self.primary_samples >= self.min_samples and self.primary_distinct_days >= self.min_distinct_days

    @property
    def _fallback_ready(self) -> bool:
        return self.fallback_samples >= self.min_samples and self.fallback_distinct_days >= self.min_distinct_days

    @property
    def selected_bucket_minutes(self) -> int:
        if self._selected_is_primary:
            return self.primary_bucket_minutes
        return self.fallback_bucket_minutes

    @property
    def selected_sample_count(self) -> int:
        if self._selected_is_primary:
            return self.primary_samples
        return self.fallback_samples

    @property
    def selected_distinct_days(self) -> int:
        if self._selected_is_primary:
            return self.primary_distinct_days
        return self.fallback_distinct_days

    @property
    def _selected_is_primary(self) -> bool:
        if self._primary_ready:
            return True
        if self._fallback_ready:
            return False
        return (self.primary_samples, self.primary_distinct_days) >= (
            self.fallback_samples,
            self.fallback_distinct_days,
        )

    @property
    def eta_coverage_percent(self) -> int:
        return percent(self.eta_samples, self.total_samples)

    @property
    def fresh_eta_coverage_percent(self) -> int:
        return percent(self.fresh_eta_samples, self.total_samples)

    @property
    def traffic_coverage_percent(self) -> int:
        return percent(self.traffic_samples, self.total_samples)


@dataclass(frozen=True)
class ForecastCoverageBucket:
    label: str
    ready: bool
    selected_sample_count: int
    selected_distinct_days: int
    selected_bucket_minutes: int
    primary_samples: int
    fallback_samples: int
    primary_distinct_days: int
    fallback_distinct_days: int

    def __post_init__(self) -> None:
        if not isinstance(self.ready, bool):
            raise ValueError("forecast coverage ready needs bool")
        _ensure_positive_count(
            "selected forecast coverage bucket minutes",
            self.selected_bucket_minutes,
        )
        _ensure_distinct_days_fit_samples(
            "selected forecast coverage",
            self.selected_distinct_days,
            self.selected_sample_count,
        )
        _ensure_distinct_days_fit_samples(
            "primary forecast coverage",
            self.primary_distinct_days,
            self.primary_samples,
        )
        _ensure_distinct_days_fit_samples(
            "fallback forecast coverage",
            self.fallback_distinct_days,
            self.fallback_samples,
        )


@dataclass(frozen=True)
class ForecastWindowCoverageSummary:
    window_key: str
    profile_key: str
    days: int
    min_samples: int
    min_distinct_days: int
    total_samples: int
    eta_samples: int
    fresh_eta_samples: int
    traffic_samples: int
    latest_sampled_at: datetime | None
    buckets: tuple[ForecastCoverageBucket, ...]

    def __post_init__(self) -> None:
        _ensure_positive_count("forecast window coverage days", self.days)
        _ensure_positive_count("forecast window coverage min samples", self.min_samples)
        _ensure_positive_count(
            "forecast window coverage min distinct days",
            self.min_distinct_days,
        )
        if self.min_distinct_days > self.min_samples:
            raise ValueError("forecast window coverage min distinct days must not exceed min samples")
        _ensure_sample_counts(
            "forecast window coverage",
            total_samples=self.total_samples,
            eta_samples=self.eta_samples,
            fresh_eta_samples=self.fresh_eta_samples,
            traffic_samples=self.traffic_samples,
        )
        _ensure_optional_aware_datetime(
            "forecast window coverage latest_sampled_at",
            self.latest_sampled_at,
        )
        if not isinstance(self.buckets, tuple) or any(
            not isinstance(bucket, ForecastCoverageBucket) for bucket in self.buckets
        ):
            raise ValueError("forecast window coverage buckets need ForecastCoverageBucket tuple")
        for bucket in self.buckets:
            _ensure_count_not_above(
                "forecast window coverage selected samples",
                bucket.selected_sample_count,
                self.fresh_eta_samples,
                "fresh ETA samples",
            )
            _ensure_count_not_above(
                "forecast window coverage primary samples",
                bucket.primary_samples,
                self.fresh_eta_samples,
                "fresh ETA samples",
            )
            _ensure_count_not_above(
                "forecast window coverage fallback samples",
                bucket.fallback_samples,
                self.fresh_eta_samples,
                "fresh ETA samples",
            )

    @property
    def ready_buckets(self) -> int:
        return sum(1 for bucket in self.buckets if bucket.ready)

    @property
    def total_buckets(self) -> int:
        return len(self.buckets)

    @property
    def readiness_percent(self) -> int:
        return percent(self.ready_buckets, self.total_buckets)

    @property
    def eta_coverage_percent(self) -> int:
        return percent(self.eta_samples, self.total_samples)

    @property
    def fresh_eta_coverage_percent(self) -> int:
        return percent(self.fresh_eta_samples, self.total_samples)

    @property
    def traffic_coverage_percent(self) -> int:
        return percent(self.traffic_samples, self.total_samples)


def percent(numerator: int, denominator: int) -> int:
    _ensure_count("percent numerator", numerator)
    _ensure_count("percent denominator", denominator)
    if denominator == 0:
        if numerator != 0:
            raise ValueError("percent numerator must not exceed denominator")
        return 0
    if numerator > denominator:
        raise ValueError("percent numerator must not exceed denominator")
    return round(numerator * 100 / denominator)


def _normalize_count_key(key: str) -> str:
    normalized = " ".join(key.split())
    if not normalized:
        return "-"
    return normalized


def _ensure_count(label: str, value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} needs non-negative integer")


def _ensure_positive_count(label: str, value: int) -> None:
    _ensure_count(label, value)
    if value == 0:
        raise ValueError(f"{label} needs positive integer")


def _ensure_optional_count(label: str, value: int | None) -> None:
    if value is not None:
        _ensure_count(label, value)


def _ensure_sample_counts(
    label: str,
    *,
    total_samples: int,
    eta_samples: int,
    fresh_eta_samples: int,
    traffic_samples: int,
) -> None:
    _ensure_count_not_above(f"{label} ETA samples", eta_samples, total_samples, "total samples")
    _ensure_count_not_above(
        f"{label} fresh ETA samples",
        fresh_eta_samples,
        eta_samples,
        "ETA samples",
    )
    _ensure_count_not_above(f"{label} traffic samples", traffic_samples, total_samples, "total samples")


def _ensure_count_not_above(label: str, value: int, limit: int, limit_label: str) -> None:
    _ensure_count(label, value)
    _ensure_count(limit_label, limit)
    if value > limit:
        raise ValueError(f"{label} must not exceed {limit_label}")


def _ensure_collector_run_counts(
    label: str,
    *,
    total_runs: int,
    result_runs: int,
    eta_runs: int,
    traffic_ok_runs: int,
    skipped_runs: int,
) -> None:
    _ensure_count(f"{label} total runs", total_runs)
    for field_label, value in (
        ("result runs", result_runs),
        ("ETA runs", eta_runs),
        ("traffic ok runs", traffic_ok_runs),
        ("skipped runs", skipped_runs),
    ):
        _ensure_count_not_above(f"{label} {field_label}", value, total_runs, "total runs")


def _ensure_count_tuple(label: str, value: object) -> None:
    if not isinstance(value, tuple) or any(not isinstance(item, CountByKey) for item in value):
        raise ValueError(f"{label} needs CountByKey tuple")


def _ensure_plain_key(label: str, value: object) -> None:
    _ensure_single_line_text(label, value)
    text = str(value)
    if not text.isascii() or any(not (char.isalnum() or char in "_-") for char in text):
        raise ValueError(f"{label} needs plain text key")


def _ensure_text(label: str, value: object) -> None:
    if not isinstance(value, str):
        raise ValueError(f"{label} needs text")
    if not value.strip():
        raise ValueError(f"{label} is required")


def _ensure_single_line_text(label: str, value: object, *, allow_empty: bool = False) -> None:
    if not isinstance(value, str):
        raise ValueError(f"{label} needs text")
    if not allow_empty and not value.strip():
        raise ValueError(f"{label} is required")
    if value != value.strip() or any(char.isspace() for char in value):
        raise ValueError(f"{label} needs single-line text")


def _ensure_coordinate_pair(label: str, lat: float | None, lng: float | None) -> None:
    if lat is None and lng is None:
        return
    if lat is None or lng is None:
        raise ValueError(f"{label} need latitude and longitude")
    _ensure_coordinate(f"{label} latitude", lat, -90, 90)
    _ensure_coordinate(f"{label} longitude", lng, -180, 180)


def _ensure_coordinate(label: str, value: object, minimum: float, maximum: float) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, int | float)
        or not isfinite(value)
        or value < minimum
        or value > maximum
    ):
        raise ValueError(f"{label} must be a finite coordinate")


def _ensure_aware_datetime(label: str, value: object) -> None:
    if not isinstance(value, datetime):
        raise ValueError(f"{label} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware")


def _ensure_optional_aware_datetime(label: str, value: datetime | None) -> None:
    if value is not None:
        _ensure_aware_datetime(label, value)


def _ensure_distinct_days_fit_samples(label: str, distinct_days: int, samples: int) -> None:
    _ensure_count(f"{label} distinct days", distinct_days)
    _ensure_count(f"{label} samples", samples)
    if distinct_days > samples:
        raise ValueError(f"{label} distinct days must not exceed samples")
