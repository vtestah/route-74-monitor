from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from route74.models import NOVOSIBIRSK_TZ
from route74.storage.models import (
    CollectorHeartbeat,
    CollectorRunSummary,
    CollectorWindowRunSummary,
    CountByKey,
)


def main() -> None:
    now = datetime(2026, 6, 4, 7, 0, tzinfo=NOVOSIBIRSK_TZ)
    CollectorHeartbeat(
        name="yandex-collect",
        updated_at=now,
        pid=1234,
        profile_filter="morning\nall",
        last_status="ok\npartial",
        last_message="collector ok",
    )
    CollectorRunSummary(
        name="yandex-collect",
        hours=24,
        total_runs=2,
        result_runs=2,
        eta_runs=1,
        traffic_ok_runs=1,
        skipped_runs=0,
        latest_started_at=now,
        statuses=(CountByKey("ok", 2),),
    )
    CollectorWindowRunSummary(
        window_key="weekday_morning_09_12",
        profile_key="morning",
        total_runs=1,
        result_runs=1,
        eta_runs=1,
        traffic_ok_runs=0,
        skipped_runs=0,
        latest_started_at=now,
        statuses=(CountByKey("ok", 1),),
    )

    _assert_rejects(
        lambda: CollectorHeartbeat(
            name="yandex-collect!",
            updated_at=now,
            pid=1234,
            profile_filter="all",
            last_status="ok",
            last_message="ok",
        ),
        "plain text key",
    )
    _assert_rejects(
        lambda: CollectorHeartbeat(
            name="yandex-collect",
            updated_at=datetime(2026, 6, 4, 7, 0),
            pid=1234,
            profile_filter="all",
            last_status="ok",
            last_message="ok",
        ),
        "timezone-aware",
    )
    _assert_rejects(
        lambda: CollectorHeartbeat(
            name="yandex-collect",
            updated_at=now,
            pid=0,
            profile_filter="all",
            last_status="ok",
            last_message="ok",
        ),
        "positive integer",
    )
    _assert_rejects(
        lambda: CollectorHeartbeat(
            name="yandex-collect",
            updated_at=now,
            pid=1234,
            profile_filter="all",
            last_status=" ",
            last_message="ok",
        ),
        "is required",
    )
    _assert_rejects(lambda: _run_summary(hours=0), "positive integer")
    _assert_rejects(lambda: _run_summary(result_runs=True), "non-negative integer")
    _assert_rejects(lambda: _run_summary(eta_runs=2), "must not exceed total runs")
    _assert_rejects(lambda: _run_summary(statuses=(object(),)), "CountByKey tuple")
    _assert_rejects(
        lambda: _run_summary(latest_started_at=datetime(2026, 6, 4, 7, 0)),
        "timezone-aware",
    )
    _assert_rejects(lambda: _window_summary(window_key="weekday.morning"), "plain text key")
    _assert_rejects(lambda: _window_summary(skipped_runs=2), "must not exceed total runs")
    print("OK | collector model smoke passed")


def _run_summary(**overrides: object) -> CollectorRunSummary:
    now = datetime(2026, 6, 4, 7, 0, tzinfo=NOVOSIBIRSK_TZ)
    values = {
        "name": "yandex-collect",
        "hours": 24,
        "total_runs": 1,
        "result_runs": 1,
        "eta_runs": 1,
        "traffic_ok_runs": 0,
        "skipped_runs": 0,
        "latest_started_at": now,
        "statuses": (),
    } | overrides
    return CollectorRunSummary(**values)  # type: ignore[arg-type]


def _window_summary(**overrides: object) -> CollectorWindowRunSummary:
    now = datetime(2026, 6, 4, 7, 0, tzinfo=NOVOSIBIRSK_TZ)
    values = {
        "window_key": "weekday_morning_09_12",
        "profile_key": "morning",
        "total_runs": 1,
        "result_runs": 1,
        "eta_runs": 1,
        "traffic_ok_runs": 0,
        "skipped_runs": 0,
        "latest_started_at": now,
        "statuses": (),
    } | overrides
    return CollectorWindowRunSummary(**values)  # type: ignore[arg-type]


def _assert_rejects(factory: Callable[[], object], expected: str) -> None:
    try:
        factory()
    except ValueError as error:
        if expected and expected not in str(error):
            raise AssertionError(f"expected {expected!r} in {str(error)!r}") from error
    else:
        raise AssertionError("expected validation error")


if __name__ == "__main__":
    main()
