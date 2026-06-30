from __future__ import annotations

from dataclasses import dataclass

import httpx

from route74.diagnostics import sanitize_diagnostic_text
from route74.notifications.base import NotificationMessage, NotificationSendResult, NotificationStatus, Notifier
from route74.notifications.config import PushoverConfig


PUSHOVER_API_URL = "https://api.pushover.net/1/messages.json"
PUSHOVER_TIMEOUT_SECONDS = 10.0


@dataclass(frozen=True)
class NullNotifier:
    provider: str = "pushover"
    detail: str = "Pushover не настроен"

    def status(self) -> NotificationStatus:
        return NotificationStatus(provider=self.provider, configured=False, detail=self.detail)

    def send(self, message: NotificationMessage) -> NotificationSendResult:
        return NotificationSendResult(provider=self.provider, delivered=False, detail=self.detail)


class PushoverNotifier:
    def __init__(self, config: PushoverConfig) -> None:
        self._config = config

    def status(self) -> NotificationStatus:
        return NotificationStatus(provider="pushover", configured=True, detail="Pushover готов")

    def send(self, message: NotificationMessage) -> NotificationSendResult:
        payload = {
            "token": self._config.app_token,
            "user": self._config.user_key,
            "title": message.title,
            "message": message.body,
            "priority": str(message.priority),
        }
        try:
            with httpx.Client(timeout=PUSHOVER_TIMEOUT_SECONDS) as client:
                response = client.post(PUSHOVER_API_URL, data=payload)
                response.raise_for_status()
                data = response.json()
        except Exception as exc:
            return NotificationSendResult(
                provider="pushover",
                delivered=False,
                detail=sanitize_diagnostic_text(exc, fallback=type(exc).__name__, limit=120),
                error_type=type(exc).__name__,
            )
        if not isinstance(data, dict) or int(data.get("status", 0)) != 1:
            return NotificationSendResult(
                provider="pushover",
                delivered=False,
                detail="Pushover вернул неуспешный ответ",
                error_type="PushoverResponseError",
            )
        return NotificationSendResult(provider="pushover", delivered=True, detail="delivered")


def build_notifier(config: PushoverConfig | None) -> Notifier:
    if config is None:
        return NullNotifier()
    return PushoverNotifier(config)
