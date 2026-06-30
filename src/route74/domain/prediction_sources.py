from __future__ import annotations

from dataclasses import dataclass

from route74.domain.eta import EtaSource


SOURCE_TARGET_STOP_LIVE = "target_stop_live"
SOURCE_CORRECTED_LIVE = "corrected_live"
SOURCE_VEHICLE_PROGRESS = "vehicle_progress"
SOURCE_HISTORY_HEADWAY = "history_headway"
SOURCE_ENSEMBLE = "ensemble"


@dataclass(frozen=True)
class PredictionSourceSpec:
    eta_source: EtaSource
    event_source: str
    priority: int
    is_live: bool
    early_conflict_eligible: bool

    def __post_init__(self) -> None:
        if not isinstance(self.eta_source, EtaSource):
            raise ValueError("prediction source ETA source is required")
        if not _is_plain_key(self.event_source):
            raise ValueError("prediction source event source must be a plain key")
        if isinstance(self.priority, bool) or not isinstance(self.priority, int) or self.priority < 0:
            raise ValueError("prediction source priority needs non-negative integer")
        if not isinstance(self.is_live, bool):
            raise ValueError("prediction source live flag needs boolean")
        if not isinstance(self.early_conflict_eligible, bool):
            raise ValueError("prediction source early conflict flag needs boolean")


def validate_prediction_source_specs(specs: tuple[PredictionSourceSpec, ...]) -> tuple[PredictionSourceSpec, ...]:
    if not isinstance(specs, tuple):
        raise ValueError("prediction source specs need tuple")
    eta_sources: set[EtaSource] = set()
    event_sources: set[str] = set()
    priorities: set[int] = set()
    for spec in specs:
        if not isinstance(spec, PredictionSourceSpec):
            raise ValueError("prediction source specs need PredictionSourceSpec entries")
        if spec.eta_source in eta_sources:
            raise ValueError(f"duplicate prediction ETA source: {spec.eta_source}")
        if spec.event_source in event_sources:
            raise ValueError(f"duplicate prediction event source: {spec.event_source}")
        if spec.priority in priorities:
            raise ValueError(f"duplicate prediction source priority: {spec.priority}")
        eta_sources.add(spec.eta_source)
        event_sources.add(spec.event_source)
        priorities.add(spec.priority)
    missing = set(EtaSource) - eta_sources
    if missing:
        labels = ", ".join(sorted(source.value for source in missing))
        raise ValueError(f"missing prediction ETA source specs: {labels}")
    expected_priorities = tuple(range(len(specs)))
    actual_priorities = tuple(sorted(priorities))
    if actual_priorities != expected_priorities:
        expected = ", ".join(str(priority) for priority in expected_priorities)
        actual = ", ".join(str(priority) for priority in actual_priorities)
        raise ValueError(
            f"prediction source priorities must be contiguous from zero: expected {expected}, got {actual}"
        )
    return specs


def validate_evaluated_event_sources(
    sources: tuple[str, ...],
    specs: tuple[PredictionSourceSpec, ...],
) -> tuple[str, ...]:
    if not isinstance(sources, tuple):
        raise ValueError("evaluated event sources need tuple")
    spec_sources = {spec.event_source for spec in validate_prediction_source_specs(specs)}
    if SOURCE_ENSEMBLE in spec_sources:
        raise ValueError("ensemble source must not map ETA source")
    seen: set[str] = set()
    for source in sources:
        if not _is_plain_key(source):
            raise ValueError("evaluated event source must be a plain key")
        if source in seen:
            raise ValueError(f"duplicate evaluated event source: {source}")
        seen.add(source)
    expected = spec_sources | {SOURCE_ENSEMBLE}
    unknown = seen - expected
    if unknown:
        labels = ", ".join(sorted(unknown))
        raise ValueError(f"unknown evaluated event sources: {labels}")
    missing = expected - seen
    if missing:
        labels = ", ".join(sorted(missing))
        raise ValueError(f"missing evaluated event sources: {labels}")
    if not sources or sources[-1] != SOURCE_ENSEMBLE:
        raise ValueError("ensemble source must be last")
    ordered_sources = tuple(
        spec.event_source for spec in sorted(specs, key=lambda spec: spec.priority)
    )
    if sources != (*ordered_sources, SOURCE_ENSEMBLE):
        raise ValueError("evaluated event sources must follow prediction priority")
    return sources


def _is_plain_key(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and value == value.strip()
        and value.isascii()
        and all(char.isalnum() or char == "_" for char in value)
    )


PREDICTION_SOURCE_SPECS = validate_prediction_source_specs((
    PredictionSourceSpec(
        EtaSource.YANDEX_CORRECTED,
        SOURCE_CORRECTED_LIVE,
        priority=0,
        is_live=True,
        early_conflict_eligible=True,
    ),
    PredictionSourceSpec(
        EtaSource.YANDEX,
        SOURCE_TARGET_STOP_LIVE,
        priority=1,
        is_live=True,
        early_conflict_eligible=True,
    ),
    PredictionSourceSpec(
        EtaSource.VEHICLE_PROGRESS,
        SOURCE_VEHICLE_PROGRESS,
        priority=2,
        is_live=False,
        early_conflict_eligible=True,
    ),
    PredictionSourceSpec(
        EtaSource.YANDEX_HISTORY,
        SOURCE_HISTORY_HEADWAY,
        priority=3,
        is_live=False,
        early_conflict_eligible=False,
    ),
))

SOURCE_PRIORITY_BY_ETA_SOURCE = {spec.eta_source: spec.priority for spec in PREDICTION_SOURCE_SPECS}
EVENT_SOURCE_BY_ETA_SOURCE = {spec.eta_source: spec.event_source for spec in PREDICTION_SOURCE_SPECS}
ETA_SOURCE_BY_EVENT_SOURCE = {spec.event_source: spec.eta_source for spec in PREDICTION_SOURCE_SPECS}
EVENT_SOURCE_PRIORITY = {spec.event_source: spec.priority for spec in PREDICTION_SOURCE_SPECS}
LIVE_ETA_SOURCES = frozenset(spec.eta_source for spec in PREDICTION_SOURCE_SPECS if spec.is_live)
LIVE_EVENT_SOURCES = frozenset(spec.event_source for spec in PREDICTION_SOURCE_SPECS if spec.is_live)
EARLY_CONFLICT_ETA_SOURCES = frozenset(
    spec.eta_source for spec in PREDICTION_SOURCE_SPECS if spec.early_conflict_eligible
)
EARLY_CONFLICT_EVENT_SOURCES = frozenset(
    spec.event_source for spec in PREDICTION_SOURCE_SPECS if spec.early_conflict_eligible
)
EVALUATED_EVENT_SOURCES = validate_evaluated_event_sources((
    SOURCE_CORRECTED_LIVE,
    SOURCE_TARGET_STOP_LIVE,
    SOURCE_VEHICLE_PROGRESS,
    SOURCE_HISTORY_HEADWAY,
    SOURCE_ENSEMBLE,
), PREDICTION_SOURCE_SPECS)
