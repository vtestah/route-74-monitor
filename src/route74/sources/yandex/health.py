from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from route74.sources.yandex.models import YandexSourceMethod, YandexSourceStatus


COOLDOWN_STATUSES = {
    YandexSourceStatus.EMPTY,
    YandexSourceStatus.TIMEOUT,
    YandexSourceStatus.BLOCKED,
    YandexSourceStatus.UNAVAILABLE,
    YandexSourceStatus.PARSE_ERROR,
}


@dataclass(frozen=True)
class YandexCooldown:
    method: YandexSourceMethod
    until: datetime
    reason: str

    def active_at(self, current_time: datetime) -> bool:
        return current_time < self.until

    def remaining_seconds(self, current_time: datetime) -> int:
        return max(0, round((self.until - current_time).total_seconds()))


class YandexSourceHealth:
    def __init__(self) -> None:
        self._cooldowns: dict[tuple[str, YandexSourceMethod], YandexCooldown] = {}

    def cooldown(
        self,
        profile_key: str,
        method: YandexSourceMethod,
        current_time: datetime,
    ) -> YandexCooldown | None:
        key = (profile_key, method)
        cooldown = self._cooldowns.get(key)
        if cooldown is None:
            return None
        if cooldown.active_at(current_time):
            return cooldown
        self._cooldowns.pop(key, None)
        return None

    def record(
        self,
        profile_key: str,
        method: YandexSourceMethod,
        status: YandexSourceStatus,
        current_time: datetime,
        cooldown_seconds: int,
        reason: str,
    ) -> None:
        key = (profile_key, method)
        if status == YandexSourceStatus.OK or status == YandexSourceStatus.COORDINATES_ONLY:
            self._cooldowns.pop(key, None)
            return
        if status not in COOLDOWN_STATUSES or cooldown_seconds <= 0:
            return
        self._cooldowns[key] = YandexCooldown(
            method=method,
            until=current_time + timedelta(seconds=cooldown_seconds),
            reason=reason or status.value,
        )
