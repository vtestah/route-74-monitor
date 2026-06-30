from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta, timezone, tzinfo

from route74.domain.commute import (
    CommuteSnapshot,
    DepartureDecision,
    DepartureSource,
    DepartureUrgency,
)
from route74.domain.eta import EtaConfidence, EtaConsensus, EtaSource
from route74.domain.profiles import MORNING
from route74.models import NOVOSIBIRSK_TZ, require_local_datetime


def main() -> None:
    assert_commute_payload_guardrails()
    print("OK | decision validation smoke passed")


def assert_commute_payload_guardrails() -> None:
    now = datetime(2026, 6, 4, 7, 0, tzinfo=NOVOSIBIRSK_TZ)
    consensus = EtaConsensus(EtaSource.YANDEX, 12, EtaConfidence.HIGH, 2, None, "")
    _assert_equal(require_local_datetime(now, name="smoke clock"), now)
    _assert_rejects(
        lambda: require_local_datetime(
            datetime(2026, 6, 4, 7, 0, tzinfo=timezone(timedelta(hours=7))),
            name="fixed-offset clock",
        ),
        "Asia/Novosibirsk",
    )
    _assert_rejects(
        lambda: require_local_datetime(
            datetime(2026, 6, 4, 7, 0, tzinfo=_SpoofNovosibirskTZ()),
            name="spoofed clock",
        ),
        "Asia/Novosibirsk",
    )
    CommuteSnapshot(profile=MORNING, current_time=now, walk_minutes=12, eta_consensus=consensus)
    _assert_rejects(lambda: CommuteSnapshot(profile=object(), current_time=now, walk_minutes=12), "profile")
    _assert_rejects(lambda: CommuteSnapshot(profile=MORNING, current_time="now", walk_minutes=12), "datetime")
    _assert_rejects(
        lambda: CommuteSnapshot(profile=MORNING, current_time=now.replace(tzinfo=None), walk_minutes=12), "timezone"
    )
    _assert_rejects(
        lambda: CommuteSnapshot(
            profile=MORNING,
            current_time=datetime(2026, 6, 4, 7, 0, tzinfo=timezone(timedelta(hours=7))),
            walk_minutes=12,
        ),
        "Asia/Novosibirsk",
    )
    _assert_rejects(
        lambda: CommuteSnapshot(
            profile=MORNING,
            current_time=datetime(2026, 6, 4, 7, 0, tzinfo=_SpoofNovosibirskTZ()),
            walk_minutes=12,
        ),
        "Asia/Novosibirsk",
    )
    _assert_rejects(lambda: CommuteSnapshot(profile=MORNING, current_time=now, walk_minutes=True), "walk")
    _assert_rejects(lambda: _decision(source="yandex"), "DepartureSource")
    _assert_rejects(lambda: _decision(urgency="relax"), "DepartureUrgency")
    fixed_now = datetime(2026, 6, 4, 7, 0, tzinfo=timezone(timedelta(hours=7)))
    _assert_rejects(
        lambda: _decision(
            current_time=fixed_now,
            arrival_at=fixed_now + timedelta(minutes=12),
            leave_at=fixed_now - timedelta(minutes=2),
        ),
        "Asia/Novosibirsk",
    )
    _assert_rejects(lambda: _decision(arrival_in_minutes=True), "arrival_in_minutes")
    _assert_rejects(lambda: _decision(arrival_in_minutes=-1), "arrival_in_minutes")
    _assert_rejects(lambda: _decision(leave_in_minutes=True), "leave_in_minutes")
    _assert_rejects(lambda: _decision(arrival_at=now.replace(tzinfo=None)), "timezone")
    _assert_rejects(
        lambda: _decision(arrival_at=(now + timedelta(minutes=12)).astimezone(timezone.utc)),
        "Asia/Novosibirsk",
    )
    _assert_rejects(
        lambda: _decision(leave_at=(now - timedelta(minutes=2)).astimezone(timezone.utc)),
        "Asia/Novosibirsk",
    )
    _assert_rejects(lambda: _decision(arrival_at=None), "arrival and leave")
    _assert_rejects(lambda: _decision(leave_at=None), "arrival and leave")
    _assert_rejects(lambda: _decision(arrival_at=now + timedelta(minutes=13)), "arrival time")
    _assert_rejects(
        lambda: _decision(
            arrival_in_minutes=13,
            arrival_at=now + timedelta(minutes=13),
        ),
        "arrival must match ETA consensus",
    )
    _assert_rejects(lambda: _decision(leave_at=now - timedelta(minutes=3)), "leave time")
    _assert_rejects(lambda: _decision(leave_in_minutes=13, leave_at=now + timedelta(minutes=13)), "after arrival")
    _assert_rejects(lambda: _decision(next_live_minutes=[20]), "next_live_minutes")
    _assert_rejects(lambda: _decision(next_live_minutes=(12,)), "after selected arrival")
    _assert_rejects(lambda: _decision(next_live_minutes=(20, 20)), "strictly increasing")
    _assert_rejects(lambda: _decision(next_live_minutes=(25, 20)), "strictly increasing")
    _assert_rejects(lambda: _decision(eta_consensus=object()), "EtaConsensus")
    _assert_rejects(
        lambda: _decision(
            source=DepartureSource.YANDEX,
            eta_consensus=EtaConsensus(EtaSource.YANDEX_HISTORY, 12, EtaConfidence.LOW, 6, None, ""),
        ),
        "source must match ETA consensus",
    )
    _assert_rejects(lambda: _decision(yandex_forecast=object()), "YandexLiveForecast")
    _assert_rejects(lambda: _decision(source=DepartureSource.NONE), "NO_DATA")
    _assert_rejects(lambda: _no_data_decision(arrival_in_minutes=12), "must not have")
    _assert_rejects(lambda: _no_data_decision(next_live_minutes=(20,)), "next live")
    _assert_rejects(lambda: _no_data_decision(eta_consensus=consensus), "ETA consensus")


def _decision(**overrides: object) -> DepartureDecision:
    now = datetime(2026, 6, 4, 7, 0, tzinfo=NOVOSIBIRSK_TZ)
    values = {
        "profile": MORNING,
        "current_time": now,
        "walk_minutes": 12,
        "source": DepartureSource.YANDEX,
        "urgency": DepartureUrgency.RELAX,
        "arrival_in_minutes": 12,
        "arrival_at": now + timedelta(minutes=12),
        "leave_in_minutes": -2,
        "leave_at": now - timedelta(minutes=2),
        "next_live_minutes": (),
        "eta_consensus": EtaConsensus(EtaSource.YANDEX, 12, EtaConfidence.HIGH, 2, None, ""),
    } | overrides
    return DepartureDecision(**values)  # type: ignore[arg-type]


def _no_data_decision(**overrides: object) -> DepartureDecision:
    values = {
        "source": DepartureSource.NONE,
        "urgency": DepartureUrgency.NO_DATA,
        "arrival_in_minutes": None,
        "arrival_at": None,
        "leave_in_minutes": None,
        "leave_at": None,
    } | overrides
    return _decision(**values)


def _assert_rejects(factory: Callable[[], object], expected: str) -> None:
    try:
        factory()
    except ValueError as error:
        if expected not in str(error):
            raise AssertionError(f"expected {expected!r} in {str(error)!r}") from error
    else:
        raise AssertionError(f"expected validation error: {expected}")


class _SpoofNovosibirskTZ(tzinfo):
    key = "Asia/Novosibirsk"

    def utcoffset(self, _dt: datetime | None) -> timedelta:
        return timedelta(hours=3)

    def dst(self, _dt: datetime | None) -> timedelta:
        return timedelta(0)


def _assert_equal(actual: object, expected: object) -> None:
    if actual != expected:
        raise AssertionError(f"expected {expected!r}, got {actual!r}")


if __name__ == "__main__":
    main()
