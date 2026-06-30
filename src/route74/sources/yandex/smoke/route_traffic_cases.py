from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from io import StringIO
from typing import Any

from route74.domain.traffic import RouteTrafficSnapshot
from route74.sources.yandex.browser_rate_limit import _read_last_start
from route74.sources.yandex.route_traffic import (
    YandexRouteSummary,
    YandexRouteTrafficSource,
    traffic_from_route_summaries,
)
from route74.sources.yandex.smoke.assertions import assert_equal


def run_route_traffic_smoke(current_time: datetime) -> None:
    assert_equal(callable(YandexRouteTrafficSource(timeout_seconds=0.1)), True)
    assert_equal(_read_last_start(StringIO("nan")), 0.0)
    assert_equal(_read_last_start(StringIO("inf")), 0.0)
    assert_equal(_read_last_start(StringIO("200.0"), current_time=100.0), 0.0)
    assert_equal(_read_last_start(StringIO("99.0"), current_time=100.0), 99.0)

    traffic = traffic_from_route_summaries(
        "morning",
        current_time,
        (
            YandexRouteSummary("28 минПрибытие в 09:3814,7 кмПосмотреть подробнее", active=True),
            YandexRouteSummary("1 ч 5 мин Прибытие в 10:15 15,6 км"),
        ),
        "https://yandex.ru/maps/example",
    )
    assert_equal(traffic.provider, "yandex_route_dom")
    assert_equal(traffic.status, "ok")
    assert_equal(traffic.route_duration_seconds, 1680)
    assert_equal(traffic.route_duration_in_traffic_seconds, 1680)
    assert_equal(traffic.distance_meters, 14700)

    zero_duration = traffic_from_route_summaries("morning", current_time, (YandexRouteSummary("0 мин 14,7 км"),))
    assert_equal(zero_duration.status, "unavailable")
    assert_equal(zero_duration.raw["reason"], "route_duration_not_found")
    recovered_duration = traffic_from_route_summaries(
        "morning",
        current_time,
        (
            YandexRouteSummary("0 мин 14,7 км"),
            YandexRouteSummary("28 мин Прибытие в 09:38 14,7 км"),
        ),
    )
    assert_equal(recovered_duration.status, "ok")
    assert_equal(recovered_duration.route_duration_seconds, 1680)
    assert_equal(recovered_duration.distance_meters, 14700)
    active_placeholder = traffic_from_route_summaries(
        "morning",
        current_time,
        (
            YandexRouteSummary("0 мин 14,7 км", active=True),
            YandexRouteSummary("28 мин Прибытие в 09:38 14,7 км"),
        ),
    )
    assert_equal(active_placeholder.status, "ok")
    assert_equal(active_placeholder.route_duration_seconds, 1680)
    assert_equal(active_placeholder.distance_meters, 14700)
    absurd_distance = traffic_from_route_summaries("morning", current_time, (YandexRouteSummary("28 мин 999999 м"),))
    assert_equal(absurd_distance.status, "ok")
    assert_equal(absurd_distance.distance_meters, None)

    unavailable = traffic_from_route_summaries("morning", current_time, ())
    assert_equal(unavailable.status, "unavailable")
    _assert_invalid_traffic(lambda: RouteTrafficSnapshot("", "ok"), "provider")
    _assert_invalid_traffic(lambda: RouteTrafficSnapshot("   ", "ok"), "provider")
    _assert_invalid_traffic(lambda: RouteTrafficSnapshot("yandex-route", "ok"), "plain key")
    _assert_invalid_traffic(lambda: RouteTrafficSnapshot("яндекс", "ok"), "plain key")
    _assert_invalid_traffic(lambda: RouteTrafficSnapshot("fake", ""), "status")
    _assert_invalid_traffic(lambda: RouteTrafficSnapshot("fake", "   "), "status")
    _assert_invalid_traffic(lambda: RouteTrafficSnapshot("fake", "not-ok"), "plain key")
    _assert_invalid_traffic(lambda: RouteTrafficSnapshot("fake", "ошибка"), "plain key")
    _assert_invalid_traffic(lambda: RouteTrafficSnapshot("fake", "ok", raw=_invalid_raw()), "raw")
    _assert_invalid_traffic(lambda: RouteTrafficSnapshot("fake", "ok", jams_level=11), "jams level")
    _assert_invalid_traffic(lambda: RouteTrafficSnapshot("fake", "ok", delay_seconds=-1), "traffic delay")


def _invalid_raw() -> Any:
    return []


def _assert_invalid_traffic(factory: Callable[[], object], expected: str) -> None:
    try:
        factory()
    except ValueError as error:
        assert_equal(expected in str(error), True)
    else:
        raise AssertionError(f"expected traffic validation error containing {expected!r}")
