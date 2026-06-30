from __future__ import annotations

from dataclasses import dataclass

from route74.domain.yandex_history import DEFAULT_HISTORY_PERCENTILE


DEFAULT_FORECAST_BACKTEST_PERCENTILES = (70, DEFAULT_HISTORY_PERCENTILE, 90)
FORECAST_BACKTEST_PERCENTILES_ERROR = "expected percentiles from 1 to 100"


@dataclass(frozen=True)
class ForecastBacktestResult:
    percentile: int
    evaluated_cases: int
    skipped_cases: int
    miss_cases: int
    bucket_accurate_cases: int
    miss_minutes: int
    extra_wait_minutes: int
    mean_absolute_error: float

    @property
    def miss_rate_percent(self) -> int:
        if self.evaluated_cases == 0:
            return 0
        return round(self.miss_cases * 100 / self.evaluated_cases)

    @property
    def bucket_accuracy_percent(self) -> int:
        if self.evaluated_cases == 0:
            return 0
        return round(self.bucket_accurate_cases * 100 / self.evaluated_cases)


@dataclass(frozen=True)
class ForecastBacktestSummary:
    profile_key: str
    report_window_key: str
    history_days: int
    bucket_minutes: int
    min_samples: int
    min_distinct_days: int
    percentiles: tuple[int, ...]
    target_cases: int
    results: tuple[ForecastBacktestResult, ...]

    @property
    def selected_result(self) -> ForecastBacktestResult | None:
        return selected_forecast_backtest_result(self)

    @property
    def best_result(self) -> ForecastBacktestResult | None:
        return best_forecast_backtest_result(self)


def selected_forecast_backtest_result(
    summary: ForecastBacktestSummary,
    *,
    percentile: int = DEFAULT_HISTORY_PERCENTILE,
) -> ForecastBacktestResult | None:
    if not summary.results:
        return None
    reference = next((result for result in summary.results if result.percentile == percentile), None)
    if reference is None:
        return best_forecast_backtest_result(summary)
    best = best_forecast_backtest_result(summary)
    if best is None or best.percentile == reference.percentile:
        return reference
    if _backtest_candidate_improves(best, reference):
        return best
    return reference


def best_forecast_backtest_result(summary: ForecastBacktestSummary) -> ForecastBacktestResult | None:
    if not summary.results:
        return None
    return min(summary.results, key=_backtest_score)


def validate_forecast_backtest_percentiles(percentiles: tuple[int, ...]) -> tuple[int, ...]:
    if not percentiles or any(
        isinstance(percentile, bool)
        or not isinstance(percentile, int)
        or percentile <= 0
        or percentile > 100
        for percentile in percentiles
    ):
        raise ValueError(FORECAST_BACKTEST_PERCENTILES_ERROR)
    if len(set(percentiles)) != len(percentiles):
        raise ValueError("expected unique percentiles")
    return percentiles


def _backtest_candidate_improves(candidate: ForecastBacktestResult, reference: ForecastBacktestResult) -> bool:
    return (
        candidate.miss_rate_percent,
        candidate.miss_minutes,
        candidate.mean_absolute_error,
    ) < (
        reference.miss_rate_percent,
        reference.miss_minutes,
        reference.mean_absolute_error,
    )


def _backtest_score(result: ForecastBacktestResult) -> tuple[float, ...]:
    return (
        float(result.miss_rate_percent),
        float(result.miss_minutes),
        float(result.mean_absolute_error),
        float(result.extra_wait_minutes),
        0.0 if result.percentile == DEFAULT_HISTORY_PERCENTILE else 1.0,
        float(result.percentile),
    )
