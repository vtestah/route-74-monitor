from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from route74.sources.yandex.parser.coordinates import coord_pair


@dataclass(frozen=True)
class YandexLineStop:
    stop_id: str
    name: str
    lat: float | None = None
    lng: float | None = None


@dataclass(frozen=True)
class YandexLinePoint:
    lat: float
    lng: float


@dataclass(frozen=True)
class YandexLineThread:
    thread_id: str
    line_id: str
    name: str
    vehicle_type: str
    start_stop_id: str
    start_stop_name: str
    end_stop_id: str
    end_stop_name: str
    stops: tuple[YandexLineStop, ...]
    points: tuple[YandexLinePoint, ...]

    def has_stop(self, stop_id: str) -> bool:
        return any(stop.stop_id == stop_id for stop in self.stops)

    @property
    def segment_point_count(self) -> int:
        return len(self.points)


@dataclass(frozen=True)
class YandexLineTopology:
    line_id: str
    active_thread_id: str
    threads: tuple[YandexLineThread, ...]

    def thread_for_stop(
        self,
        stop_id: str,
        *,
        preferred_thread_ids: tuple[str, ...] = (),
    ) -> YandexLineThread | None:
        result = self.thread_for_stops((stop_id,), preferred_thread_ids=preferred_thread_ids)
        return result[0] if result is not None else None

    def thread_for_stops(
        self,
        stop_ids: tuple[str, ...],
        *,
        preferred_thread_ids: tuple[str, ...] = (),
    ) -> tuple[YandexLineThread, str] | None:
        candidates = [(thread, stop_id) for stop_id in stop_ids for thread in self.threads if thread.has_stop(stop_id)]
        if not candidates:
            return None
        for thread_id in preferred_thread_ids:
            preferred = [(thread, stop_id) for thread, stop_id in candidates if thread.thread_id == thread_id]
            if preferred:
                return preferred[0]
        if self.active_thread_id:
            active = [(thread, stop_id) for thread, stop_id in candidates if thread.thread_id == self.active_thread_id]
            if active:
                return active[0]
        return candidates[0]


def parse_line_payload(payload: dict[str, Any]) -> YandexLineTopology:
    data = _dict_value(payload.get("data"))
    threads = tuple(
        thread
        for item in _list_value(data.get("features"))
        if (thread := _parse_thread(_dict_value(item))) is not None
    )
    active_thread_id = _thread_id(data.get("activeThread"))
    if not threads and isinstance(data.get("activeThread"), dict):
        active_thread = _parse_thread(_dict_value(data.get("activeThread")))
        threads = (active_thread,) if active_thread is not None else ()
    line_id = next((thread.line_id for thread in threads if thread.line_id), "")
    return YandexLineTopology(line_id=line_id, active_thread_id=active_thread_id, threads=threads)


def _parse_thread(item: dict[str, Any]) -> YandexLineThread | None:
    metadata = _metadata(item)
    thread_id = str(metadata.get("id") or "")
    if not thread_id:
        return None
    stops: list[YandexLineStop] = []
    points: list[YandexLinePoint] = []
    for feature in _list_value(item.get("features")):
        feature_dict = _dict_value(feature)
        if "points" in feature_dict:
            points.extend(_points_from_feature(feature_dict))
            continue
        stop = _stop_from_feature(feature_dict)
        if stop is not None:
            stops.append(stop)
    start_id, start_name, end_id, end_name = _essential_stop_names(metadata, tuple(stops))
    return YandexLineThread(
        thread_id=thread_id,
        line_id=str(metadata.get("lineId") or ""),
        name=str(metadata.get("name") or ""),
        vehicle_type=str(metadata.get("type") or ""),
        start_stop_id=start_id,
        start_stop_name=start_name,
        end_stop_id=end_id,
        end_stop_name=end_name,
        stops=tuple(stops),
        points=tuple(points),
    )


def _metadata(item: dict[str, Any]) -> dict[str, Any]:
    properties = _dict_value(item.get("properties"))
    return _dict_value(properties.get("ThreadMetaData"))


def _thread_id(item: Any) -> str:
    if not isinstance(item, dict):
        return ""
    return str(_metadata(item).get("id") or "")


def _stop_from_feature(feature: dict[str, Any]) -> YandexLineStop | None:
    stop_id = str(feature.get("id") or "")
    name = str(feature.get("name") or "")
    if not stop_id or not name:
        return None
    lat, lng = coord_pair(feature.get("coordinates"))
    return YandexLineStop(stop_id=stop_id, name=name, lat=lat, lng=lng)


def _points_from_feature(feature: dict[str, Any]) -> tuple[YandexLinePoint, ...]:
    result: list[YandexLinePoint] = []
    for point in _list_value(feature.get("points")):
        lat, lng = coord_pair(point)
        if lat is not None and lng is not None:
            result.append(YandexLinePoint(lat=lat, lng=lng))
    return tuple(result)


def _essential_stop_names(
    metadata: dict[str, Any],
    stops: tuple[YandexLineStop, ...],
) -> tuple[str, str, str, str]:
    essential = [_dict_value(item) for item in _list_value(metadata.get("EssentialStops"))]
    first = essential[0] if essential else {}
    last = essential[-1] if essential else {}
    start_id = str(first.get("id") or (stops[0].stop_id if stops else ""))
    start_name = str(first.get("name") or (stops[0].name if stops else ""))
    end_id = str(last.get("id") or (stops[-1].stop_id if stops else ""))
    end_name = str(last.get("name") or (stops[-1].name if stops else ""))
    return start_id, start_name, end_id, end_name


def _dict_value(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list_value(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []
