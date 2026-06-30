from __future__ import annotations

from dataclasses import replace

from route74.domain.commute import CommuteProfile
from route74.sources.yandex.constants import max_raw_eta_minutes
from route74.sources.yandex.models import YandexVehicle


def filter_raw_eta_limit(
    vehicles: tuple[YandexVehicle, ...],
    profile: CommuteProfile | None,
) -> tuple[tuple[YandexVehicle, ...], str]:
    max_minutes = max_raw_eta_minutes(profile)
    filtered: list[YandexVehicle] = []
    rejected = 0
    for vehicle in vehicles:
        if vehicle.arrival_minutes is not None and vehicle.arrival_minutes > max_minutes:
            filtered.append(replace(vehicle, arrival_minutes=None))
            rejected += 1
        else:
            filtered.append(vehicle)
    reason = f"raw_eta_over_limit:{max_minutes}" if rejected else ""
    return tuple(filtered), reason
