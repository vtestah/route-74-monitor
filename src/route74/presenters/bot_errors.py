from __future__ import annotations

from route74.diagnostics import sanitize_diagnostic_text


ERROR_CATEGORY_LABELS = {
    "followup_send_error": "quick-start подсказка не ушла",
    "decision_record_error": "runtime-факт не записался",
    "watch_start_error": "watch не стартовал",
    "send_error": "основной ответ не ушёл",
    "reply_error": "сбор ответа упал",
    "unknown_error": "ошибка без детали",
}


def bot_error_category_text(key: str) -> str:
    cleaned = sanitize_diagnostic_text(key, fallback="unknown_error", limit=80)
    return ERROR_CATEGORY_LABELS.get(cleaned, cleaned)


def bot_error_top_category_text(items: object) -> str:
    if not isinstance(items, tuple) or not items:
        return ""
    top = items[0]
    key = str(getattr(top, "key", "") or "")
    if not key:
        return ""
    label = bot_error_category_text(key)
    count = getattr(top, "count", 0)
    if isinstance(count, int) and not isinstance(count, bool) and count > 0:
        return f"{label}:{count}"
    return label
