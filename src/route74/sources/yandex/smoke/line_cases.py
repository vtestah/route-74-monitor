from __future__ import annotations

from route74.sources.yandex.line import parse_line_payload
from route74.sources.yandex.smoke.assertions import assert_equal


def run_line_smoke() -> None:
    topology = parse_line_payload(
        {
            "data": {
                "features": [
                    _line_thread_payload(
                        thread_id="2161326764",
                        start=("2167003192", "Цветной проезд"),
                        end=("stop__9982203", "Улица Твардовского"),
                        stops=[
                            ("2167003192", "Цветной проезд", 83.088001901, 54.839601216),
                            ("stop__9982194", "Медицинский центр", 83.099067176, 54.937428366),
                            ("stop__9982094", "Вычислительный центр", 83.10261213, 54.853318735),
                            ("stop__9982203", "Улица Твардовского", 83.128003, 54.930634),
                            ("invalid-coordinate-stop", "Ошибочная геометрия", 183.1, 54.9),
                        ],
                    ),
                    _line_thread_payload(
                        thread_id="2161326768",
                        start=("2167156332", "Улица Твардовского"),
                        end=("3174363647", "Цветной проезд"),
                        stops=[
                            ("2167156332", "Улица Твардовского", 83.128206541, 54.930844926),
                            ("stop__9982194", "Медицинский центр", 83.099067176, 54.937428366),
                            ("3174363647", "Цветной проезд", 83.088311805, 54.839683688),
                        ],
                    ),
                ],
                "activeThread": _line_thread_payload(
                    thread_id="2161326764",
                    start=("2167003192", "Цветной проезд"),
                    end=("stop__9982203", "Улица Твардовского"),
                    stops=[],
                ),
            }
        }
    )
    assert_equal(topology.line_id, "65_74_minibus_novosibirskgortrans")
    assert_equal(topology.active_thread_id, "2161326764")
    assert_equal(len(topology.threads), 2)
    assert_equal(topology.threads[0].start_stop_name, "Цветной проезд")
    assert_equal(topology.threads[0].segment_point_count, 8)
    assert_equal(topology.threads[0].points[0].lng, 83.088001901)
    assert_equal(topology.threads[0].stops[2].lat, 54.853318735)
    assert_equal(topology.threads[0].stops[4].lat, None)
    assert_equal(topology.threads[0].stops[4].lng, None)
    assert_equal(topology.thread_for_stop("stop__9982194").thread_id, "2161326764")
    assert_equal(topology.thread_for_stop("stop__9982194", preferred_thread_ids=("2161326768",)).thread_id, "2161326768")
    preferred_thread, selected_stop_id = topology.thread_for_stops(
        ("missing", "stop__9982194"),
        preferred_thread_ids=("2161326768",),
    )
    assert_equal(preferred_thread.thread_id, "2161326768")
    assert_equal(selected_stop_id, "stop__9982194")


def _line_thread_payload(
    *,
    thread_id: str,
    start: tuple[str, str],
    end: tuple[str, str],
    stops: list[tuple[str, str, float, float]],
) -> dict[str, object]:
    features: list[dict[str, object]] = []
    for stop_id, name, lng, lat in stops:
        features.append({"id": stop_id, "name": name, "coordinates": [lng, lat]})
        features.append({"points": [[lng, lat], [lng + 0.001, lat + 0.001]]})
    return {
        "features": features,
        "properties": {
            "ThreadMetaData": {
                "id": thread_id,
                "lineId": "65_74_minibus_novosibirskgortrans",
                "name": "74",
                "type": "minibus",
                "EssentialStops": [
                    {"id": start[0], "name": start[1]},
                    {"id": end[0], "name": end[1]},
                ],
            }
        },
    }
