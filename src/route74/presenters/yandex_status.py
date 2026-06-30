from __future__ import annotations

from route74.diagnostics import sanitize_diagnostic_text
from route74.sources.yandex.models import YandexSourceMethod, YandexSourceStatus


def yandex_method_text(method: YandexSourceMethod) -> str:
    return {
        YandexSourceMethod.NONE: "нет метода",
        YandexSourceMethod.HTTP: "HTTP",
        YandexSourceMethod.BROWSER: "браузер",
        YandexSourceMethod.VEHICLE_PREDICTION: "машина на карте",
        YandexSourceMethod.STOP_INFO: "остановка",
    }[method]


def yandex_status_text(status: YandexSourceStatus) -> str:
    return {
        YandexSourceStatus.DISABLED: "выключен",
        YandexSourceStatus.OK: "данные есть",
        YandexSourceStatus.COORDINATES_ONLY: "дал только координаты",
        YandexSourceStatus.SCHEDULE_ONLY: "на остановке дал только план",
        YandexSourceStatus.FREQUENCY_ONLY: "на остановке дал только интервал",
        YandexSourceStatus.NO_TARGET: "нет нашей остановки в прогнозе",
        YandexSourceStatus.EMPTY: "машин не отдал",
        YandexSourceStatus.STALE: "данные устарели",
        YandexSourceStatus.NEEDS_SIGNATURE: "нужен browser-capture",
        YandexSourceStatus.BLOCKED: "заблокировал запрос",
        YandexSourceStatus.TIMEOUT: "не ответил вовремя",
        YandexSourceStatus.UNAVAILABLE: "недоступен",
        YandexSourceStatus.PARSE_ERROR: "непонятный ответ",
    }[status]


def yandex_issue_text(status: YandexSourceStatus, reason: str, *, fallback: str) -> str:
    details = _known_reason_texts(status, reason)
    if details:
        return "; ".join(details)
    if status == YandexSourceStatus.SCHEDULE_ONLY or status == YandexSourceStatus.FREQUENCY_ONLY:
        return sanitize_diagnostic_text(reason, fallback=fallback)
    return sanitize_diagnostic_text(reason, fallback=fallback)


def yandex_status_detail(status: YandexSourceStatus, reason: str) -> str:
    details = yandex_issue_text(status, reason, fallback="")
    if details == yandex_status_text(status):
        return ""
    return f" ({details})" if details else ""


def yandex_status_summary(status: YandexSourceStatus, reason: str) -> str:
    return f"{yandex_status_text(status)}{yandex_status_detail(status, reason)}"


def _known_reason_texts(status: YandexSourceStatus, reason: str) -> tuple[str, ...]:
    text = sanitize_diagnostic_text(reason, fallback="", limit=240)
    matches = []
    if _has_any(text, "needs_signature", "bad_request_maybe_s"):
        matches.append("нужен browser-capture")
    if _has_any(text, "blocked", "forbidden", "captcha"):
        matches.append("Яндекс заблокировал запрос")
    if _has_any(text, "timeout"):
        matches.append("Яндекс не ответил вовремя")
    if _has_any(
        text,
        "vehicle_prediction_thread_fallback",
        "direction_thread_not_found",
        "direction_thread_missing",
        "no_target",
    ):
        matches.append("нужное направление не найдено")
    has_invalid_eta = _has_any(text, "invalid_eta_filtered")
    if has_invalid_eta:
        matches.append("ETA некорректный")
    if _has_any(text, "raw_eta_over_limit"):
        matches.append("ETA за пределом доверия")
    if _has_any(text, "route_vehicle_eta_ignored", "legacy_route_vehicle_eta"):
        matches.append("route-level ETA не доверяем")
    if _has_any(text, "cache_arrivals_expired"):
        matches.append("ETA из кэша устарел")
    if not has_invalid_eta and _has_any(text, "available_without_eta", "browser_no_prediction_response"):
        matches.append("ETA сейчас не отдал")
    if _has_any(text, "vehicles_not_found") or status == YandexSourceStatus.EMPTY:
        matches.append("машин по нужному направлению нет")
    if _has_any(text, "parse_error"):
        matches.append("ответ Яндекса не разобран")
    if _has_any(text, "source_exception"):
        matches.append("ошибка источника")
    return _unique_texts(tuple(matches))


def _has_any(text: str, *needles: str) -> bool:
    return any(needle in text for needle in needles)


def _unique_texts(values: tuple[str, ...]) -> tuple[str, ...]:
    unique = []
    for value in values:
        if value not in unique:
            unique.append(value)
    return tuple(unique)
