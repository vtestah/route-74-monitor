"""Unit tests for dashboard series aggregation.

Covers the small-sample suppression and single-rounding behaviour of
``route74.dashboard.data._series_row`` and its numeric helpers.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from route74.dashboard.data import (
    MIN_SERIES_SAMPLES_FOR_STATS,
    _avg,
    _avg_minutes_from_seconds,
    _p80,
    _series_row,
)

Row = Callable[..., dict[str, object]]


@pytest.mark.parametrize(
    ("eta_count", "expect_stats"),
    [
        (1, False),
        (2, False),
        (MIN_SERIES_SAMPLES_FOR_STATS, True),
        (5, True),
    ],
)
def test_series_eta_stats_respect_min_sample_threshold(report_row: Row, eta_count: int, expect_stats: bool) -> None:
    rows = [report_row(arrivals=f"[{minutes}]") for minutes in range(5, 5 + eta_count)]

    series = _series_row("2026-06-04", rows)

    assert series["eta_samples"] == eta_count
    if expect_stats:
        assert series["p80_eta_minutes"] is not None
        assert series["avg_eta_minutes"] is not None
    else:
        assert series["p80_eta_minutes"] is None
        assert series["avg_eta_minutes"] is None


def test_series_eta_stats_values_with_enough_samples(report_row: Row) -> None:
    rows = [report_row(arrivals=f"[{minutes}]") for minutes in (5, 7, 12)]

    series = _series_row("2026-06-04", rows)

    # avg = round((5 + 7 + 12) / 3) = 8; p80 index = ceil(3 * 0.8) - 1 = 2 -> 12.
    assert series["avg_eta_minutes"] == 8
    assert series["p80_eta_minutes"] == 12


def test_series_ignores_malformed_traffic_values(report_row: Row) -> None:
    rows = [
        report_row(arrivals="[5]", delay="bad-delay", jams="bad-jams"),
        report_row(arrivals="[7]", delay=300, jams=4),
        report_row(arrivals="[]", delay=-60, jams=-1),
    ]

    series = _series_row("2026-06-04", rows)

    assert series["samples"] == 3
    assert series["eta_samples"] == 2
    # Only the single valid delay (300s) survives -> 5 minutes.
    assert series["avg_traffic_delay_minutes"] == 5
    assert series["max_traffic_delay_minutes"] == 5
    assert series["avg_jams_level"] == 4


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        ([], None),
        ([300], 5),
        # Single rounding: mean(40, 40, 100) = 60s -> 1 min.
        # Per-sample rounding would have rounded each value first.
        ([40, 40, 100], 1),
        ([90, 90], 2),
    ],
)
def test_avg_minutes_from_seconds_rounds_once(seconds: list[int], expected: int | None) -> None:
    assert _avg_minutes_from_seconds(seconds) == expected


@pytest.mark.parametrize(
    ("values", "expected"),
    [
        ([], None),
        ([10], 10),
        ([5, 7, 12], 8),
    ],
)
def test_avg_handles_empty_and_rounds(values: list[int], expected: int | None) -> None:
    assert _avg(values) == expected


@pytest.mark.parametrize(
    ("values", "expected"),
    [
        ([], None),
        ([4], 4),
        ([5, 7, 12], 12),
        # index = ceil(5 * 0.8) - 1 = 3 -> ordered[3] = 4.
        ([1, 2, 3, 4, 5], 4),
    ],
)
def test_p80_picks_expected_percentile(values: list[int], expected: int | None) -> None:
    assert _p80(values) == expected
