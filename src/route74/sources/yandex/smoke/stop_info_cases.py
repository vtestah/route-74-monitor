from __future__ import annotations

from datetime import datetime, timedelta

from route74.domain.profiles import EVENING
from route74.models import NOVOSIBIRSK_TZ
from route74.sources.yandex.models import YandexSourceMethod, YandexSourceStatus
from route74.sources.yandex.smoke.assertions import assert_equal
from route74.sources.yandex.stop_info import parse_stop_info_payload


def run_stop_info_smoke() -> None:
    current_time = datetime(2026, 6, 4, 20, 10, tzinfo=NOVOSIBIRSK_TZ)
    forecast = parse_stop_info_payload(
        _stop_info_payload(
            events=[
                {
                    "Estimated": {
                        "value": str(round((current_time + timedelta(minutes=8)).timestamp())),
                        "tzOffset": 25200,
                        "text": "20:18",
                    },
                    "vehicleId": "novosib_obl1|route74",
                }
            ],
        ),
        profile=EVENING,
        current_time=current_time,
    )
    assert_equal(forecast.available, True)
    assert_equal(forecast.source_method, YandexSourceMethod.STOP_INFO)
    assert_equal(forecast.arrival_minutes, (8,))
    assert_equal(forecast.newest_age_seconds, 0)
    assert_equal(forecast.vehicles[0].vehicle_id, "novosib_obl1|route74")

    frequency = parse_stop_info_payload(
        _stop_info_payload(
            events=[],
            frequencies=[
                {
                    "text": "30\u00a0мин",
                    "value": 1800,
                    "begin": {"text": "06:51"},
                    "end": {"text": "21:11"},
                }
            ],
        ),
        profile=EVENING,
        current_time=current_time,
    )
    assert_equal(frequency.available, False)
    assert_equal(frequency.status, YandexSourceStatus.FREQUENCY_ONLY)
    assert_equal("интервал 30 мин" in frequency.fallback_reason, True)

    non_finite_value = parse_stop_info_payload(
        _stop_info_payload(
            events=[
                {
                    "Estimated": {"value": "nan", "text": "20:18"},
                    "vehicleId": "stop-info-nan",
                }
            ],
        ),
        profile=EVENING,
        current_time=current_time,
    )
    assert_equal(non_finite_value.available, True)
    assert_equal(non_finite_value.arrival_minutes, (8,))
    assert_equal(non_finite_value.vehicles[0].vehicle_id, "stop-info-nan")

    stale_numeric_value = parse_stop_info_payload(
        _stop_info_payload(
            events=[
                {
                    "Estimated": {"value": "1", "text": "20:18"},
                    "vehicleId": "stop-info-stale-value",
                }
            ],
        ),
        profile=EVENING,
        current_time=current_time,
    )
    assert_equal(stale_numeric_value.available, True)
    assert_equal(stale_numeric_value.arrival_minutes, (8,))
    assert_equal(stale_numeric_value.vehicles[0].vehicle_id, "stop-info-stale-value")

    non_finite_frequency = parse_stop_info_payload(
        _stop_info_payload(events=[], frequencies=[{"value": "inf"}]),
        profile=EVENING,
        current_time=current_time,
    )
    assert_equal(non_finite_frequency.available, False)
    assert_equal(non_finite_frequency.status, YandexSourceStatus.FREQUENCY_ONLY)
    assert_equal(non_finite_frequency.fallback_reason, "есть только интервальное расписание")


def run_stop_info_midnight_text_smoke() -> None:
    forecast = parse_stop_info_payload(
        _stop_info_payload(
            events=[
                {
                    "Estimated": {"text": "00:05"},
                    "vehicleId": "stop-info-midnight",
                }
            ],
        ),
        profile=EVENING,
        current_time=datetime(2026, 6, 4, 22, 58, tzinfo=NOVOSIBIRSK_TZ),
    )
    assert_equal(forecast.available, True)
    assert_equal(forecast.source_method, YandexSourceMethod.STOP_INFO)
    assert_equal(forecast.arrival_minutes, (67,))
    assert_equal(forecast.vehicles[0].vehicle_id, "stop-info-midnight")


def _stop_info_payload(
    *,
    events: list[dict[str, object]],
    frequencies: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    return {
        "data": {
            "transports": [
                {
                    "lineId": "65_74_minibus_novosibirskgortrans",
                    "name": "74",
                    "type": "minibus",
                    "threads": [
                        {
                            "EssentialStops": [
                                {"name": "Цветной проезд", "info": {"firstStop": True}},
                                {
                                    "name": "Улица Твардовского",
                                    "info": {"lastStop": True},
                                },
                            ],
                            "BriefSchedule": {
                                "Events": events,
                                "Frequencies": frequencies or [],
                            },
                        }
                    ],
                }
            ]
        }
    }
