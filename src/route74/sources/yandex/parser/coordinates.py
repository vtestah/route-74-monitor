from __future__ import annotations

from typing import Any

from route74.sources.yandex.parser.common import as_float, number_at

ROUTE74_LAT_RANGE = (54.6, 55.2)
ROUTE74_LNG_RANGE = (82.6, 83.6)


def coordinates(item: dict[str, Any]) -> tuple[float | None, float | None]:
    lat = number_at(item, ("lat", "latitude", "y"))
    lng = number_at(item, ("lng", "lon", "longitude", "x"))
    if lat is not None and lng is not None:
        return valid_coordinates(lat, lng)

    geometry = item.get("geometry")
    if isinstance(geometry, dict):
        coords = coordinates_from_geometry(geometry)
        if coords != (None, None):
            return coords

    coords = _coordinates_from_features(item)
    if coords != (None, None):
        return coords

    position = item.get("position")
    if isinstance(position, dict):
        return coordinates(position)
    if is_coord_pair(position):
        return coord_pair(position)
    return None, None


def coordinates_from_geometry(
    geometry: dict[str, Any],
) -> tuple[float | None, float | None]:
    return _last_coord_pair(geometry.get("coordinates"))


def coord_pair(coords: Any) -> tuple[float | None, float | None]:
    if not is_coord_pair(coords):
        return None, None
    first = as_float(coords[0])
    second = as_float(coords[1])
    if first is None or second is None:
        return None, None
    if abs(first) <= 90 and abs(second) > 90:
        return valid_coordinates(first, second)
    return valid_coordinates(second, first)


def is_coord_pair(value: Any) -> bool:
    return isinstance(value, list | tuple) and len(value) >= 2


def valid_coordinates(lat: float, lng: float) -> tuple[float | None, float | None]:
    lat_min, lat_max = ROUTE74_LAT_RANGE
    lng_min, lng_max = ROUTE74_LNG_RANGE
    if lat_min <= lat <= lat_max and lng_min <= lng <= lng_max:
        return lat, lng
    return None, None


def _last_coord_pair(value: Any) -> tuple[float | None, float | None]:
    direct_pair = coord_pair(value)
    if direct_pair != (None, None):
        return direct_pair
    if not isinstance(value, list | tuple):
        return None, None
    for candidate in reversed(value):
        nested_pair = _last_coord_pair(candidate)
        if nested_pair != (None, None):
            return nested_pair
    return None, None


def _coordinates_from_features(
    item: dict[str, Any],
) -> tuple[float | None, float | None]:
    features = item.get("features")
    if not isinstance(features, list):
        return None, None
    for feature in features:
        if not isinstance(feature, dict):
            continue
        geometry = feature.get("geometry")
        if isinstance(geometry, dict):
            coords = coordinates_from_geometry(geometry)
            if coords != (None, None):
                return coords
    return None, None
