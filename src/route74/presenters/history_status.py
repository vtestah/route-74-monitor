from __future__ import annotations

import re

from route74.diagnostics import sanitize_diagnostic_text
from route74.domain.yandex_history import YandexHistoryPrediction, YandexHistoryScope


INSUFFICIENT_HISTORY_PATTERN = re.compile(r"^insufficient_history:(\d+)/(\d+);days:(\d+)/(\d+)$")


def history_basis_text(history: YandexHistoryPrediction) -> str:
    if history.scope == YandexHistoryScope.REPORT_WINDOW:
        return "история Яндекса этого окна"
    return "история Яндекса по похожему времени"


def history_scope_text(history: YandexHistoryPrediction) -> str:
    if history.scope == YandexHistoryScope.REPORT_WINDOW:
        return f"отчётное окно {history.report_window_key}"
    return "похожее время профиля"


def unavailable_history_status_text(history: YandexHistoryPrediction) -> str:
    if history.available:
        return ""
    scope = history_scope_text(history)
    count_detail = _insufficient_history_detail(history.fallback_reason)
    if count_detail:
        state = "данных мало" if history.sample_count else "данных пока нет"
        return f"{state} · {count_detail} · {scope}"
    detail = history_unavailable_reason_text(history.fallback_reason)
    if history.sample_count:
        suffix = f" · {detail}" if detail else ""
        return f"данных мало · n={history.sample_count} · {scope}{suffix}"
    if detail:
        return f"недоступна · {scope} · {detail}"
    return f"данных пока нет · {scope}"


def history_unavailable_reason_text(reason: str) -> str:
    text = sanitize_diagnostic_text(reason, fallback="", limit=120)
    if not text or text == "history_unavailable":
        return ""
    if text == "history_disabled":
        return "история не подключена в этом режиме"
    if text == "local_history_unavailable":
        return "локальная история недоступна"
    if text.startswith("history_error:"):
        error_type = sanitize_diagnostic_text(text.split(":", 1)[1], fallback="ошибка", limit=60)
        return f"ошибка чтения истории: {error_type}"
    return text


def _insufficient_history_detail(reason: str) -> str:
    match = INSUFFICIENT_HISTORY_PATTERN.match(sanitize_diagnostic_text(reason, fallback="", limit=120))
    if match is None:
        return ""
    samples, min_samples, days, min_days = match.groups()
    return f"{samples}/{min_samples} замеров, {days}/{min_days} дней"
