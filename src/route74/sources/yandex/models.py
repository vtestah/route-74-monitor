from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import StrEnum
from math import isfinite

from route74.domain.eta import EtaConfidence


MAX_YANDEX_RAW_REASON_LENGTH = 200
MAX_YANDEX_DIAGNOSTIC_LENGTH = 200


class YandexSourceMode(StrEnum):
    AUTO = "auto"
    HTTP = "http"
    BROWSER = "browser"
    OFF = "off"


class YandexSourceMethod(StrEnum):
    NONE = "none"
    HTTP = "http"
    BROWSER = "browser"
    VEHICLE_PREDICTION = "vehicle_prediction"
    STOP_INFO = "stop_info"


class YandexSourceStatus(StrEnum):
    DISABLED = "disabled"
    OK = "ok"
    COORDINATES_ONLY = "coordinates_only"
    SCHEDULE_ONLY = "schedule_only"
    FREQUENCY_ONLY = "frequency_only"
    NO_TARGET = "no_target"
    EMPTY = "empty"
    STALE = "stale"
    NEEDS_SIGNATURE = "needs_signature"
    BLOCKED = "blocked"
    TIMEOUT = "timeout"
    UNAVAILABLE = "unavailable"
    PARSE_ERROR = "parse_error"


@dataclass(frozen=True)
class YandexVehicle:
    vehicle_id: str
    lat: float | None = None
    lng: float | None = None
    arrival_minutes: int | None = None
    age_seconds: int | None = None
    thread_id: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.vehicle_id, str) or not self.vehicle_id:
            raise ValueError("Yandex vehicle id is required")
        if not isinstance(self.thread_id, str):
            raise ValueError("Yandex vehicle thread id needs text")
        if (self.lat is None) != (self.lng is None):
            raise ValueError("Yandex vehicle coordinates need latitude and longitude")
        _ensure_coordinate("latitude", self.lat, -90, 90)
        _ensure_coordinate("longitude", self.lng, -180, 180)


@dataclass(frozen=True)
class YandexLiveForecast:
    enabled: bool
    available: bool
    source_method: YandexSourceMethod
    status: YandexSourceStatus
    arrival_minutes: tuple[int, ...] = ()
    vehicles: tuple[YandexVehicle, ...] = ()
    vehicle_count: int = 0
    newest_age_seconds: int | None = None
    confidence: EtaConfidence = EtaConfidence.UNKNOWN
    fallback_reason: str = ""
    raw_status: str = ""
    diagnostics: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        _ensure_bool("enabled", self.enabled)
        _ensure_bool("available", self.available)
        if not isinstance(self.source_method, YandexSourceMethod):
            raise ValueError("Yandex forecast source method needs YandexSourceMethod")
        if not isinstance(self.status, YandexSourceStatus):
            raise ValueError("Yandex forecast status needs YandexSourceStatus")
        if not isinstance(self.arrival_minutes, tuple):
            raise ValueError("Yandex forecast arrival minutes need tuple")
        if not isinstance(self.vehicles, tuple) or any(
            not isinstance(vehicle, YandexVehicle) for vehicle in self.vehicles
        ):
            raise ValueError("Yandex forecast vehicles need tuple of YandexVehicle")
        _ensure_non_negative_integer("vehicle count", self.vehicle_count)
        if not isinstance(self.confidence, EtaConfidence):
            raise ValueError("Yandex forecast confidence needs EtaConfidence")
        _ensure_text("fallback reason", self.fallback_reason)
        _ensure_text("raw status", self.raw_status)
        if not isinstance(self.diagnostics, tuple) or any(
            not isinstance(item, str) for item in self.diagnostics
        ):
            raise ValueError("Yandex forecast diagnostics need tuple of text")
        object.__setattr__(
            self,
            "diagnostics",
            tuple(
                item
                for item in (
                    _compact_text(value, MAX_YANDEX_DIAGNOSTIC_LENGTH)
                    for value in self.diagnostics
                )
                if item
            ),
        )
        if not self.enabled and self.available:
            raise ValueError("disabled Yandex forecast cannot be available")

    @classmethod
    def disabled(cls) -> "YandexLiveForecast":
        return cls(
            enabled=False,
            available=False,
            source_method=YandexSourceMethod.NONE,
            status=YandexSourceStatus.DISABLED,
        )

    @classmethod
    def unavailable(
        cls,
        *,
        status: YandexSourceStatus,
        source_method: YandexSourceMethod = YandexSourceMethod.NONE,
        reason: str = "",
        diagnostics: tuple[str, ...] = (),
    ) -> "YandexLiveForecast":
        return cls(
            enabled=True,
            available=False,
            source_method=source_method,
            status=status,
            fallback_reason=reason,
            diagnostics=diagnostics,
        )

    def with_method(self, method: YandexSourceMethod) -> "YandexLiveForecast":
        return replace(self, source_method=method)


@dataclass(frozen=True)
class YandexRawResponse:
    payload: dict[str, object] | None
    status: YandexSourceStatus
    reason: str = ""

    def __post_init__(self) -> None:
        if self.payload is not None and not isinstance(self.payload, dict):
            raise ValueError("Yandex raw response payload needs dictionary or None")
        if not isinstance(self.status, YandexSourceStatus):
            raise ValueError("Yandex raw response status needs YandexSourceStatus")
        if not isinstance(self.reason, str):
            raise ValueError("Yandex raw response reason needs text")
        object.__setattr__(self, "reason", _compact_text(self.reason, MAX_YANDEX_RAW_REASON_LENGTH))
        if self.status == YandexSourceStatus.OK and self.payload is None:
            raise ValueError("OK Yandex raw response needs payload")


def _ensure_bool(label: str, value: object) -> None:
    if not isinstance(value, bool):
        raise ValueError(f"Yandex forecast {label} needs bool")


def _ensure_non_negative_integer(label: str, value: object) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"Yandex forecast {label} needs non-negative integer")


def _ensure_text(label: str, value: object) -> None:
    if not isinstance(value, str):
        raise ValueError(f"Yandex forecast {label} needs text")


def _compact_text(value: str, max_length: int) -> str:
    printable = "".join(character if character.isprintable() else " " for character in value)
    return " ".join(printable.split())[:max_length]


def _ensure_coordinate(label: str, value: object, lower: float, upper: float) -> None:
    if value is None:
        return
    if (
        isinstance(value, bool)
        or not isinstance(value, int | float)
        or not isfinite(value)
        or not lower <= value <= upper
    ):
        raise ValueError(f"Yandex vehicle {label} must be a finite coordinate")
