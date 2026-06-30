from __future__ import annotations

from route74.domain.profiles import MORNING
from route74.models import now_local
from route74.sources.yandex.config import YandexSourceConfig
from route74.sources.yandex.dump import YandexDumpEntry, YandexDumpResult
from route74.sources.yandex.smoke.assertions import assert_equal
from route74.sources.yandex.transport import YandexTransportSource


def run_dump_smoke() -> None:
    result = YandexDumpResult(
        url="https://yandex.ru/maps/",
        entries=(
            YandexDumpEntry(
                method="getVehiclePredictionInfo",
                status=200,
                url="https://yandex.ru/maps/api/masstransit/getVehiclePredictionInfo",
                payload={"data": {"stops": []}},
            ),
        ),
    )
    payload = result.to_jsonable()
    assert_equal(payload["entries"][0]["method"], "getVehiclePredictionInfo")
    assert_equal(payload["entries"][0]["payload"], {"data": {"stops": []}})


def run_live_probe() -> None:
    config = YandexSourceConfig(timeout_seconds=3.0, debug=True)
    forecast = YandexTransportSource(config).get_forecast(MORNING, now_local())
    source = forecast.source_method.value
    status = forecast.status.value
    eta = forecast.arrival_minutes[0] if forecast.arrival_minutes else None
    reason = f" reason={forecast.fallback_reason}" if forecast.fallback_reason else ""
    print(f"INFO | yandex live probe source={source} status={status} eta={eta}{reason}")
