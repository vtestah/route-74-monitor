from __future__ import annotations

from dataclasses import dataclass


SELECTION_POLICY_NAME = "risk_adjusted_priority_with_quality_and_buffered_tie_override"
EARLY_CONFLICT_MINUTES = 3
MEDIUM_CONFIDENCE_EARLY_CONFLICT_MINUTES = 2
LOW_CONFIDENCE_EARLY_CONFLICT_MINUTES = 1


@dataclass(frozen=True)
class PredictionSelectionCandidate:
    key: str
    priority: int
    arrival_minutes: int
    early_conflict_eligible: bool
    safety_wait_minutes: int = 0
    early_conflict_minutes: int = EARLY_CONFLICT_MINUTES
    quality_rank: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.key, str) or not self.key.strip():
            raise ValueError("prediction selection candidate key is required")
        if _invalid_non_negative_int(self.priority):
            raise ValueError("prediction selection priority needs non-negative integer")
        if _invalid_int(self.arrival_minutes):
            raise ValueError("prediction selection ETA needs integer minutes")
        if not isinstance(self.early_conflict_eligible, bool):
            raise ValueError("prediction selection early conflict eligible needs boolean")
        if _invalid_non_negative_int(self.safety_wait_minutes):
            raise ValueError("prediction selection safety wait needs non-negative minutes")
        if _invalid_non_negative_int(self.early_conflict_minutes):
            raise ValueError("prediction selection early conflict needs non-negative minutes")
        if _invalid_non_negative_int(self.quality_rank):
            raise ValueError("prediction selection quality rank needs non-negative integer")

    @property
    def selection_minutes(self) -> int:
        return max(0, self.arrival_minutes - self.safety_wait_minutes)


def select_prediction_key(candidates: tuple[PredictionSelectionCandidate, ...]) -> str:
    if not isinstance(candidates, tuple) or any(
        not isinstance(candidate, PredictionSelectionCandidate) for candidate in candidates
    ):
        raise ValueError("prediction selection candidates need tuple of PredictionSelectionCandidate")
    _validate_unique_candidate_keys(candidates)
    valid_candidates = tuple(candidate for candidate in candidates if candidate.arrival_minutes >= 0)
    if not valid_candidates:
        raise ValueError("prediction selection needs at least one non-negative ETA candidate")
    selected = sorted(valid_candidates, key=_priority_sort_key)[0]
    buffered_tie_candidate = _buffered_tie_candidate(valid_candidates, selected)
    if buffered_tie_candidate is not None:
        return buffered_tie_candidate.key
    early_candidate = _materially_earlier_candidate(valid_candidates, selected)
    return (early_candidate or selected).key


def _validate_unique_candidate_keys(candidates: tuple[PredictionSelectionCandidate, ...]) -> None:
    seen: set[str] = set()
    for candidate in candidates:
        if candidate.key in seen:
            raise ValueError(f"duplicate prediction selection candidate key: {candidate.key}")
        seen.add(candidate.key)


def _priority_sort_key(candidate: PredictionSelectionCandidate) -> tuple[int, int, int, str]:
    return candidate.priority, candidate.quality_rank, candidate.selection_minutes, candidate.key


def _materially_earlier_candidate(
    candidates: tuple[PredictionSelectionCandidate, ...],
    selected: PredictionSelectionCandidate,
) -> PredictionSelectionCandidate | None:
    early_candidates = tuple(candidate for candidate in candidates if candidate.early_conflict_eligible)
    if not early_candidates:
        return None
    earliest = sorted(
        early_candidates,
        key=lambda item: (item.selection_minutes, item.priority, item.quality_rank, item.key),
    )[0]
    if earliest.selection_minutes + selected.early_conflict_minutes <= selected.selection_minutes:
        return earliest
    return None


def _buffered_tie_candidate(
    candidates: tuple[PredictionSelectionCandidate, ...],
    selected: PredictionSelectionCandidate,
) -> PredictionSelectionCandidate | None:
    if selected.safety_wait_minutes <= 0:
        return None
    candidates_with_real_earlier_eta = tuple(
        candidate
        for candidate in candidates
        if candidate.key != selected.key
        and candidate.early_conflict_eligible
        and candidate.arrival_minutes < selected.arrival_minutes
        and candidate.arrival_minutes <= selected.selection_minutes
    )
    if not candidates_with_real_earlier_eta:
        return None
    return sorted(
        candidates_with_real_earlier_eta,
        key=lambda item: (item.selection_minutes, item.priority, item.quality_rank, item.key),
    )[0]


def _invalid_non_negative_int(value: object) -> bool:
    return _invalid_int(value) or value < 0


def _invalid_int(value: object) -> bool:
    return isinstance(value, bool) or not isinstance(value, int)
