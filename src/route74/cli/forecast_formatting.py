from __future__ import annotations

from pathlib import Path

from route74.storage import ForecastReadinessSummary, ForecastWindowCoverageSummary


def format_forecast_readiness_summary(summary: ForecastReadinessSummary, db_path: Path) -> str:
    latest = summary.latest_sampled_at.strftime("%Y-%m-%d %H:%M") if summary.latest_sampled_at else "-"
    readiness = "ready" if summary.ready else "not_ready"
    age = "none" if summary.max_age_seconds is None else f"{summary.max_age_seconds}s"
    profile = _diagnostic_text(summary.profile_key)
    scope = f" window={_diagnostic_text(summary.report_window_key)}" if summary.report_window_key is not None else ""
    return "\n".join(
        [
            (
                f"forecast readiness profile={profile}{scope} at={summary.current_time:%H:%M} "
                f"days={summary.days} db={_diagnostic_text(db_path)}"
            ),
            (
                f"status={readiness} selected_bucket=±{summary.selected_bucket_minutes}m "
                f"samples={summary.selected_sample_count}/{summary.min_samples} "
                f"days={summary.selected_distinct_days}/{summary.min_distinct_days}"
            ),
            (
                f"total={summary.total_samples} eta={summary.eta_samples}"
                f"({summary.eta_coverage_percent}%) fresh_eta={summary.fresh_eta_samples}"
                f"({summary.fresh_eta_coverage_percent}%) traffic={summary.traffic_samples}"
                f"({summary.traffic_coverage_percent}%) max_age={age} latest={latest}"
            ),
            (
                f"buckets=primary:{summary.primary_samples}/{summary.primary_distinct_days}d "
                f"fallback:{summary.fallback_samples}/{summary.fallback_distinct_days}d"
            ),
        ]
    )


def format_forecast_window_coverage_summary(summary: ForecastWindowCoverageSummary, db_path: Path) -> str:
    latest = summary.latest_sampled_at.strftime("%Y-%m-%d %H:%M") if summary.latest_sampled_at else "-"
    bucket_text = " ".join(
        (
            f"{_diagnostic_text(bucket.label)}:{'ok' if bucket.ready else 'no'}"
            f"({bucket.selected_sample_count}/{summary.min_samples},d={bucket.selected_distinct_days}/{summary.min_distinct_days})"
        )
        for bucket in summary.buckets
    )
    return "\n".join(
        [
            (
                f"forecast coverage window={_diagnostic_text(summary.window_key)} "
                f"profile={_diagnostic_text(summary.profile_key)} days={summary.days} db={_diagnostic_text(db_path)}"
            ),
            (
                f"ready_buckets={summary.ready_buckets}/{summary.total_buckets}"
                f"({summary.readiness_percent}%) samples_min={summary.min_samples} days_min={summary.min_distinct_days}"
            ),
            (
                f"total={summary.total_samples} eta={summary.eta_samples}"
                f"({summary.eta_coverage_percent}%) fresh_eta={summary.fresh_eta_samples}"
                f"({summary.fresh_eta_coverage_percent}%) traffic={summary.traffic_samples}"
                f"({summary.traffic_coverage_percent}%) latest={latest}"
            ),
            f"buckets={bucket_text or '-'}",
        ]
    )


def _diagnostic_text(value: object, *, fallback: str = "-", limit: int = 120) -> str:
    if value is None:
        return fallback
    printable = "".join(character if character.isprintable() else " " for character in str(value))
    normalized = " ".join(printable.split())
    return normalized[:limit] if normalized else fallback
