from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta, timezone, tzinfo

from route74.models import NOVOSIBIRSK_TZ, now_local, require_local_datetime


def main() -> None:
    local_time = datetime(2026, 6, 4, 7, 0, tzinfo=NOVOSIBIRSK_TZ)
    _assert_equal(require_local_datetime(local_time, name="smoke clock"), local_time)
    _assert_equal(now_local().tzinfo, NOVOSIBIRSK_TZ)
    _assert_rejects(lambda: require_local_datetime("now", name="smoke clock"), "needs datetime")
    _assert_rejects(
        lambda: require_local_datetime(local_time.replace(tzinfo=None), name="naive clock"),
        "timezone-aware datetime",
    )
    _assert_rejects(
        lambda: require_local_datetime(
            datetime(2026, 6, 4, 7, 0, tzinfo=timezone.utc),
            name="UTC clock",
        ),
        "got UTC",
    )
    _assert_rejects(
        lambda: require_local_datetime(
            datetime(2026, 6, 4, 7, 0, tzinfo=timezone(timedelta(hours=7))),
            name="fixed-offset clock",
        ),
        "got UTC+07:00",
    )
    _assert_rejects(
        lambda: require_local_datetime(
            datetime(2026, 6, 4, 7, 0, tzinfo=_SpoofNovosibirskTZ()),
            name="spoofed clock",
        ),
        "got UTC+03:00",
    )
    _assert_rejects(
        lambda: require_local_datetime(
            datetime(2026, 6, 4, 7, 0, tzinfo=_BrokenTZ()),
            name="broken clock",
        ),
        "timezone-aware datetime",
    )
    print("OK | models smoke passed")


class _SpoofNovosibirskTZ(tzinfo):
    key = "Asia/Novosibirsk"

    def utcoffset(self, _dt: datetime | None) -> timedelta:
        return timedelta(hours=3)

    def dst(self, _dt: datetime | None) -> timedelta:
        return timedelta(0)


class _BrokenTZ(tzinfo):
    def utcoffset(self, _dt: datetime | None) -> timedelta:
        raise RuntimeError("broken timezone")

    def dst(self, _dt: datetime | None) -> timedelta:
        return timedelta(0)


def _assert_rejects(factory: Callable[[], object], expected: str) -> None:
    try:
        factory()
    except ValueError as error:
        if expected not in str(error):
            raise AssertionError(f"expected {expected!r} in {str(error)!r}") from error
    else:
        raise AssertionError(f"expected validation error: {expected}")


def _assert_equal(actual: object, expected: object) -> None:
    if actual != expected:
        raise AssertionError(f"expected {expected!r}, got {actual!r}")


if __name__ == "__main__":
    main()
