from __future__ import annotations

from datetime import datetime
from typing import Any

from route74.sources.yandex.models import YandexVehicle
from route74.sources.yandex.parser.coordinates import coordinates
from route74.sources.yandex.parser.time_fields import age_seconds, arrival_minutes


def parse_vehicle(item: dict[str, Any], index: int, current_time: datetime) -> YandexVehicle:
    lat, lng = coordinates(item)
    return YandexVehicle(
        vehicle_id=vehicle_id(item, index),
        lat=lat,
        lng=lng,
        arrival_minutes=arrival_minutes(item, current_time),
        age_seconds=age_seconds(item, current_time),
        thread_id=thread_id(item),
    )


def vehicle_id(item: dict[str, Any], index: int) -> str:
    return str(
        item.get("id")
        or item.get("vehicleId")
        or item.get("uid")
        or _vehicle_metadata(item).get("id")
        or _transport_metadata(item).get("id")
        or f"yandex-{index}"
    )


def thread_id(item: dict[str, Any]) -> str:
    return str(_transport_metadata(item).get("threadId") or item.get("threadId") or "")


def _vehicle_metadata(item: dict[str, Any]) -> dict[str, Any]:
    properties = item.get("properties")
    if not isinstance(properties, dict):
        return {}
    metadata = properties.get("VehicleMetaData")
    return metadata if isinstance(metadata, dict) else {}


def _transport_metadata(item: dict[str, Any]) -> dict[str, Any]:
    transport = _vehicle_metadata(item).get("Transport")
    return transport if isinstance(transport, dict) else {}
