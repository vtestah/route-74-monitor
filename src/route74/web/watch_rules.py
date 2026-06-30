from __future__ import annotations

from route74.domain.commute import DepartureDecision, DepartureUrgency
from route74.domain.watch_policy import EARLY_ALERT_LEAVE_IN, FINAL_ALERT_LEAVE_IN


def is_early(decision: DepartureDecision) -> bool:
    return (
        decision.urgency in {DepartureUrgency.GET_READY, DepartureUrgency.GO_NOW}
        and decision.leave_in_minutes is not None
        and decision.leave_in_minutes <= EARLY_ALERT_LEAVE_IN
    )


def is_final(decision: DepartureDecision) -> bool:
    return (
        decision.urgency == DepartureUrgency.GO_NOW
        and decision.leave_in_minutes is not None
        and decision.leave_in_minutes <= FINAL_ALERT_LEAVE_IN
    )
