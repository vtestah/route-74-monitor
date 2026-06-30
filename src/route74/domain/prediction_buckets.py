from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PredictionEtaBucket:
    label: str
    max_minutes: int | None
    accuracy_tolerance_minutes: int

    def __post_init__(self) -> None:
        _validate_bucket_label(self.label)
        if self.max_minutes is not None and _invalid_int(self.max_minutes):
            raise ValueError("prediction ETA bucket max needs non-negative minutes or None")
        if _invalid_int(self.accuracy_tolerance_minutes):
            raise ValueError("prediction ETA bucket tolerance needs non-negative minutes")
        if self.max_minutes is not None and self.accuracy_tolerance_minutes > self.max_minutes:
            raise ValueError("prediction ETA bucket tolerance must not exceed finite bucket max")

    def contains(self, minutes: int) -> bool:
        _validate_minutes(minutes)
        if self.max_minutes is None:
            return True
        return minutes <= self.max_minutes


def _validate_minutes(minutes: int) -> None:
    if _invalid_int(minutes):
        raise ValueError("prediction ETA bucket needs non-negative minutes as an integer")


def _invalid_int(value: object) -> bool:
    return isinstance(value, bool) or not isinstance(value, int) or value < 0


def _validate_bucket_label(label: str) -> None:
    if not isinstance(label, str) or not label:
        raise ValueError("prediction ETA bucket label is required")
    if label != label.strip() or any(char.isspace() for char in label):
        raise ValueError("prediction ETA bucket label must be compact")
    if not label.isascii() or any(not (char.isalnum() or char in {"_", "-", "+"}) for char in label):
        raise ValueError("prediction ETA bucket label must be an ASCII key")


def validate_prediction_eta_buckets(
    buckets: tuple[PredictionEtaBucket, ...],
) -> tuple[PredictionEtaBucket, ...]:
    if not isinstance(buckets, tuple) or not buckets:
        raise ValueError("prediction ETA buckets must be a non-empty tuple")

    previous_max_minutes: int | None = None
    previous_tolerance_minutes = -1
    seen_labels: set[str] = set()
    for index, bucket in enumerate(buckets):
        if not isinstance(bucket, PredictionEtaBucket):
            raise ValueError("prediction ETA buckets must contain PredictionEtaBucket items")
        if bucket.label in seen_labels:
            raise ValueError(f"duplicate prediction ETA bucket label: {bucket.label}")
        seen_labels.add(bucket.label)
        if bucket.max_minutes is None and index != len(buckets) - 1:
            raise ValueError("open-ended prediction ETA bucket must be last")
        if bucket.max_minutes is not None:
            if previous_max_minutes is not None and bucket.max_minutes <= previous_max_minutes:
                raise ValueError("prediction ETA bucket max minutes must increase")
            previous_max_minutes = bucket.max_minutes
        if bucket.accuracy_tolerance_minutes < previous_tolerance_minutes:
            raise ValueError("prediction ETA bucket tolerance must not shrink")
        previous_tolerance_minutes = bucket.accuracy_tolerance_minutes

    if buckets[-1].max_minutes is not None:
        raise ValueError("last prediction ETA bucket must be open-ended")
    return buckets


PREDICTION_ETA_BUCKETS = validate_prediction_eta_buckets(
    (
        PredictionEtaBucket("0-3", max_minutes=3, accuracy_tolerance_minutes=1),
        PredictionEtaBucket("3-6", max_minutes=6, accuracy_tolerance_minutes=2),
        PredictionEtaBucket("6-10", max_minutes=10, accuracy_tolerance_minutes=3),
        PredictionEtaBucket("10-15", max_minutes=15, accuracy_tolerance_minutes=4),
        PredictionEtaBucket("15+", max_minutes=None, accuracy_tolerance_minutes=5),
    )
)


def prediction_eta_bucket(minutes: int) -> PredictionEtaBucket:
    _validate_minutes(minutes)
    for bucket in PREDICTION_ETA_BUCKETS:
        if bucket.contains(minutes):
            return bucket
    return PREDICTION_ETA_BUCKETS[-1]


def prediction_bucket_label(minutes: int) -> str:
    return prediction_eta_bucket(minutes).label


def prediction_bucket_tolerance(minutes: int) -> int:
    return prediction_eta_bucket(minutes).accuracy_tolerance_minutes
