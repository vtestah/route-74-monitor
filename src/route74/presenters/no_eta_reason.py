from __future__ import annotations

from typing import Protocol

from route74.diagnostics import sanitize_diagnostic_text


class CountView(Protocol):
    key: str
    count: int


REASON_TEXTS = {
    "unknown_no_eta": "не записана",
    "yandex_disabled": "Яндекс выключен",
    "yandex_no_eta": "Яндекс не дал ETA",
    "yandex_no_target": "Яндекс: нет нашей остановки",
    "yandex_empty": "Яндекс: машин нет",
    "yandex_stale": "Яндекс: данные старые",
    "yandex_blocked": "Яндекс заблокировал запрос",
    "yandex_timeout": "Яндекс не ответил",
    "yandex_unavailable": "Яндекс недоступен",
    "yandex_parse_error": "ответ Яндекса не разобран",
    "yandex_needs_signature": "Яндексу нужен browser-capture",
    "yandex_coordinates_only": "Яндекс дал только координаты",
    "yandex_schedule_only": "Яндекс дал только план",
    "yandex_frequency_only": "Яндекс дал только интервал",
    "yandex_untrusted_eta": "ETA Яндекса не прошёл доверие",
    "history_disabled": "история не подключена",
    "history_unavailable": "история недоступна",
    "history_local_unavailable": "локальная история недоступна",
    "history_error": "ошибка чтения истории",
    "history_insufficient": "история: мало данных",
}


def no_eta_reason_text(reason: str) -> str:
    key = sanitize_diagnostic_text(reason, fallback="unknown_no_eta", limit=120)
    parts = tuple(part for part in key.split("+") if part)
    if parts:
        return "; ".join(_reason_part_text(part) for part in parts)
    return _reason_part_text(key)


def no_eta_top_reason_text(reasons: object) -> str:
    top = _top_reason(reasons)
    if top is None:
        return ""
    label = no_eta_reason_text(top.key)
    if top.count > 0:
        return f"{label} ({top.count})"
    return label


def _top_reason(reasons: object) -> CountView | None:
    if not isinstance(reasons, tuple) or not reasons:
        return None
    top = reasons[0]
    key = getattr(top, "key", "")
    count = getattr(top, "count", 0)
    if not isinstance(key, str) or not isinstance(count, int) or isinstance(count, bool):
        return None
    if count < 0:
        return None
    return top


def _reason_part_text(reason: str) -> str:
    key = sanitize_diagnostic_text(reason, fallback="unknown_no_eta", limit=80)
    return REASON_TEXTS.get(key, key.replace("_", " "))
