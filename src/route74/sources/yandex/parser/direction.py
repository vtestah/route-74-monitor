from __future__ import annotations

from route74.domain.commute import CommuteProfile
from route74.sources.yandex.constants import expected_thread_ids
from route74.sources.yandex.models import YandexVehicle


def filter_expected_thread(
    vehicles: tuple[YandexVehicle, ...],
    profile: CommuteProfile | None,
) -> tuple[tuple[YandexVehicle, ...], str]:
    if profile is None:
        return vehicles, ""
    expected = set(expected_thread_ids(profile))
    if not expected:
        return vehicles, ""
    known_thread_vehicles = tuple(vehicle for vehicle in vehicles if vehicle.thread_id)
    if not known_thread_vehicles:
        return (), "direction_thread_missing"
    filtered = tuple(vehicle for vehicle in known_thread_vehicles if vehicle.thread_id in expected)
    if not filtered:
        return (), "direction_thread_not_found"
    return filtered, ""
