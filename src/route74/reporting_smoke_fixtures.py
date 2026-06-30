from __future__ import annotations

from route74.domain.eta import EtaConfidence
from route74.sources.yandex.models import YandexLiveForecast, YandexSourceMethod, YandexSourceStatus, YandexVehicle
from route74.storage import RouteTrafficSnapshot


class FakeYandexSource:
    def get_forecast(self, *_args: object) -> YandexLiveForecast:
        return YandexLiveForecast(
            enabled=True,
            available=True,
            source_method=YandexSourceMethod.VEHICLE_PREDICTION,
            status=YandexSourceStatus.OK,
            arrival_minutes=(8,),
            vehicles=(
                YandexVehicle(
                    vehicle_id="vehicle-1",
                    thread_id="2161326764",
                    lat=54.84,
                    lng=83.11,
                    arrival_minutes=8,
                    age_seconds=12,
                ),
            ),
            vehicle_count=1,
            confidence=EtaConfidence.HIGH,
        )


def fake_traffic_source(*_args: object) -> RouteTrafficSnapshot:
    return RouteTrafficSnapshot(
        provider="fake",
        status="ok",
        jams_level=4,
        route_duration_seconds=1200,
        route_duration_in_traffic_seconds=1500,
        delay_seconds=300,
        distance_meters=8200,
        raw={"source": "smoke"},
    )
