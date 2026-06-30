from __future__ import annotations

from dataclasses import dataclass

from route74.domain.eta import EtaConfidence


@dataclass(frozen=True)
class EtaPolicy:
    high_spread_minutes: int = 5
    medium_spread_minutes: int = 10
    high_target_wait_minutes: int = 2
    medium_target_wait_minutes: int = 3
    low_target_wait_minutes: int = 5
    history_target_wait_minutes: int = 6
    vehicle_progress_medium_target_extra_minutes: int = 1
    vehicle_progress_low_target_extra_minutes: int = 2
    source_risk_high_miss_rate_percent: int = 35
    source_risk_very_high_miss_rate_percent: int = 50
    source_risk_high_min_buffer_minutes: int = 2
    source_risk_very_high_min_buffer_minutes: int = 3

    def __post_init__(self) -> None:
        _ensure_positive_int("high spread", self.high_spread_minutes)
        _ensure_positive_int("medium spread", self.medium_spread_minutes)
        _ensure_positive_int("high target wait", self.high_target_wait_minutes)
        _ensure_positive_int("medium target wait", self.medium_target_wait_minutes)
        _ensure_positive_int("low target wait", self.low_target_wait_minutes)
        _ensure_positive_int("history target wait", self.history_target_wait_minutes)
        _ensure_non_negative_int(
            "vehicle progress medium target extra",
            self.vehicle_progress_medium_target_extra_minutes,
        )
        _ensure_non_negative_int(
            "vehicle progress low target extra",
            self.vehicle_progress_low_target_extra_minutes,
        )
        _ensure_percent(
            "high source risk miss rate",
            self.source_risk_high_miss_rate_percent,
        )
        _ensure_percent(
            "very high source risk miss rate",
            self.source_risk_very_high_miss_rate_percent,
        )
        _ensure_non_negative_int(
            "high source risk buffer",
            self.source_risk_high_min_buffer_minutes,
        )
        _ensure_non_negative_int(
            "very high source risk buffer",
            self.source_risk_very_high_min_buffer_minutes,
        )
        if self.medium_spread_minutes <= self.high_spread_minutes:
            raise ValueError("medium spread must be greater than high spread")
        if not (
            self.high_target_wait_minutes
            < self.medium_target_wait_minutes
            < self.low_target_wait_minutes
            < self.history_target_wait_minutes
        ):
            raise ValueError("target wait minutes must strictly grow as confidence decreases")
        if (
            self.vehicle_progress_low_target_extra_minutes
            < self.vehicle_progress_medium_target_extra_minutes
        ):
            raise ValueError("low vehicle progress extra must not be below medium vehicle progress extra")
        if (
            self.source_risk_very_high_miss_rate_percent
            <= self.source_risk_high_miss_rate_percent
        ):
            raise ValueError("very high source risk miss rate must be greater than high source risk miss rate")
        if self.source_risk_high_miss_rate_percent <= 0:
            raise ValueError("high source risk miss rate must be above zero")
        if self.source_risk_high_min_buffer_minutes <= 0:
            raise ValueError("high source risk buffer must be a positive integer")
        if (
            self.source_risk_very_high_min_buffer_minutes
            < self.source_risk_high_min_buffer_minutes
        ):
            raise ValueError("very high source risk buffer must not be below high source risk buffer")


def _ensure_positive_int(label: str, value: int) -> None:
    if _invalid_int(value) or value <= 0:
        raise ValueError(f"{label} must be a positive integer")


def _ensure_non_negative_int(label: str, value: int) -> None:
    if _invalid_int(value) or value < 0:
        raise ValueError(f"{label} must be a non-negative integer")


def _ensure_percent(label: str, value: int) -> None:
    if _invalid_int(value) or not 0 <= value <= 100:
        raise ValueError(f"{label} must be an integer percent from 0 to 100")


def _invalid_int(value: object) -> bool:
    return isinstance(value, bool) or not isinstance(value, int)


DEFAULT_ETA_POLICY = EtaPolicy()


def target_wait_minutes_for_confidence(
    confidence: EtaConfidence,
    *,
    policy: EtaPolicy = DEFAULT_ETA_POLICY,
) -> int:
    _ensure_policy(policy)
    _ensure_eta_confidence(confidence)
    if confidence == EtaConfidence.HIGH:
        return policy.high_target_wait_minutes
    if confidence == EtaConfidence.MEDIUM:
        return policy.medium_target_wait_minutes
    return policy.low_target_wait_minutes


def vehicle_progress_target_extra_minutes_for_confidence(
    confidence: EtaConfidence,
    *,
    policy: EtaPolicy = DEFAULT_ETA_POLICY,
) -> int:
    _ensure_policy(policy)
    _ensure_eta_confidence(confidence)
    if confidence == EtaConfidence.HIGH:
        return 0
    if confidence == EtaConfidence.MEDIUM:
        return policy.vehicle_progress_medium_target_extra_minutes
    return policy.vehicle_progress_low_target_extra_minutes


def is_high_source_risk(
    miss_rate_percent: int,
    *,
    policy: EtaPolicy = DEFAULT_ETA_POLICY,
) -> bool:
    _ensure_policy(policy)
    _ensure_percent("source risk miss rate", miss_rate_percent)
    return miss_rate_percent >= policy.source_risk_high_miss_rate_percent


def is_very_high_source_risk(
    miss_rate_percent: int,
    *,
    policy: EtaPolicy = DEFAULT_ETA_POLICY,
) -> bool:
    _ensure_policy(policy)
    _ensure_percent("source risk miss rate", miss_rate_percent)
    return miss_rate_percent >= policy.source_risk_very_high_miss_rate_percent


def source_risk_buffer_floor_minutes(
    miss_rate_percent: int,
    *,
    policy: EtaPolicy = DEFAULT_ETA_POLICY,
) -> int:
    _ensure_policy(policy)
    _ensure_percent("source risk miss rate", miss_rate_percent)
    if is_very_high_source_risk(miss_rate_percent, policy=policy):
        return policy.source_risk_very_high_min_buffer_minutes
    if is_high_source_risk(miss_rate_percent, policy=policy):
        return policy.source_risk_high_min_buffer_minutes
    return 0


def _ensure_policy(policy: EtaPolicy) -> None:
    if not isinstance(policy, EtaPolicy):
        raise ValueError("ETA policy needs EtaPolicy")


def _ensure_eta_confidence(confidence: EtaConfidence) -> None:
    if not isinstance(confidence, EtaConfidence):
        raise ValueError("ETA confidence needs EtaConfidence")

HIGH_SPREAD_MINUTES = DEFAULT_ETA_POLICY.high_spread_minutes
MEDIUM_SPREAD_MINUTES = DEFAULT_ETA_POLICY.medium_spread_minutes
HIGH_TARGET_WAIT_MINUTES = DEFAULT_ETA_POLICY.high_target_wait_minutes
MEDIUM_TARGET_WAIT_MINUTES = DEFAULT_ETA_POLICY.medium_target_wait_minutes
LOW_TARGET_WAIT_MINUTES = DEFAULT_ETA_POLICY.low_target_wait_minutes
HISTORY_TARGET_WAIT_MINUTES = DEFAULT_ETA_POLICY.history_target_wait_minutes
VEHICLE_PROGRESS_MEDIUM_TARGET_EXTRA_MINUTES = (
    DEFAULT_ETA_POLICY.vehicle_progress_medium_target_extra_minutes
)
VEHICLE_PROGRESS_LOW_TARGET_EXTRA_MINUTES = DEFAULT_ETA_POLICY.vehicle_progress_low_target_extra_minutes
SOURCE_RISK_HIGH_MISS_RATE_PERCENT = DEFAULT_ETA_POLICY.source_risk_high_miss_rate_percent
SOURCE_RISK_VERY_HIGH_MISS_RATE_PERCENT = (
    DEFAULT_ETA_POLICY.source_risk_very_high_miss_rate_percent
)
SOURCE_RISK_HIGH_MIN_BUFFER_MINUTES = DEFAULT_ETA_POLICY.source_risk_high_min_buffer_minutes
SOURCE_RISK_VERY_HIGH_MIN_BUFFER_MINUTES = (
    DEFAULT_ETA_POLICY.source_risk_very_high_min_buffer_minutes
)
