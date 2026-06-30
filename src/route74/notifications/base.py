from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class NotificationMessage:
    title: str
    body: str
    priority: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.title, str) or not self.title.strip():
            raise ValueError("notification title must be non-empty text")
        if not isinstance(self.body, str) or not self.body.strip():
            raise ValueError("notification body must be non-empty text")
        if isinstance(self.priority, bool) or not isinstance(self.priority, int):
            raise ValueError("notification priority must be an integer")


@dataclass(frozen=True)
class NotificationStatus:
    provider: str
    configured: bool
    detail: str

    def __post_init__(self) -> None:
        for field_name in ("provider", "detail"):
            value = getattr(self, field_name)
            if not isinstance(value, str):
                raise ValueError(f"notification {field_name} must be text")


@dataclass(frozen=True)
class NotificationSendResult:
    provider: str
    delivered: bool
    detail: str = ""
    error_type: str = ""

    def __post_init__(self) -> None:
        for field_name in ("provider", "detail", "error_type"):
            value = getattr(self, field_name)
            if not isinstance(value, str):
                raise ValueError(f"notification {field_name} must be text")


class Notifier(Protocol):
    def status(self) -> NotificationStatus: ...

    def send(self, message: NotificationMessage) -> NotificationSendResult: ...
