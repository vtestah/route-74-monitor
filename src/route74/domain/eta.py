from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class EtaConfidence(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    UNKNOWN = "unknown"


class EtaSource(StrEnum):
    YANDEX = "yandex"
    YANDEX_CORRECTED = "yandex_corrected"
    VEHICLE_PROGRESS = "vehicle_progress"
    YANDEX_HISTORY = "yandex_history"


ETA_SOURCE_LABELS = {
    EtaSource.YANDEX.value: "Яндекс live",
    EtaSource.YANDEX_CORRECTED.value: "Яндекс+поправка",
    EtaSource.VEHICLE_PROGRESS.value: "координата",
    EtaSource.YANDEX_HISTORY.value: "история Яндекса",
}


class EtaFactorKind(StrEnum):
    SAFETY_BUFFER = "safety_buffer"
    RESIDUAL_CORRECTION = "residual_correction"
    SOURCE_RISK = "source_risk"
    GUARDRAIL_UNAVAILABLE = "guardrail_unavailable"
    SPREAD = "spread"
    HISTORY_SAMPLE = "history_sample"
    HISTORY_DISAGREEMENT = "history_disagreement"
    VEHICLE_PROGRESS_BUFFER = "vehicle_progress_buffer"
    IGNORED_WEAK_PROGRESS = "ignored_weak_progress"
    IGNORED_LIVE_ETA = "ignored_live_eta"


class EtaExplanationCode(StrEnum):
    LIVE_ETA = "live_eta"
    CORRECTED_LIVE = "corrected_live"
    VEHICLE_PROGRESS = "vehicle_progress"
    HISTORY_FALLBACK = "history_fallback"
    RISK_BUFFER = "risk_buffer"
    WEAK_LIVE_IGNORED = "weak_live_ignored"
    STORAGE_GUARDRAIL = "storage_guardrail"
    NO_ETA = "no_eta"


class EtaExplanationAction(StrEnum):
    TRUST_ETA = "trust_eta"
    KEEP_BUFFER = "keep_buffer"
    CHECK_MAP = "check_map"
    WATCH_FOR_LIVE = "watch_for_live"
    WAIT_FOR_DATA = "wait_for_data"


MAX_ETA_WARNING_LENGTH = 200
MAX_ETA_FACTOR_SCOPE_LENGTH = 80
MAX_ETA_EXPLANATION_DETAIL_LENGTH = 120


@dataclass(frozen=True)
class EtaEstimate:
    source: EtaSource
    arrival_minutes: int

    def __post_init__(self) -> None:
        if not isinstance(self.source, EtaSource):
            raise ValueError("ETA estimate source needs EtaSource")
        if _invalid_non_negative_int(self.arrival_minutes):
            raise ValueError("ETA estimate needs non-negative arrival minutes")


@dataclass(frozen=True)
class EtaFactor:
    kind: EtaFactorKind
    minutes: int = 0
    sample_count: int = 0
    percent: int = 0
    scope: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.kind, EtaFactorKind):
            raise ValueError("ETA factor kind needs EtaFactorKind")
        if _invalid_non_negative_int(self.minutes):
            raise ValueError("ETA factor minutes need non-negative integer")
        if _invalid_non_negative_int(self.sample_count):
            raise ValueError("ETA factor sample count needs non-negative integer")
        if _invalid_percent(self.percent):
            raise ValueError("ETA factor percent needs 0..100 integer")
        if not isinstance(self.scope, str):
            raise ValueError("ETA factor scope needs text")
        if len(self.scope) > MAX_ETA_FACTOR_SCOPE_LENGTH or self.scope != " ".join(self.scope.split()):
            raise ValueError("ETA factor scope needs compact single-line text")


@dataclass(frozen=True)
class EtaExplanation:
    code: EtaExplanationCode
    action: EtaExplanationAction
    detail: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.code, EtaExplanationCode):
            raise ValueError("ETA explanation code needs EtaExplanationCode")
        if not isinstance(self.action, EtaExplanationAction):
            raise ValueError("ETA explanation action needs EtaExplanationAction")
        if not isinstance(self.detail, str):
            raise ValueError("ETA explanation detail needs text")
        if (
            len(self.detail) > MAX_ETA_EXPLANATION_DETAIL_LENGTH
            or self.detail != " ".join(self.detail.split())
        ):
            raise ValueError("ETA explanation detail needs compact single-line text")


@dataclass(frozen=True)
class EtaConsensus:
    selected_source: EtaSource | None
    arrival_minutes: int | None
    confidence: EtaConfidence
    target_wait_minutes: int
    spread_minutes: int | None
    warning: str
    estimates: tuple[EtaEstimate, ...] = ()
    factors: tuple[EtaFactor, ...] = ()
    explanations: tuple[EtaExplanation, ...] = ()

    def __post_init__(self) -> None:
        if self.selected_source is not None and not isinstance(self.selected_source, EtaSource):
            raise ValueError("ETA consensus selected source needs EtaSource")
        if not isinstance(self.confidence, EtaConfidence):
            raise ValueError("ETA consensus confidence needs EtaConfidence")
        if self.arrival_minutes is not None and _invalid_non_negative_int(self.arrival_minutes):
            raise ValueError("ETA consensus needs non-negative arrival minutes")
        if _invalid_non_negative_int(self.target_wait_minutes):
            raise ValueError("ETA consensus target wait needs non-negative minutes")
        if self.spread_minutes is not None and _invalid_non_negative_int(self.spread_minutes):
            raise ValueError("ETA consensus spread needs non-negative minutes")
        if not isinstance(self.warning, str):
            raise ValueError("ETA consensus warning needs text")
        if len(self.warning) > MAX_ETA_WARNING_LENGTH or self.warning != " ".join(self.warning.split()):
            raise ValueError("ETA consensus warning needs compact single-line text")
        if not isinstance(self.estimates, tuple) or any(
            not isinstance(estimate, EtaEstimate) for estimate in self.estimates
        ):
            raise ValueError("ETA consensus estimates need tuple of EtaEstimate")
        _validate_estimate_sources(self.estimates)
        if not isinstance(self.factors, tuple) or any(not isinstance(factor, EtaFactor) for factor in self.factors):
            raise ValueError("ETA consensus factors need tuple of EtaFactor")
        if not isinstance(self.explanations, tuple) or any(
            not isinstance(explanation, EtaExplanation) for explanation in self.explanations
        ):
            raise ValueError("ETA consensus explanations need tuple of EtaExplanation")
        if self.spread_minutes is not None and len(self.estimates) < 2:
            raise ValueError("ETA consensus spread needs at least two estimates")
        if self.spread_minutes is not None and self.spread_minutes != _spread_minutes(self.estimates):
            raise ValueError("ETA consensus spread must match estimates")
        if self.selected_source is None and self.arrival_minutes is not None:
            raise ValueError("ETA consensus arrival needs selected source")
        if self.selected_source is not None and self.arrival_minutes is None:
            raise ValueError("ETA consensus selected source needs arrival minutes")
        if self.estimates and (
            self.selected_source is None
            or self.arrival_minutes is None
            or not any(
                estimate.source == self.selected_source and estimate.arrival_minutes == self.arrival_minutes
                for estimate in self.estimates
            )
        ):
            raise ValueError("ETA consensus estimates need selected arrival")
        if self.selected_source is None and self.confidence != EtaConfidence.UNKNOWN:
            raise ValueError("ETA consensus without source needs unknown confidence")
        if self.selected_source is not None and self.confidence == EtaConfidence.UNKNOWN:
            raise ValueError("ETA consensus selected source needs known confidence")

    @classmethod
    def disabled(cls) -> "EtaConsensus":
        return cls(
            selected_source=None,
            arrival_minutes=None,
            confidence=EtaConfidence.UNKNOWN,
            target_wait_minutes=5,
            spread_minutes=None,
            warning="",
            explanations=(EtaExplanation(EtaExplanationCode.NO_ETA, EtaExplanationAction.WAIT_FOR_DATA),),
        )


def eta_source_text(source: EtaSource | str) -> str:
    if isinstance(source, EtaSource):
        key = source.value
    else:
        key = str(source or "").strip()
    if not key:
        return ""
    return ETA_SOURCE_LABELS.get(key, _compact_text(key, fallback="источник неизвестен", limit=48))


def eta_scope_text(scope: str) -> str:
    if not isinstance(scope, str):
        return _compact_text(scope, fallback="scope unknown", limit=48)
    value = scope.strip()
    if not value:
        return ""
    base, source = _split_eta_scope(value)
    source_text = eta_source_text(source) if source else ""
    if base == "live_eta_no_coordinates":
        return "без координаты машины"
    if base == "source":
        return _scope_with_source("по общей статистике источника", source_text, fallback="по источнику")
    if base == "bucket":
        return _scope_with_source("по прошлым ошибкам источника", source_text, fallback="по прошлым ошибкам")
    if base == "bot_runtime_bucket":
        return _scope_with_source(
            "по похожим ответам бота для источника",
            source_text,
            fallback="по похожим ответам бота",
        )
    if base == "bot_runtime_source":
        return _scope_with_source(
            "по реальным ответам бота для источника",
            source_text,
            fallback="по ответам бота по источнику",
        )
    return _compact_text(value, fallback="scope unknown", limit=48)


def _split_eta_scope(value: str) -> tuple[str, str]:
    if ":" not in value:
        return value, ""
    base, source = value.split(":", 1)
    return base, source


def _scope_with_source(prefix: str, source_text: str, *, fallback: str) -> str:
    return f"{prefix} {source_text}" if source_text else fallback


def _compact_text(value: object, *, fallback: str, limit: int) -> str:
    text = " ".join(str(value or "").split())
    if not text:
        return fallback
    return text[:limit]


def _invalid_non_negative_int(value: object) -> bool:
    return isinstance(value, bool) or not isinstance(value, int) or value < 0


def _invalid_percent(value: object) -> bool:
    return _invalid_non_negative_int(value) or value > 100


def _validate_estimate_sources(estimates: tuple[EtaEstimate, ...]) -> None:
    seen_sources: set[EtaSource] = set()
    for estimate in estimates:
        if estimate.source in seen_sources:
            raise ValueError(f"duplicate ETA consensus estimate source: {estimate.source.value}")
        seen_sources.add(estimate.source)


def _spread_minutes(estimates: tuple[EtaEstimate, ...]) -> int:
    arrivals = tuple(estimate.arrival_minutes for estimate in estimates)
    return max(arrivals) - min(arrivals)
