from __future__ import annotations

from route74.domain.commute import CommuteProfile
from route74.domain.profiles import EVENING, MORNING
from route74.domain.departure_policy import (
    EVENING_AUTO_END,
    EVENING_AUTO_START,
    GET_READY_THRESHOLD_MINUTES,
    GO_NOW_THRESHOLD_MINUTES,
    MORNING_AUTO_END,
    MORNING_AUTO_START,
)
from route74.domain.eta_policy import (
    HISTORY_TARGET_WAIT_MINUTES,
    HIGH_TARGET_WAIT_MINUTES,
    LOW_TARGET_WAIT_MINUTES,
    MEDIUM_TARGET_WAIT_MINUTES,
)
from route74.domain.watch_policy import (
    EARLY_ALERT_LEAVE_IN,
    FINAL_ALERT_LEAVE_IN,
    WATCH_DURATION_MINUTES,
    WATCH_POLL_INTERVAL_SECONDS,
)
from route74.domain.walk_buffer import is_valid_walk_minutes


def format_calculation_explanation(morning_walk_minutes: int, evening_walk_minutes: int) -> str:
    _validate_walk_minutes("morning", morning_walk_minutes)
    _validate_walk_minutes("evening", evening_walk_minutes)
    return "\n".join(
        [
            "🧮 Как я считаю",
            "",
            "1. Выбираю направление по времени Новосибирска:",
            f"🌅 утро {MORNING_AUTO_START:%H:%M}-{MORNING_AUTO_END:%H:%M};",
            f"🌆 вечер {EVENING_AUTO_START:%H:%M}-{EVENING_AUTO_END:%H:%M}.",
            "",
            "2. Беру только безопасные ориентиры:",
            "Порядок источников: свежий Яндекс у остановки -> история Яндекса -> нет точного ETA.",
            "🟡 Яндекс.Карты: когда виден 74-й именно у твоей остановки;",
            "🧭 координаты машины: только как осторожный сигнал, если времени нет;",
            "📈 история Яндекса: запасной ориентир, когда карта временно молчит.",
            "Если безопасного времени нет, я не угадываю и не подмешиваю расписание.",
            "",
            "3. Считаю выход так:",
            "время 74-го - твой запас до остановки - спокойное ожидание = когда выходить.",
            f"Высокое доверие: цель ждать {HIGH_TARGET_WAIT_MINUTES} мин.",
            f"Среднее доверие: цель ждать {MEDIUM_TARGET_WAIT_MINUTES} мин.",
            f"Низкое доверие: цель ждать {LOW_TARGET_WAIT_MINUTES} мин.",
            f"История Яндекса: цель ждать {HISTORY_TARGET_WAIT_MINUTES} мин.",
            "Сейчас запас до остановки:",
            _buffer_line("🌅 утром", morning_walk_minutes, MORNING),
            _buffer_line("🌆 вечером", evening_walk_minutes, EVENING),
            "Запас до остановки - это не только маршрут по карте.",
            "Туда входит выйти из дома/здания, дойти до остановки и оставить маленький резерв.",
            "",
            "4. Вердикт:",
            f"🎯 <= {GO_NOW_THRESHOLD_MINUTES} мин до оптимального выхода - выходить сейчас;",
            f"🧥 <= {GET_READY_THRESHOLD_MINUTES} мин - собираться;",
            "✅ больше - можно ждать в безопасном месте.",
            "",
            "5. После кнопки слежу еще:",
            f"до {WATCH_DURATION_MINUTES} мин, проверка каждые {WATCH_POLL_INTERVAL_SECONDS} сек.",
            f"Ранний сигнал: <= {EARLY_ALERT_LEAVE_IN} мин до выхода.",
            f"Финальный сигнал: <= {FINAL_ALERT_LEAVE_IN} мин до выхода.",
            "Финальный сигнал отправляю одним сообщением.",
            "",
            "6. Доверие к данным:",
            "верю только свежему времени Яндекса для нужной посадочной остановки;",
            "если пришло только плановое время, интервал или общее время маршрута, не называю это прогнозом.",
        ]
    )


def _validate_walk_minutes(label: str, value: object) -> None:
    if not is_valid_walk_minutes(value):
        raise ValueError(f"{label} walk minutes is out of range")


def _buffer_line(label: str, minutes: int, profile: CommuteProfile) -> str:
    suffix = f" ({profile.walk_note})" if profile.walk_note else ""
    return f"{label}: {minutes} мин{suffix}."
