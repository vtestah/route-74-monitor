from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import parse_qs, urlencode

from route74.domain.commute import CommuteProfile

YANDEX_LINE_ID = "65_74_minibus_novosibirskgortrans"
YANDEX_ROUTE_74_ID = (
    "796d617073626d313a2f2f7472616e7369742f6c696e653f69643d36355f37345f"
    "6d696e696275735f6e6f766f7369626972736b676f727472616e73266c6c3d38332e"
    "30383034333325324335342e383639303938266e616d653d373426723d313230303026"
    "747970653d6d696e69627573"
)
YANDEX_ROUTE_74_URL = f"https://yandex.ru/maps/65/novosibirsk/routes/minibus_74/{YANDEX_ROUTE_74_ID}/"
YANDEX_VEHICLES_URL = "https://yandex.ru/maps/api/masstransit/getVehiclesInfoWithRegion"
YANDEX_STOP_INFO_URL = "https://yandex.ru/maps/api/masstransit/getStopInfo"
YANDEX_VEHICLE_PREDICTION_URL = "https://yandex.ru/maps/api/masstransit/getVehiclePredictionInfo"
YANDEX_LINE_URL = "https://yandex.ru/maps/api/masstransit/getLine"
YANDEX_USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"
# stopInfo opens a stop page, prediction stops are matched inside
# getVehiclePredictionInfo, and expected threads validate direction.
STOP_ID_BY_PROFILE = {
    "morning": "stop__9982194",
    "evening": "stop__9982094",
}
TERMINAL_STOP_ID_BY_PROFILE = {
    "morning": "3174363647",
    "evening": "stop__9982203",
}
PREDICTION_STOP_IDS_BY_PROFILE = {
    "morning": ("stop__9982194",),
    "evening": ("stop__9982094",),
}
EXPECTED_THREAD_IDS_BY_PROFILE = {
    "morning": ("2161326768",),
    "evening": ("2161326764",),
}
MAX_RAW_ETA_MINUTES_BY_PROFILE = {
    "morning": 60,
    "evening": 60,
}
MAP_PARAMS_BY_PROFILE = {
    "morning": "ll=83.080433%2C54.869098&tab=stops&z=12",
    "evening": "ll=83.132629%2C54.840880&tab=stops&z=13",
}
DEFAULT_SPAN_BY_PROFILE = {
    "morning": "0.128746,0.027641",
    "evening": "0.128746,0.027641",
}


@dataclass(frozen=True)
class YandexRoutePoint:
    lat: float
    lng: float


ROUTE_TRAFFIC_POINTS_BY_PROFILE = {
    "morning": (
        YandexRoutePoint(lat=54.937428366, lng=83.099067176),
        YandexRoutePoint(lat=54.839683688, lng=83.088311805),
    ),
    "evening": (
        YandexRoutePoint(lat=54.853318735, lng=83.10261213),
        YandexRoutePoint(lat=54.930844926, lng=83.128206541),
    ),
}


def route_map_url(profile: CommuteProfile) -> str:
    params = map_params(profile)
    if thread_params := route_thread_params(profile):
        params = f"{params}&{thread_params}"
    return f"{YANDEX_ROUTE_74_URL}?{params}"


def route_traffic_url(profile: CommuteProfile) -> str:
    origin, destination = route_traffic_points(profile)
    center_lat = (origin.lat + destination.lat) / 2
    center_lng = (origin.lng + destination.lng) / 2
    params = urlencode(
        {
            "ll": f"{center_lng:.6f},{center_lat:.6f}",
            "mode": "routes",
            "rtext": f"{origin.lat:.9f},{origin.lng:.9f}~{destination.lat:.9f},{destination.lng:.9f}",
            "rtt": "auto",
            "ruri": "~",
            "z": "12",
        },
        safe="~,",
    )
    return f"https://yandex.ru/maps/65/novosibirsk/?{params}"


def route_traffic_points(
    profile: CommuteProfile,
) -> tuple[YandexRoutePoint, YandexRoutePoint]:
    return ROUTE_TRAFFIC_POINTS_BY_PROFILE.get(profile.key, ROUTE_TRAFFIC_POINTS_BY_PROFILE["morning"])


def stop_id(profile: CommuteProfile) -> str:
    return STOP_ID_BY_PROFILE.get(profile.key, STOP_ID_BY_PROFILE["morning"])


def terminal_stop_id(profile: CommuteProfile) -> str:
    return TERMINAL_STOP_ID_BY_PROFILE.get(profile.key, TERMINAL_STOP_ID_BY_PROFILE["morning"])


def prediction_stop_ids(profile: CommuteProfile) -> tuple[str, ...]:
    return PREDICTION_STOP_IDS_BY_PROFILE.get(profile.key, PREDICTION_STOP_IDS_BY_PROFILE["morning"])


def expected_thread_ids(profile: CommuteProfile) -> tuple[str, ...]:
    return EXPECTED_THREAD_IDS_BY_PROFILE.get(profile.key, ())


def max_raw_eta_minutes(profile: CommuteProfile | None) -> int:
    if profile is None:
        return 180
    return MAX_RAW_ETA_MINUTES_BY_PROFILE.get(profile.key, 180)


def route_thread_params(profile: CommuteProfile) -> str:
    expected_threads = expected_thread_ids(profile)
    target_stops = prediction_stop_ids(profile)
    if not expected_threads or not target_stops:
        return ""
    return urlencode(
        {
            "threadId": expected_threads[0],
            "openedBy[stopId]": target_stops[0],
        }
    )


def stop_map_url(profile: CommuteProfile) -> str:
    return f"https://yandex.ru/maps/65/novosibirsk/stops/{stop_id(profile)}/?{map_params(profile)}"


def map_params(profile: CommuteProfile) -> str:
    return MAP_PARAMS_BY_PROFILE.get(profile.key, MAP_PARAMS_BY_PROFILE["morning"])


def viewport_params(profile: CommuteProfile) -> dict[str, str]:
    parsed = parse_qs(map_params(profile))
    return {
        "ll": parsed.get("ll", ["83.099269,54.937230"])[0],
        "spn": parsed.get("spn", [DEFAULT_SPAN_BY_PROFILE.get(profile.key, "0.128746,0.027641")])[0],
    }
