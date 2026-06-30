from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator

from route74.domain.walk_buffer import is_valid_walk_minutes


ProfileSelector = Literal["auto", "morning", "evening"]


class CatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profile: ProfileSelector = "auto"
    morning_walk_minutes: int | None = None
    evening_walk_minutes: int | None = None
    start_watch: bool = True

    @field_validator("morning_walk_minutes", "evening_walk_minutes")
    @classmethod
    def validate_walk_minutes(cls, value: int | None) -> int | None:
        if value is None:
            return None
        if not is_valid_walk_minutes(value):
            raise ValueError("walk minutes out of range")
        return value


class WatchStopRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profile_key: Literal["morning", "evening"]
