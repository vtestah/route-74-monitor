from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class DepartureChange:
    previous_sampled_at: datetime
    current_sampled_at: datetime
    previous_arrival_at: datetime | None
    current_arrival_at: datetime | None
    arrival_shift_minutes: int | None
    previous_source: str
    current_source: str

    @property
    def source_changed(self) -> bool:
        return bool(self.previous_source and self.current_source and self.previous_source != self.current_source)

    def __post_init__(self) -> None:
        if not isinstance(self.previous_sampled_at, datetime) or not isinstance(self.current_sampled_at, datetime):
            raise ValueError("departure change sampled_at needs datetime values")
        if self.previous_sampled_at >= self.current_sampled_at:
            raise ValueError("departure change previous sample must be before current sample")
        for name in ("previous_arrival_at", "current_arrival_at"):
            value = getattr(self, name)
            if value is not None and not isinstance(value, datetime):
                raise ValueError(f"departure change {name} needs datetime or None")
        if self.arrival_shift_minutes is not None and (
            isinstance(self.arrival_shift_minutes, bool) or not isinstance(self.arrival_shift_minutes, int)
        ):
            raise ValueError("departure change arrival shift needs integer minutes or None")
        if self.arrival_shift_minutes is not None and (
            self.previous_arrival_at is None or self.current_arrival_at is None
        ):
            raise ValueError("departure change arrival shift needs both arrival times")
        if not isinstance(self.previous_source, str) or not isinstance(self.current_source, str):
            raise ValueError("departure change sources need text")
